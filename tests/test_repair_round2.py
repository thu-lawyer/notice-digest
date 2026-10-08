"""t7 修复轮回归测试（R-1 / R-5 / D-2 预取规则）。

只做**离线**断言，不碰网络、不碰真实数据库：
· R-1 —— ``enrich_pending`` 曾用 ``structured_time`` / ``text_only`` 当局部变量名，
  与同名模块函数冲突，第一轮就 ``TypeError: 'int' object is not callable``，
  真实 enrich 全崩；
· R-5 —— 列表 API 会返回详情 404 的条目，单条失败不得中断整批，且 404 要计数进输出；
· D-2 —— 预取判定名单必须排除通用 HTTP 客户端库名（``python-urllib`` 曾把合法反馈
  静默吞掉），且丢弃必须可观测。
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

from notice_digest import enrich, feedback, fetch

TZ_SHANGHAI = timezone(timedelta(hours=8))


def _cfg():
    return types.SimpleNamespace(campus="thu", timeout=5, retries=1)


class _FakeStore:
    """只实现 enrich_pending 需要的那几个方法。"""

    def __init__(self, items):
        self._items = list(items)
        self.touched: list[str] = []
        self.updated: list[str] = []

    def pending_enrich(self, limit):
        return list(self._items[:limit])

    def touch_enrich_attempt(self, item_id):
        self.touched.append(item_id)

    def update_detail(self, item_id, detail):
        self.updated.append(item_id)


class TestEnrichPendingRobustness(unittest.TestCase):
    """R-1（不许 TypeError）+ R-5（单条失败不中断整批）。"""

    ITEMS = [
        {"id": "good-1", "title": "讲座 A"},
        {"id": "nf-404", "title": "已下架"},
        {"id": "boom", "title": "异常"},
        {"id": "good-2", "title": "无时间"},
    ]

    def setUp(self):
        self._orig_detail = fetch.fetch_detail
        self._orig_needs = enrich.needs_enrich
        enrich.needs_enrich = lambda item: True

        def fake_detail(campus, item_id, session=None, **kw):
            if item_id == "nf-404":
                raise fetch.FetchError("详情请求失败: HTTP 404 Not Found")
            if item_id == "boom":
                raise RuntimeError("boom")
            if item_id == "good-1":
                return {
                    "id": item_id,
                    "title": "讲座 A",
                    "event_start": "2026-10-20T19:00:00+08:00",
                    "content": "",
                }
            return {"id": item_id, "title": "无时间", "content": "正文无时间信息"}

        fetch.fetch_detail = fake_detail
        self.now = datetime(2026, 10, 8, 20, 0, tzinfo=TZ_SHANGHAI)

    def tearDown(self):
        fetch.fetch_detail = self._orig_detail
        enrich.needs_enrich = self._orig_needs

    def test_enrich_pending_survives_404_and_exceptions(self):
        store = _FakeStore(self.ITEMS)
        res = enrich.enrich_pending(store, _cfg(), self.now, limit=10, min_interval=0.0)

        # R-1：函数名没被局部变量遮蔽 —— 能跑完就说明 structured_time(detail, now) 可调用
        self.assertEqual(res["structured_time"], 1)
        self.assertEqual(res["no_time"], 1)

        # R-5：404 与普通异常都不中断整批，成功条目照常写入
        self.assertEqual(res["fetched"], 2)
        self.assertEqual(res["failed"], 2)
        self.assertEqual(res["not_found"], 1)
        self.assertEqual(res["errors"], 1)
        self.assertEqual(store.updated, ["good-1", "good-2"])

        # 统计口径向后兼容：cli.cmd_enrich 读的这些键必须都在
        for key in ("candidates", "fetched", "failed", "skipped",
                    "structured_time", "text_only", "no_time",
                    "not_found", "errors", "error_samples"):
            self.assertIn(key, res)

        # 404 条目已计入重试排队（touch），普通异常条目留给下一轮自然重试
        self.assertEqual(store.touched, ["nf-404"])
        self.assertTrue(any("boom" in s for s in res["error_samples"]))


class TestPrefetchRules(unittest.TestCase):
    """D-2：预取名单只认邮件代理/扫描器，不认通用客户端库；丢弃必须可观测。"""

    def test_unknown_ua_is_prefetch(self):
        self.assertTrue(feedback._looks_like_prefetch({}))
        self.assertTrue(feedback._looks_like_prefetch({"User-Agent": ""}))
        self.assertTrue(feedback._looks_like_prefetch({"User-Agent": "   "}))

    def test_known_mail_proxy_is_prefetch(self):
        for ua in (
            "Mozilla/5.0 (compatible) GoogleImageProxy",
            "YahooMailProxy; https://help.yahoo.com/kb/yahoo-mail-proxy-SLN28749.html",
            "Mozilla/5.0 Proofpoint Link Scanner",
            "Barracuda Sentinel",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        ):
            self.assertTrue(feedback._looks_like_prefetch({"User-Agent": ua}), ua)

    def test_generic_client_libraries_are_not_prefetch(self):
        """这五个里任何一个被判成预取，都会重现 D-2 的「合法反馈被吞」。"""
        for ua in (
            "Python-urllib/3.12",
            "python-requests/2.31.0",
            "curl/8.4.0",
            "Wget/1.21.4",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
        ):
            self.assertFalse(feedback._looks_like_prefetch({"User-Agent": ua}), ua)


if __name__ == "__main__":
    unittest.main()
