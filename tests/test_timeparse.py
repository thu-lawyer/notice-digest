"""timeparse 单元测试（unittest 风格，纯标准库）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from notice_digest.timeparse import BUCKETS, ParsedTime, bucket_label, parse_item_time

TZ = timezone(timedelta(hours=8))


def parse(ai_event_time: str = "", evidence: str = "", title: str = "", body: str = "", now=None):
    now = now or datetime(2026, 10, 8, 9, 0, tzinfo=TZ)  # 周四
    return parse_item_time(title, ai_event_time, evidence, body, now)


class TestRelativeDays(unittest.TestCase):
    def test_today(self):
        p = parse("今天")
        self.assertEqual(p.bucket, "today")
        self.assertEqual(p.start.date().isoformat(), "2026-10-08")

    def test_tonight_defaults_to_evening(self):
        p = parse("今晚 19:00 开始")
        self.assertEqual(p.start.hour, 19)
        self.assertEqual(p.bucket, "today")

    def test_tomorrow(self):
        p = parse("明天下午3点")
        self.assertEqual(p.bucket, "tomorrow")
        self.assertEqual(p.start.date().isoformat(), "2026-10-09")
        self.assertEqual(p.start.hour, 15)

    def test_day_after_tomorrow(self):
        p = parse("后天")
        self.assertEqual(p.bucket, "this_week")
        self.assertEqual(p.start.date().isoformat(), "2026-10-10")


class TestWeekdays(unittest.TestCase):
    def test_this_week_friday(self):
        p = parse("本周五 14:00")
        self.assertEqual(p.start.date().isoformat(), "2026-10-09")
        self.assertEqual(p.bucket, "tomorrow")

    def test_next_week_monday(self):
        p = parse("下周一")
        self.assertEqual(p.start.date().isoformat(), "2026-10-12")
        self.assertEqual(p.bucket, "next_week")

    def test_bare_weekday_rolls_forward(self):
        # 今天周四，说「周一」应指下周一（向前看）
        p = parse("周一 上午9点")
        self.assertEqual(p.start.date().isoformat(), "2026-10-12")

    def test_weekday_xiangqi(self):
        p = parse("星期六")
        self.assertEqual(p.start.date().isoformat(), "2026-10-10")

    def test_weekday_ri(self):
        p = parse("星期日")
        self.assertEqual(p.start.date().isoformat(), "2026-10-11")


class TestAbsoluteDates(unittest.TestCase):
    def test_with_weekday_suffix(self):
        p = parse("2026年10月10日（周六）")
        self.assertEqual(p.start.date().isoformat(), "2026-10-10")
        self.assertEqual(p.start.year, 2026)

    def test_without_year(self):
        p = parse("10月20日 19:00")
        self.assertEqual(p.start.date().isoformat(), "2026-10-20")
        self.assertEqual(p.start.hour, 19)

    def test_cross_year_rolls_forward(self):
        p = parse("1月5日")
        self.assertEqual(p.start.date().isoformat(), "2027-01-05")

    def test_hao_variant(self):
        p = parse("11月3号")
        self.assertEqual(p.start.date().isoformat(), "2026-11-03")


class TestRanges(unittest.TestCase):
    def test_from_today_to(self):
        p = parse("即日起至10月31日")
        self.assertIsNotNone(p.start)
        self.assertIsNotNone(p.end)
        self.assertEqual(p.start.date().isoformat(), "2026-10-08")
        self.assertEqual(p.end.date().isoformat(), "2026-10-31")

    def test_explicit_range(self):
        p = parse("10月10日至10月20日")
        self.assertEqual(p.start.date().isoformat(), "2026-10-10")
        self.assertEqual(p.end.date().isoformat(), "2026-10-20")


class TestDeadlines(unittest.TestCase):
    def test_deadline_month_day(self):
        p = parse("报名截止10月15日")
        self.assertIsNotNone(p.deadline)
        self.assertEqual(p.deadline.date().isoformat(), "2026-10-15")

    def test_deadline_with_hour(self):
        p = parse("报名截止10月15日17时")
        self.assertEqual(p.deadline.date().isoformat(), "2026-10-15")
        self.assertEqual(p.deadline.hour, 17)

    def test_deadline_day_only_with_hour(self):
        p = parse("截止12日17:00")
        self.assertIsNotNone(p.deadline)
        self.assertEqual(p.deadline.day, 12)
        self.assertEqual(p.deadline.hour, 17)

    def test_deadline_before_keyword(self):
        p = parse("10月20日截止")
        self.assertEqual(p.deadline.date().isoformat(), "2026-10-20")


class TestTimeExpressions(unittest.TestCase):
    def test_colon_time(self):
        p = parse("10月9日 18:30 开始")
        self.assertEqual(p.start.hour, 18)
        self.assertEqual(p.start.minute, 30)

    def test_dian_ban(self):
        p = parse("明天下午3点半")
        self.assertEqual(p.start.hour, 15)
        self.assertEqual(p.start.minute, 30)

    def test_x_qi(self):
        p = parse("10月9日 19 时起")
        self.assertEqual(p.start.hour, 19)

    def test_afternoon_adjustment(self):
        p = parse("10月9日 下午2点")
        self.assertEqual(p.start.hour, 14)


class TestFallbacks(unittest.TestCase):
    def test_unparseable_is_undated_and_keeps_evidence(self):
        p = parse("", evidence="详见通知正文，时间以主办方通知为准")
        self.assertEqual(p.bucket, "undated")
        self.assertIsNone(p.start)
        self.assertEqual(p.evidence, "详见通知正文，时间以主办方通知为准")

    def test_empty_inputs(self):
        p = parse("", "", "", "")
        self.assertIsInstance(p, ParsedTime)
        self.assertEqual(p.bucket, "undated")
        self.assertEqual(p.evidence, "")

    def test_body_fallback(self):
        p = parse("", "", "某某论坛", "活动时间：10月25日 14:00，地点：六教")
        self.assertIsNotNone(p.start)
        self.assertEqual(p.start.date().isoformat(), "2026-10-25")
        self.assertEqual(p.start.hour, 14)

    def test_evidence_prefers_matched_text(self):
        p = parse("2026年10月10日（周六）", "10月10日")
        self.assertEqual(p.bucket, "this_week")
        self.assertTrue(p.evidence)

    def test_past_date_bucket(self):
        p = parse("10月1日")
        self.assertEqual(p.bucket, "past")

    def test_never_raises_on_garbage(self):
        for junk in ("", "。。", "月日", "99月99日", "周九", "  ", None):
            with self.subTest(junk=junk):
                result = parse_item_time("标题", junk, junk, junk, datetime(2026, 10, 8, tzinfo=TZ))
                self.assertIn(result.bucket, BUCKETS)

    def test_far_future_is_undated_but_keeps_start(self):
        p = parse("2027年5月1日")
        self.assertEqual(p.bucket, "undated")
        self.assertIsNotNone(p.start)


class TestHelpers(unittest.TestCase):
    def test_as_dict(self):
        p = parse("10月10日")
        d = p.as_dict()
        self.assertEqual(set(d), {"start", "end", "deadline", "bucket", "evidence"})
        self.assertTrue(d["start"].startswith("2026-10-10"))

    def test_bucket_label(self):
        self.assertEqual(bucket_label("today"), "今天")
        self.assertEqual(bucket_label("nonexistent"), "待定")


if __name__ == "__main__":
    unittest.main()
