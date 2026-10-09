"""t27：详情端点 429 限流的可观测性、分级与优雅降级（离线用例）。

全部离线、stdlib only（无 requests、无网络、无新依赖）：
- fetch 层：直接给 ``_get_json`` / ``_rate_limit_retry_after`` 喂受控响应；
- enrich 层：打桩 ``enrich_mod.fetch.fetch_detail``，用**真实** Store（临时 SQLite）
  验证「未补全的条目原样留在 pending、不写失败态、不丢行」；
- cli 层：进程内跑 ``cli_main(['enrich', ...])``，打桩 ``report_failure`` 观测
  是否*尝试*发失败邮件（只观测，不替换判定逻辑）。

对应 docs/RUNBOOK.md §17。
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error

from notice_digest import cli as cli_mod
from notice_digest import config as config_mod
from notice_digest import enrich as enrich_mod
from notice_digest import fetch as fetch_mod
from notice_digest.store import Store, now_shanghai

#: 打桩用的 detail（成功路径）：ISO 事件时间 + 自由文本时间各一份
DETAIL_OK = {
    "detail_status": "complete",
    "body_status": "ok",
    "ai_event_time": "2026年12月1日 19:00-21:00",
    "event_start": "2026-12-01T19:00:00+08:00",
    "event_end": "2026-12-01T21:00:00+08:00",
    "ai_event_location": "六教6A118",
}


class _Resp:
    """最小响应桩：只需 ``headers.get``。"""

    def __init__(self, headers: dict | None = None):
        self.headers = dict(headers or {})


def _http_error(code: int, url: str = "https://pkuknow.cn/thu/api/notices/x") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", {}, None)


def envelope(item_id: str) -> dict:
    """最小可入库条目（``needs_enrich`` 为真：category=校园动态）。"""
    return {
        "id": item_id,
        "source_id": "t27",
        "source_name": "T27验证源",
        "title": f"T27 验证条目 {item_id}",
        "published_at": "2026-10-08T10:00:00+08:00",
        "url": "https://example.invalid/t27",
        "category": "校园动态",
        "intent_group": "information",
    }


class _T27Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="nd_t27_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "notice.db"
        env = self.tmp / ".env.t27"
        env.write_text(
            "ND_SMTP_HOST=smtp.t27-verify.invalid\n"
            "ND_SMTP_PORT=465\n"
            "ND_SMTP_USER=t27-verify-user\n"
            "ND_SMTP_PASS=t27-verify-placeholder\n"
            "ND_TO_ADDR=t27@t27-verify.invalid\n"
            "ND_FROM_ADDR=t27@t27-verify.invalid\n"
            "ND_HMAC_SECRET=t27-verify-hmac\n",
            encoding="utf-8",
        )
        self.env = env
        self.now = now_shanghai()
        cfg = config_mod.load_config(
            profile_path=self.tmp / "no-such-profile.yaml", env_path=env
        )
        self.cfg = dataclasses.replace(cfg, db_path=self.db, campus="thu")

    def seed(self, ids: list[str]) -> None:
        store = Store(self.db)
        try:
            store.upsert_items([envelope(i) for i in ids])
        finally:
            store.close()

    def rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute("SELECT * FROM items ORDER BY id"))
        finally:
            conn.close()

    @contextlib.contextmanager
    def patch_detail(self, func):
        """只替换**网络边界**（fetch_detail），不动判定逻辑。"""
        original = enrich_mod.fetch.fetch_detail
        enrich_mod.fetch.fetch_detail = func
        try:
            yield
        finally:
            enrich_mod.fetch.fetch_detail = original

    @contextlib.contextmanager
    def failure_mail_spy(self):
        calls: list[tuple] = []

        def spy(cfg, stage, detail, store=None):
            calls.append((stage, str(detail)))
            return "spy"

        original = cli_mod.report_failure
        cli_mod.report_failure = spy
        try:
            yield calls
        finally:
            cli_mod.report_failure = original

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli_mod.main(list(argv))
            except SystemExit as exc:  # argparse 退出
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def enrich_argv(self, limit: int = 20) -> list[str]:
        return [
            "enrich",
            "--db",
            str(self.db),
            "--limit",
            str(limit),
            "--env-file",
            str(self.env),
        ]


# ======================================================= 1. fetch：分类与不重试
class Test01FetchRateLimitClassification(unittest.TestCase):
    def test_01a_429_immediately_raises_rate_limited_without_retry(self) -> None:
        """429 必须**立即**抛 RateLimited：既不重试，也不在 _get_json 里睡退避。"""
        calls: list[str] = []
        sleeps: list[float] = []
        original_read = fetch_mod._read_json
        original_sleep = fetch_mod.time.sleep

        def fake_read_json(session, url, timeout):
            calls.append(url)
            raise _http_error(429, url)

        fetch_mod._read_json = fake_read_json
        fetch_mod.time.sleep = sleeps.append
        try:
            with self.assertRaises(fetch_mod.RateLimited) as ctx:
                fetch_mod._get_json("https://pkuknow.cn/thu/api/notices/x", timeout=5, retries=3)
        finally:
            fetch_mod._read_json = original_read
            fetch_mod.time.sleep = original_sleep

        self.assertEqual(len(calls), 1, "429 不得重试（重试只会烧退避时间）")
        self.assertEqual(sleeps, [], "退避不得发生在 _get_json 里（交调用方决定）")
        self.assertIsInstance(ctx.exception, fetch_mod.FetchError)
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("429", str(ctx.exception))

    def test_01b_429_is_not_in_the_break_tuple_path(self) -> None:
        """反证：400/404 走的 break 分支不得吞掉 429 的分类信息。"""
        original_read = fetch_mod._read_json
        fetch_mod._read_json = lambda *a, **k: (_ for _ in ()).throw(_http_error(400))
        try:
            with self.assertRaises(fetch_mod.FetchError) as ctx:
                fetch_mod._get_json("https://pkuknow.cn/thu/api/notices/x", timeout=5, retries=3)
        finally:
            fetch_mod._read_json = original_read
        self.assertNotIsInstance(ctx.exception, fetch_mod.RateLimited)
        self.assertIn("400", str(ctx.exception))

    def test_01c_rate_limit_retry_after_read_and_clamped(self) -> None:
        case = {None: 0.0, "": 0.0, "2": 2.0, "0": 0.0, "-5": 0.0, "abc": 0.0}
        for raw, want in case.items():
            headers = {} if raw is None else {"Retry-After": raw}
            self.assertEqual(
                fetch_mod._rate_limit_retry_after(_Resp(headers)),
                want,
                f"Retry-After={raw!r}",
            )
        self.assertEqual(
            fetch_mod._rate_limit_retry_after(_Resp({"Retry-After": "3600"})),
            fetch_mod.RATE_LIMIT_BACKOFF_CAP,
            "Retry-After 必须被钳到上界",
        )
        self.assertEqual(fetch_mod.RATE_LIMIT_BACKOFF_CAP, 30.0)
        self.assertEqual(fetch_mod._rate_limit_retry_after(object()), 0.0)

    def test_01d_rate_limited_carries_parsed_retry_after(self) -> None:
        original_read = fetch_mod._read_json
        fetch_mod._read_json = lambda *a, **k: (_ for _ in ()).throw(
            _http_error(429)
        )
        try:
            with self.assertRaises(fetch_mod.RateLimited) as ctx:
                fetch_mod._get_json("https://pkuknow.cn/thu/api/notices/x", timeout=5, retries=1)
        finally:
            fetch_mod._read_json = original_read
        self.assertEqual(ctx.exception.retry_after, 0.0, "实测该站 429 不带 Retry-After")


def _sink() -> dict:
    """enrich_pending 交给 enrich_one 的 sink 形状：**键齐全**，实现只按需覆盖。"""
    return {"failed": 0, "not_found": 0, "rate_limited": 0,
            "retry_after": 0.0, "error_samples": []}


# ================================================== 2. enrich_one：失败可观测性
class Test02EnrichOneObservability(_T27Base):
    def test_02a_fetch_error_writes_sample_with_429(self) -> None:
        sink = _sink()
        with self.patch_detail(lambda *a, **k: (_ for _ in ()).throw(
            fetch_mod.RateLimited("HTTP 429 for https://pkuknow.cn/thu/api/notices/t27:1", 7.5)
        )):
            got = enrich_mod.enrich_one(
                _CountingStore(), self.cfg, {"id": "t27:1"}, self.now, error_sink=sink
            )
        self.assertIsNone(got)
        self.assertEqual(sink["failed"], 1)
        self.assertEqual(sink["rate_limited"], 1)
        self.assertEqual(sink["not_found"], 0)
        self.assertEqual(sink["retry_after"], 7.5)
        self.assertEqual(len(sink["error_samples"]), 1, "修前这里恒为空列表")
        sample = sink["error_samples"][0]
        self.assertIn("429", sample)
        self.assertIn("t27:1", sample)
        self.assertIn("RateLimited", sample)

    def test_02b_404_still_counted_as_not_found_and_sampled(self) -> None:
        sink = _sink()
        with self.patch_detail(lambda *a, **k: (_ for _ in ()).throw(
            fetch_mod.FetchError("HTTP 404 for https://pkuknow.cn/thu/api/notices/t27:2")
        )):
            enrich_mod.enrich_one(
                _CountingStore(), self.cfg, {"id": "t27:2"}, self.now, error_sink=sink
            )
        self.assertEqual(sink["not_found"], 1)
        self.assertEqual(sink["rate_limited"], 0)
        self.assertIn("404", sink["error_samples"][0])

    def test_02c_item_id_containing_404_is_still_rate_limited(self) -> None:
        """条目 id 里出现 "404" 不得把 429 误判成 not_found（分类优先于文本匹配）。"""
        sink = _sink()
        with self.patch_detail(lambda *a, **k: (_ for _ in ()).throw(
            fetch_mod.RateLimited("HTTP 429 for https://pkuknow.cn/thu/api/notices/t27:404:9")
        )):
            enrich_mod.enrich_one(
                _CountingStore(), self.cfg, {"id": "t27:404:9"}, self.now, error_sink=sink
            )
        self.assertEqual(sink["rate_limited"], 1)
        self.assertEqual(sink["not_found"], 0)

    def test_02d_error_sink_none_is_safe(self) -> None:
        with self.patch_detail(lambda *a, **k: (_ for _ in ()).throw(
            fetch_mod.RateLimited("HTTP 429 for x")
        )):
            got = enrich_mod.enrich_one(
                _CountingStore(), self.cfg, {"id": "t27:3"}, self.now, error_sink=None
            )
        self.assertIsNone(got)

    def test_02e_cooldown_is_bounded_and_skipped_when_zero(self) -> None:
        sleeps: list[float] = []
        original = enrich_mod.time.sleep
        enrich_mod.time.sleep = sleeps.append
        try:
            self.assertEqual(enrich_mod._rate_limit_cooldown(0.0), 0.0)
            self.assertEqual(sleeps, [], "没有 Retry-After 时不等待（生产路径）")
            self.assertEqual(enrich_mod._rate_limit_cooldown(5.0), 5.0)
            self.assertEqual(enrich_mod._rate_limit_cooldown(9999.0), 30.0)
        finally:
            enrich_mod.time.sleep = original
        self.assertEqual(sleeps, [5.0, 30.0], "退避必须有上界")


class _CountingStore:
    """最小 Store 桩（只给 enrich_one 用），记录触碰与写入。"""

    def __init__(self) -> None:
        self.touched: list[str] = []
        self.updated: list[str] = []

    def touch_enrich_attempt(self, item_id: str) -> None:
        self.touched.append(item_id)

    def update_detail(self, item_id: str, detail: dict) -> None:
        self.updated.append(item_id)


# ============================================= 3. enrich_pending：优雅降级
class Test03EnrichBatchDegradation(_T27Base):
    def test_03a_batch_stops_at_first_429_and_keeps_rows_pending(self) -> None:
        ids = [f"t27:b{i}" for i in range(10)]
        self.seed(ids)
        seen: list[str] = []

        def detail(campus, item_id, **kw):
            seen.append(item_id)
            if len(seen) == 4:
                raise fetch_mod.RateLimited(f"HTTP 429 for {item_id}", 0.0)
            return dict(DETAIL_OK)

        store = Store(self.db)
        try:
            with self.patch_detail(detail):
                stats = enrich_mod.enrich_pending(store, self.cfg, self.now, limit=10, min_interval=0.0)
            rows = {r["id"]: r for r in self.rows()}
        finally:
            store.close()

        # pending_enrich 的顺序是 published_ts DESC, id DESC → b9 先，b6 是第 4 条
        self.assertEqual(seen, ["t27:b9", "t27:b8", "t27:b7", "t27:b6"])
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["stopped_reason"], "rate-limited")
        self.assertEqual(stats["stopped_at"], "t27:b6")
        self.assertEqual(stats["fetched"], 3)
        self.assertEqual(stats["attempted"], 4, "首次命中即收批：剩余 6 条一次都不打")
        self.assertEqual(stats["remaining_pending"], 6)
        self.assertEqual(stats["candidates"], 10)
        self.assertEqual(len(seen), 4)
        self.assertEqual(stats["per_item_seconds"].__len__(), 4)
        self.assertTrue(all(s >= 0 for s in stats["per_item_seconds"]))
        self.assertLess(stats["elapsed_seconds"], 10.0, "批次总时长必须有界")
        self.assertEqual(stats["errors"], 0)
        self.assertTrue(stats["error_samples"] and "429" in stats["error_samples"][0])

        # 不丢行、不写失败态：10 行都在，只有 3 行 complete，其余仍待补全
        self.assertEqual(len(rows), 10)
        complete = sorted(i for i in ids if str(rows[i]["detail_status"]) == "complete")
        self.assertEqual(complete, ["t27:b7", "t27:b8", "t27:b9"])
        for i in ("t27:b0", "t27:b1", "t27:b2", "t27:b3", "t27:b4", "t27:b5", "t27:b6"):
            self.assertIsNone(rows[i]["detail_status"], f"{i} 不得被写成任何终态")
            self.assertIsNone(rows[i]["detail_json"], f"{i} 不得被写入详情")
        # update_detail 不递增 enrich_attempts：只有真正打到 429 的那一条 +1，其余 9 条为 0
        self.assertEqual(int(rows["t27:b6"]["enrich_attempts"] or 0), 1)
        self.assertEqual(sum(int(rows[i]["enrich_attempts"] or 0) for i in ids), 1)

    def test_03b_batch_without_429_runs_to_completion(self) -> None:
        ids = [f"t27:c{i}" for i in range(3)]
        self.seed(ids)
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: dict(DETAIL_OK)):
                stats = enrich_mod.enrich_pending(store, self.cfg, self.now, limit=3, min_interval=0.0)
        finally:
            store.close()
        self.assertEqual(stats["fetched"], 3)
        self.assertEqual(stats["rate_limited"], 0)
        self.assertEqual(stats["stopped_reason"], "done")
        self.assertIsNone(stats["stopped_at"])
        self.assertEqual(stats["remaining_pending"], 0)
        self.assertEqual(stats["cooldown_seconds"], 0.0)

    def test_03c_cooldown_honoured_once_when_retry_after_present(self) -> None:
        self.seed(["t27:d0"])
        slept: list[float] = []
        original = enrich_mod.time.sleep
        enrich_mod.time.sleep = slept.append
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                fetch_mod.RateLimited("HTTP 429 for x", 3.0)
            )):
                stats = enrich_mod.enrich_pending(store, self.cfg, self.now, limit=1, min_interval=0.0)
        finally:
            enrich_mod.time.sleep = original
            store.close()
        self.assertEqual(slept, [3.0], "有 Retry-After 时只做一次有界退避")
        self.assertEqual(stats["cooldown_seconds"], 3.0)
        self.assertEqual(stats["retry_after"], 3.0)
        self.assertEqual(stats["rate_limited"], 1)


# ================================================= 4. cli：致命判据收窄（双向）
class Test04CliExitCodes(_T27Base):
    def _detail_seq(self, ok_count: int, exc: BaseException):
        seen = {"n": 0}

        def detail(campus, item_id, **kw):
            seen["n"] += 1
            if seen["n"] <= ok_count:
                return dict(DETAIL_OK)
            raise exc

        return detail

    def test_04a_all_429_batch_exits_zero_no_failure_mail(self) -> None:
        self.seed([f"t27:e{i}" for i in range(6)])
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                fetch_mod.RateLimited("HTTP 429 for https://pkuknow.cn/thu/api/notices/x", 0.0)
            )), self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=6))
        finally:
            store.close()

        self.assertEqual(code, 0, f"全 429 必须 exit 0（warn），stderr={err}")
        self.assertEqual(mails, [], "全 429 不得发失败邮件")
        stats = json.loads(out)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["fetched"], 0)
        self.assertEqual(stats["stopped_reason"], "rate-limited")
        self.assertNotIn("failure_mail", stats)
        kinds = [(a["kind"], a["severity"]) for a in stats["anomalies"]]
        self.assertIn(("detail-rate-limited", "warn"), kinds)
        self.assertNotIn("detail-fetch-all-failed", [k for k, _ in kinds])
        self.assertIn("[enrich][warn] detail-rate-limited", err)

    def test_04b_all_non_json_batch_still_exits_three_with_mail(self) -> None:
        self.seed([f"t27:f{i}" for i in range(4)])
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                fetch_mod.FetchError("Expecting value: line 1 column 1 (char 0)")
            )), self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=4))
        finally:
            store.close()

        self.assertEqual(code, 3, f"全非 JSON 仍须 exit 3，stderr={err}")
        self.assertEqual(len(mails), 1, "结构性失败仍须发失败邮件")
        stats = json.loads(out)
        self.assertEqual(stats["rate_limited"], 0)
        self.assertEqual(stats["fetched"], 0)
        kinds = [(a["kind"], a["severity"]) for a in stats["anomalies"]]
        self.assertIn(("detail-fetch-all-failed", "error"), kinds)
        self.assertNotIn("detail-rate-limited", [k for k, _ in kinds])

    def test_04c_all_5xx_batch_still_exits_three(self) -> None:
        self.seed([f"t27:g{i}" for i in range(4)])
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                fetch_mod.FetchError("HTTP 500 for https://pkuknow.cn/thu/api/notices/x")
            )), self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=4))
        finally:
            store.close()
        self.assertEqual(code, 3, f"全 5xx 仍须 exit 3，stderr={err}")
        self.assertEqual(len(mails), 1)
        stats = json.loads(out)
        self.assertIn("detail-fetch-all-failed", [a["kind"] for a in stats["anomalies"]])

    def test_04d_all_404_batch_is_not_fatal(self) -> None:
        """t7-R5 回归：单条详情 404 是上游噪音，不得升级成致命失败。"""
        self.seed([f"t27:h{i}" for i in range(3)])
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                fetch_mod.FetchError("HTTP 404 for https://pkuknow.cn/thu/api/notices/x")
            )), self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=3))
        finally:
            store.close()
        self.assertEqual(code, 0, f"全 404 不得致命，stderr={err}")
        self.assertEqual(mails, [])
        stats = json.loads(out)
        self.assertEqual(stats["not_found"], 3)
        self.assertNotIn("detail-fetch-all-failed", [a["kind"] for a in stats["anomalies"]])
        self.assertIn("detail-404", [a["kind"] for a in stats["anomalies"]])

    def test_04e_partial_success_then_429_exits_zero(self) -> None:
        """原判据（failed >= fetched）在这里会误报致命：先成功 3 条、随后被限流。"""
        self.seed([f"t27:k{i}" for i in range(6)])
        store = Store(self.db)
        try:
            with self.patch_detail(self._detail_seq(3, fetch_mod.RateLimited("HTTP 429 for x", 0.0))), \
                    self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=6))
            rows = self.rows()
        finally:
            store.close()
        self.assertEqual(code, 0, f"限流下的部分成功不得判致命，stderr={err}")
        self.assertEqual(mails, [])
        stats = json.loads(out)
        self.assertEqual(stats["fetched"], 3)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertNotIn("detail-fetch-all-failed", [a["kind"] for a in stats["anomalies"]])
        self.assertNotIn("parse-failure-spike", [a["kind"] for a in stats["anomalies"]])
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(1 for r in rows if str(r["detail_status"]) == "complete"), 3)

    def test_04f_non_fetch_exception_is_structural(self) -> None:
        """非 FetchError / 非 429 / 非 404 的真实异常仍是结构性失败（t7-F2 不被吞掉）。"""
        self.seed(["t27:m0"])
        store = Store(self.db)
        try:
            with self.patch_detail(lambda campus, item_id, **kw: (_ for _ in ()).throw(
                ValueError("detail 结构异常")
            )), self.failure_mail_spy() as mails:
                code, out, err = self.run_cli(self.enrich_argv(limit=1))
        finally:
            store.close()
        stats = json.loads(out)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["rate_limited"], 0)
        self.assertTrue(any("ValueError" in s for s in stats["error_samples"]))
        kinds = [(a["kind"], a["severity"]) for a in stats["anomalies"]]
        self.assertIn(("enrich-item-errors", "warn"), kinds)
        self.assertIn(("detail-fetch-all-failed", "error"), kinds)
        self.assertEqual(len(mails), 1, "一条详情都没补上且不可用限流/404 解释 ⇒ 仍须报警")
        self.assertEqual(code, 3)
        self.assertIn("[enrich][warn] enrich-item-errors", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
