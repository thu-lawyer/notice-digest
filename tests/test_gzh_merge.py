"""公众号源合并（t14）验收：gzh_source 单元、渲染合并、/nd/ics 单事件日历。

运行：``python3 -m pytest tests/test_gzh_merge.py -q``（项目根目录下）
"""

import dataclasses
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notice_digest import config as config_mod
from notice_digest import feedback as feedback_mod
from notice_digest import render as render_mod
from notice_digest.enrich import parsed_for_item
from notice_digest.gzh_source import (
    article_key,
    collect,
    mark_sent,
    normalize_title,
    rank,
)
from notice_digest.render import (
    SEP,
    build_gzh_subject,
    build_single_event_ics,
    render_email,
)
from notice_digest.score import Scored
from notice_digest.store import Store
from notice_digest.timeparse import ParsedTime

SH = render_mod.SHANGHAI
NOW = datetime(2026, 10, 10, 7, 30)
NOW_SH = datetime(2026, 10, 10, 7, 30, tzinfo=SH)
UNDATED = ParsedTime(start=None, end=None, deadline=None, bucket="undated", evidence="未给出时间")


def _env_file(work: Path) -> Path:
    lines = [
        "ND_HMAC_SECRET=t3-gzh-merge-hmac",
        "ND_FEEDBACK_BASE=http://feedback.example.invalid",
    ]
    path = work / "env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cfg(work: Path, **overrides):
    base = config_mod.load_config(
        profile_path=work / "no-such-profile.yaml", env_path=_env_file(work)
    )
    return dataclasses.replace(base, db_path=work / "db.sqlite", **overrides)


def _make_gzh_db(path: Path, now_epoch: int) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT,
            url TEXT,
            mp_id TEXT,
            pub_time INTEGER
        );
        CREATE TABLE feeds (id TEXT PRIMARY KEY, name TEXT);
        """
    )
    con.executemany(
        "INSERT INTO articles VALUES (?,?,?,?,?)",
        [
            (1, "法学院讲座：数字法治的理论前沿", "https://mp.weixin.qq.com/s/a1", "mp1", now_epoch - 3600),
            (2, "校园卡充值通知", "https://mp.weixin.qq.com/s/a2", "mp1", now_epoch - 7200),
            (3, "很旧的文章", "https://mp.weixin.qq.com/s/a3", "mp2", now_epoch - 30 * 86400),
        ],
    )
    con.executemany(
        "INSERT INTO feeds VALUES (?,?)",
        [("mp1", "清华法学院"), ("mp2", "某公众号")],
    )
    con.commit()
    con.close()


def _gzh(key: str, title: str) -> dict:
    return {
        "key": key,
        "title": title,
        "url": f"https://mp.weixin.qq.com/s/{key}",
        "mp": "清华法学院",
        "ts": int(time.time()),
    }


class GzhSourceTests(unittest.TestCase):
    """gzh_source：采集窗口、降级排序、台账回写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.db = self.work / "gzh.db"
        _make_gzh_db(self.db, int(time.time()))
        self.cfg = _cfg(
            self.work,
            gzh_db=str(self.db),
            gzh_state=str(self.work / "state-gzh.json"),
            gzh_top=2,
        )

    def test_collect_within_lookback(self):
        items = collect(self.cfg)
        self.assertEqual(len(items), 2)
        keys = {it["key"] for it in items}
        self.assertEqual(len(keys), 2)
        for it in items:
            self.assertTrue(it["title"])
            self.assertTrue(it["url"].startswith("https://mp.weixin.qq.com/"))
            self.assertTrue(it["mp"])

    def test_collect_missing_db_returns_empty(self):
        cfg = dataclasses.replace(self.cfg, gzh_db=str(self.work / "nope.db"))
        self.assertEqual(collect(cfg), [])

    def test_rank_without_api_key_falls_back(self):
        items = collect(self.cfg)
        picked = rank(items, 1, self.cfg)
        # rank 返回去重后的「下标列表」（cli 侧按下标映射回条目），不是条目本身。
        self.assertEqual(len(picked), 1)
        self.assertIsInstance(picked[0], int)
        self.assertTrue(0 <= picked[0] < len(items))
        self.assertEqual(len(set(picked)), len(picked))

    def test_mark_sent_roundtrip(self):
        items = collect(self.cfg)
        mark_sent(self.cfg, items)
        self.assertEqual(collect(self.cfg), [])

    def test_normalize_title_and_article_key(self):
        self.assertEqual(
            normalize_title("  「清 华 法 学」讲座！ "), normalize_title("清华法学讲座")
        )
        self.assertEqual(article_key("https://a/1"), article_key("https://a/1"))
        self.assertNotEqual(article_key("https://a/1"), article_key("https://a/2"))


class RenderGzhMergeTests(unittest.TestCase):
    """渲染合并：空短路、精选前置、+ 日历门控、单事件 ICS。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.cfg = _cfg(self.work)

    def _notice(self, item_id: str):
        item = {
            "id": item_id,
            "title": "测试通知",
            "url": f"https://pkuknow.cn/thu/doc/{item_id}",
            "source_name": "测试来源",
            "category": "校园动态",
            "intent_group": "notice",
            "ai_summary": "测试摘要",
        }
        return [Scored(item=item, score=1.0, reasons=[("test", 1.0)])]

    def test_empty_short_circuit(self):
        self.assertEqual(render_email([], {}, self.cfg, NOW), ("", "", ""))

    def test_gzh_only_subject_and_sections(self):
        g1, g2 = _gzh("k1", "数字法治讲座回顾"), _gzh("k2", "奖学金申请通知")
        subject, html, _ = render_email([], {}, self.cfg, NOW, gzh_picked=[g1, g2])
        self.assertEqual(subject, f"清华通知日报 10-10{SEP}公众号精选 2 篇")
        self.assertIn("公众号文章", html)
        self.assertIn("数字法治讲座回顾", html)
        self.assertIn("清华法学院", html)
        self.assertNotIn("今日推荐", html)

    def test_merged_subject_puts_recommendation_first(self):
        picked = [
            _gzh(f"k{i}", title)
            for i, title in enumerate(
                ["文章一", "文章二", "文章三", "文章四", "文章五", "文章六"], 1
            )
        ]
        subject, html, _ = render_email(
            self._notice("x:0"), {"x:0": UNDATED}, self.cfg, NOW, gzh_picked=picked
        )
        self.assertTrue(subject.startswith("清华通知日报 10-10"))
        self.assertTrue(subject.endswith(f"{SEP}公众号精选 6 篇"))
        # 前 5 篇前置「今日推荐」；第 6 篇留在通知之后的「公众号文章」节；互不重复。
        self.assertLess(html.index("今日推荐"), html.index("公众号文章"))
        self.assertLess(html.index("文章一"), html.index("测试通知"))
        for title in ("文章一", "文章二", "文章三", "文章四", "文章五", "文章六"):
            self.assertEqual(html.count(title), 1, title)

    def test_calendar_button_gated_on_parsed_start(self):
        start = datetime(2026, 10, 11, 14, 0, tzinfo=SH)
        parsed = {
            "x:0": ParsedTime(
                start=start, end=None, deadline=None, bucket="activity", evidence="10月11日 14:00"
            )
        }
        _, html, _ = render_email(self._notice("x:0"), parsed, self.cfg, NOW)
        self.assertIn("/nd/ics?", html)
        _, html2, _ = render_email(self._notice("x:0"), {"x:0": UNDATED}, self.cfg, NOW)
        self.assertNotIn("/nd/ics?", html2)

    def test_ics_timed_event_has_tzid_and_alarm(self):
        item = {"id": "gzh-abc", "title": "法学讲座", "url": "https://mp.weixin.qq.com/s/x1"}
        parsed = parsed_for_item(
            {
                "detail": {
                    "event_start": "2026-10-15T14:00:00+08:00",
                    "event_location": "法学院图书馆",
                    "ai_event_time": "10月15日 14:00",
                }
            },
            NOW_SH,
        )
        self.assertIsNotNone(parsed.start)
        ics = build_single_event_ics(item, parsed, NOW_SH)
        self.assertTrue(ics.endswith("\r\n"))
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("BEGIN:VTIMEZONE", ics)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261015T140000", ics)
        self.assertIn("TRIGGER:-PT30M", ics)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)

    def test_ics_all_day_uses_value_date_without_tzid(self):
        item = {"id": "gzh-abd", "title": "全天活动", "url": "https://mp.weixin.qq.com/s/x2"}
        parsed = parsed_for_item(
            {
                "detail": {
                    "event_start": "2026-10-15T00:00:00+08:00",
                    "ai_event_time": "10月15日",
                }
            },
            NOW_SH,
        )
        self.assertIsNotNone(parsed.start)
        ics = build_single_event_ics(item, parsed, NOW_SH)
        self.assertIn("DTSTART;VALUE=DATE:20261015", ics)
        vevent = ics.split("BEGIN:VEVENT")[1]
        self.assertNotIn("DTSTART;TZID", vevent)
        self.assertNotIn("BEGIN:VTIMEZONE", ics)
        self.assertIn("BEGIN:VALARM", ics)


class FeedbackIcsHttpTests(unittest.TestCase):
    """/nd/ics 端点：200、403、404 与签名校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.cfg = _cfg(self.work)
        self.store = Store(self.cfg.db_path)
        self.store.upsert_items(
            [
                {
                    "id": "t:ics1",
                    "source_id": "t3",
                    "source_name": "测试源",
                    "title": "「明德法学讲座」第 12 期",
                    "published_at": "2026-10-09T10:00:00+08:00",
                    "url": "https://pkuknow.cn/thu/doc/1",
                    "category": "学术活动",
                    "intent_group": "activity",
                    "detail": {
                        "event_start": "2026-10-15T14:00:00+08:00",
                        "event_location": "法学院图书馆",
                        "ai_event_time": "10月15日 14:00",
                    },
                },
                {
                    "id": "t:nostart",
                    "source_id": "t3",
                    "source_name": "测试源",
                    "title": "没有时间的通知",
                    "published_at": "2026-10-09T10:00:00+08:00",
                    "url": "https://pkuknow.cn/thu/doc/2",
                    "category": "校园动态",
                    "intent_group": "notice",
                    "detail": {},
                },
            ]
        )
        # detail 须经 update_detail 持久化（upsert_items 不落 detail 信封）。
        self.store.update_detail(
            "t:ics1",
            {
                "event_start": "2026-10-15T14:00:00+08:00",
                "event_location": "法学院图书馆",
                "ai_event_time": "10月15日 14:00",
            },
        )
        self.srv = feedback_mod.serve(self.cfg, self.store, port=0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    def _get(self, query: str):
        req = urllib.request.Request(f"{self.base}/nd/ics?{query}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")

    def test_happy_path_serves_ics(self):
        stored = self.store.get_item("t:ics1")
        self.assertTrue(stored.get("detail"), "detail 应经 upsert 持久化")
        token = feedback_mod.make_token("t:ics1", "ics", self.cfg)
        query = urllib.parse.urlencode({"i": "t:ics1", "id": "t:ics1", "t": token})
        status, headers, body = self._get(query)
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", headers.get("Content-Type", ""))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("filename*=UTF-8''", headers.get("Content-Disposition", ""))
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261015T140000", body)
        self.assertIn("BEGIN:VALARM", body)

    def test_wrong_kind_token_rejected(self):
        token = feedback_mod.make_token("t:ics1", "up", self.cfg)
        query = urllib.parse.urlencode({"i": "t:ics1", "id": "t:ics1", "t": token})
        status, _, body = self._get(query)
        self.assertEqual(status, 403)

    def test_missing_token_rejected(self):
        status, _, _ = self._get(urllib.parse.urlencode({"i": "t:ics1", "id": "t:ics1"}))
        self.assertEqual(status, 403)

    def test_unknown_item_rejected(self):
        token = feedback_mod.make_token("t:missing", "ics", self.cfg)
        query = urllib.parse.urlencode({"i": "t:missing", "id": "t:missing", "t": token})
        status, _, _ = self._get(query)
        self.assertEqual(status, 404)

    def test_item_without_start_rejected(self):
        token = feedback_mod.make_token("t:nostart", "ics", self.cfg)
        query = urllib.parse.urlencode({"i": "t:nostart", "id": "t:nostart", "t": token})
        status, _, _ = self._get(query)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
