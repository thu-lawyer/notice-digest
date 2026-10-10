"""独立验证套件（t3 round 1 + **t9 round 2**）：活体端到端 · ICS 合规 · 幂等 · 学习收敛 ·
失败模式 · 零配置 · 不发信 · 凭据扫描 · **R-1/D-2/R-4/R-5 关闭复验与退出码分级**.

本文件是**验证方独立编写**的，不是实现方的自检。三条纪律：

1. 端到端用例打真实 pkuknow.cn（无 mock、无固定 fixture），产物落在 ``data/t3/``；
2. ICS 合法性由本文件内的 :func:`validate_ics_bytes` 从 RFC 5545 直接实现，
   **不 import ``notice_digest.render`` 的任何 ICS 辅助函数**，也不复用其折行/转义逻辑；
3. 只在失败模式/不发信用例里打桩（monkeypatch），且打桩点是**被验证模块的边界**，
   不是把被测逻辑本身替换掉。

round 2 追加的绑定判据（captain，t9）：
   A. **D-2 只按行为判定** —— 合法 HMAC 签名 → HTTP 200 + ``feedback`` 真落库 1 行 +
      ``weights`` 真变化；同 (item_id, kind) 重复不重复计数；篡改签名 403。
      **禁止**以「源码里是否出现某个字符串」判 pass/fail（见 test_08a/08c）。
   B. **prefetch 丢弃不得静默** —— 必须同时有机器可读信号（非 200 / X-ND-* 头）与日志；
      且 ``python-urllib`` / ``requests`` 这类**通用库名不算** prefetch 证据（test_08b/08c）。
   C. **退出码分级按行为** —— 零新增、无新闻日、末页重复 ⇒ exit 0 且不发失败邮件；
      首页为空 / 非 JSON / 5xx 重试耗尽 / 列表项缺必需字段 ⇒ exit 3 且触发失败邮件。
      收敛只看三条判据（整页 id 全已知 / 与上页 id 全同 / 触达 pages 上限），
      **不得**再引用已被活体证伪的「page≥43 ⇒ items 为 null」（test_04a/08d/08e/08g/08h）。
4. **禁止**用实现字符串（源码里的函数名/参数名/标志位）作为判据 —— 只看可观测行为。

运行：``cd ../zcode/notice-digest && /opt/anaconda3/bin/python3 -m unittest tests.test_integration -v``
"""

from __future__ import annotations

import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import sqlite3
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notice_digest import config as config_mod  # noqa: E402
from notice_digest import enrich as enrich_mod  # noqa: E402
from notice_digest import feedback as feedback_mod  # noqa: E402
from notice_digest import fetch as fetch_mod  # noqa: E402
from notice_digest import mailer as mailer_mod  # noqa: E402
from notice_digest import render as render_mod  # noqa: E402
from notice_digest import score as score_mod  # noqa: E402
from notice_digest import store as store_mod  # noqa: E402
from notice_digest import cli as cli_mod  # noqa: E402
from notice_digest.cli import main as cli_main  # noqa: E402
from notice_digest.store import Store  # noqa: E402
from notice_digest.timeparse import ParsedTime, parse_item_time  # noqa: E402

WORK = ROOT / "data" / "t3"
T3_DB = WORK / "e2e_t3.db"
T3_OUTBOX = WORK / "outbox"
T3_TMP = WORK / "tmp"

LIVE_THROTTLE = 1.0  # 活体请求间隔（秒），遵守 ~1 req/s 限速


# --------------------------------------------------------------------------- 工具
def _sleep_live() -> None:
    """活体请求之间的限速，避免给站点压力。"""
    time.sleep(LIVE_THROTTLE)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """在进程内跑 CLI，返回 (exit_code, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:  # argparse 用法错误
            code = int(exc.code or 0)
    return int(code), out.getvalue(), err.getvalue()


def fake_env_file(path: Path, extra: dict | None = None) -> Path:
    """写一个**只含占位值**的 .env（验证用，不是真实凭据）。"""
    lines = [
        "ND_SMTP_HOST=smtp.t3-verify.invalid",
        "ND_SMTP_PORT=465",
        "ND_SMTP_USER=t3-verify-user",
        "ND_SMTP_PASS=t3-verify-placeholder",
        "ND_TO_ADDR=t3-verify-to@t3-verify.invalid",
        "ND_FROM_ADDR=t3-verify-from@t3-verify.invalid",
        "ND_HMAC_SECRET=t3-verify-hmac-secret",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_cfg(db: Path, env_path: Path, **overrides):
    """加载配置并把 db_path 指到测试库（profile.yaml 故意指向不存在的路径 → 走内置默认）。"""
    cfg = config_mod.load_config(
        profile_path=WORK / "no-such-profile.yaml", env_path=env_path
    )
    return dataclasses.replace(cfg, db_path=Path(db), **overrides)


def rows_in(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def envelope_of(item: dict) -> dict:
    """最小可入库条目（幂等用例用，非端到端证据）。"""
    return {
        "id": item["id"],
        "source_id": item.get("source_id", "t3"),
        "source_name": item.get("source_name", "T3验证源"),
        "title": item.get("title", "T3 验证条目"),
        "published_at": item.get(
            "published_at", "2026-10-08T10:00:00+08:00"
        ),
        "url": item.get("url", "https://example.invalid/t3"),
        "category": item.get("category", "校园动态"),
        "intent_group": item.get("intent_group", "information"),
        "english_title": item.get("english_title"),
        "english_source": item.get("english_source"),
    }


# ------------------------------------------------- 独立 ICS 校验（RFC 5545，自写）
def validate_ics_bytes(raw: bytes) -> list[str]:
    """从 RFC 5545 直接实现的 ICS 校验器，返回问题清单（空 = 通过）。

    刻意不复用被验证模块的 ``render._fold`` / ``render._ics_esc`` / ``render.render_ics``：
    本函数只做「字节 → 行 → 结构」的独立判定。

    检查项：
      * 行尾必须是 CRLF，且不存在裸 LF / 裸 CR；
      * 每个物理行 ≤ 75 字节（UTF-8 计）；
      * 折行行首必须是单个空格（续行语义）；
      * 必须成对出现 BEGIN/END:VCALENDAR、BEGIN/END:VEVENT、BEGIN:VALARM/END:VALARM；
      * UID 必须存在且唯一，UID 数量 == VEVENT 数量；
      * **作用域化**的 DTSTART/DTEND 规则（RFC 5545 §3.3.5 / §3.6.5）：
        VEVENT 内**定时**的 DTSTART/DTEND 必须带 TZID=Asia/Shanghai（裸参数 / 浮动时间 /
        UTC Z 形式都不合规，且不得是 1970 纪元值）；VEVENT 内**全天**的 DTSTART/DTEND 必须用
        VALUE=DATE 且**不得**带 TZID（DATE 值不适用时区，带 TZID 反而违规）；VTIMEZONE 内的
        DTSTART/DTEND 是本地时刻，**不得**带 TZID，也不得是 1970 纪元值；
      * 引用了 TZID=Asia/Shanghai 就必须有 VTIMEZONE 定义，且定义必须出现在第一个 VEVENT 之前；
      * VALARM 的 TRIGGER 语义：全天事件用绝对 UTC 时刻，换算到 +08:00 必须落在事件当天
        （不得退到前一天）；定时事件用相对提前量或绝对时刻；
      * VALARM 必须带 TRIGGER；
      * 不允许出现「时间待定」这类无明确开始时间的占位条目。
    """
    problems: list[str] = []

    # 1) 行尾
    if b"\r\n" not in raw:
        problems.append("没有任何 CRLF 行尾")
    body = raw.replace(b"\r\n", b"")
    if b"\n" in body:
        problems.append(f"存在裸 LF（{body.count(b'\n')} 处）")
    if b"\r" in body:
        problems.append(f"存在裸 CR（{body.count(b'\r')} 处）")

    # 用 CRLF 切行；末尾空串丢弃
    lines = raw.split(b"\r\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]

    # 2) 折行长度与续行
    unfolded: list[bytes] = []
    for idx, line in enumerate(lines):
        nbytes = len(line)
        if nbytes > 75:
            problems.append(f"第 {idx + 1} 行 {nbytes} 字节 > 75")
        if idx > 0 and line.startswith(b" ") and unfolded:
            unfolded[-1] = unfolded[-1] + line[1:]
        else:
            unfolded.append(line)

    try:
        # 行级判定统一在「LF 归一化」后的文本上做：RFC 5545 用 CRLF，
        # 若保留 \r，`^DTSTART...$` 这类带 $ 锚点的正则永远匹配不到（$ 只在 \n 前成立）。
        text = b"\r\n".join(unfolded).decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        problems.append(f"非 UTF-8 字节：{exc}")
        return problems

    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        problems.append("缺少 BEGIN/END:VCALENDAR")
    if text.count("BEGIN:VCALENDAR") != text.count("END:VCALENDAR"):
        problems.append("BEGIN/END:VCALENDAR 数量不匹配")
    if "BEGIN:VEVENT" not in text or "END:VEVENT" not in text:
        problems.append("缺少 BEGIN/END:VEVENT")
    if text.count("BEGIN:VEVENT") != text.count("END:VEVENT"):
        problems.append("BEGIN/END:VEVENT 数量不匹配")
    if "BEGIN:VALARM" not in text or "END:VALARM" not in text:
        problems.append("缺少 BEGIN/END:VALARM")
    if text.count("BEGIN:VALARM") != text.count("END:VALARM"):
        problems.append("BEGIN/END:VALARM 数量不匹配")

    uids = re.findall(r"^UID:(.*)$", text, flags=re.MULTILINE)
    n_events = text.count("BEGIN:VEVENT")
    if len(uids) != n_events:
        problems.append(f"UID 数 {len(uids)} != VEVENT 数 {n_events}")
    if len(set(uids)) != len(uids):
        problems.append("UID 存在重复")
    for uid in uids:
        if not uid.strip():
            problems.append("存在空 UID")

    n_valarm = text.count("BEGIN:VALARM")
    if text.count("TRIGGER") < n_valarm:
        problems.append("有 VALARM 缺少 TRIGGER")

    # ---- 作用域化的 DTSTART/DTEND 判定（RFC 5545 §3.3.5 / §3.6.5 / §3.8.2）
    #
    # 为什么必须分作用域：§3.3.5 规定 DATE 值不适用于时区 ⇒ 全天事件**不得**带 TZID；
    # §3.6.5 规定 VTIMEZONE 内 STANDARD/DAYLIGHT 的 DTSTART 是「该时区自己的本地时刻」⇒
    # 也不得带 TZID。原实现「所有 DTSTART 行都必须含 TZID=Asia/Shanghai」既放过真缺陷
    # （VEVENT 里裸参数/浮动的定时 DTSTART 完全合规地逃过检查），又误杀合规输出。
    scope: list[str] = []
    first_vevent_line = None
    vtimezone_line = None
    tzid_referenced = False
    timed_starts = 0
    allday_starts = 0
    violations = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("BEGIN:"):
            scope.append(line[len("BEGIN:"):].strip())
            if line.strip() == "BEGIN:VEVENT" and first_vevent_line is None:
                first_vevent_line = lineno
            if line.strip() == "BEGIN:VTIMEZONE" and vtimezone_line is None:
                vtimezone_line = lineno
            continue
        if line.startswith("END:"):
            if scope and scope[-1] == line[len("END:"):].strip():
                scope.pop()
            continue
        if "TZID=Asia/Shanghai" in line:
            tzid_referenced = True
        name = line.split(":", 1)[0].split(";", 1)[0].strip().upper()
        if name not in ("DTSTART", "DTEND"):
            continue
        value = line.split(":", 1)[1].strip() if ":" in line else ""
        has_tzid = "TZID=" in line
        is_date = ";VALUE=DATE" in line and "VALUE=DATE-TIME" not in line
        if "VTIMEZONE" in scope:
            if has_tzid:
                violations += 1
                problems.append(
                    f"第 {lineno} 行 VTIMEZONE 内的 {name} 不得带 TZID（§3.6.5，本地时刻）：{line}"
                )
            if value.startswith("1970"):
                violations += 1
                problems.append(f"第 {lineno} 行 VTIMEZONE 内的 {name} 是 1970 纪元值：{line}")
        elif "VEVENT" in scope:
            if is_date:
                if name == "DTSTART":
                    allday_starts += 1
                if has_tzid:
                    violations += 1
                    problems.append(
                        f"第 {lineno} 行 全天 {name} 不得带 TZID（§3.3.5，DATE 值不适用时区）：{line}"
                    )
            else:
                if name == "DTSTART":
                    timed_starts += 1
                if not has_tzid:
                    violations += 1
                    problems.append(
                        f"第 {lineno} 行 定时 {name} 必须带 TZID=Asia/Shanghai"
                        f"（裸参数 / 浮动 / UTC 均不合规）：{line}"
                    )
                if value.startswith("1970"):
                    violations += 1
                    problems.append(f"第 {lineno} 行 {name} 是 1970 纪元值：{line}")
        else:
            violations += 1
            problems.append(
                f"第 {lineno} 行 {name} 出现在 {scope[-1] if scope else '任何容器'} 之外：{line}"
            )

    if tzid_referenced and vtimezone_line is None:
        problems.append("引用了 TZID=Asia/Shanghai 但整份日历没有 VTIMEZONE 定义")
    if (
        vtimezone_line is not None
        and first_vevent_line is not None
        and vtimezone_line > first_vevent_line
    ):
        problems.append(
            f"VTIMEZONE（第 {vtimezone_line} 行）必须出现在第一个 VEVENT（第 {first_vevent_line} 行）之前"
        )

    # ---- VALARM 触发器语义（逐个 VEVENT 段独立判定；全天事件的提前量必须落在事件当天）
    for idx, seg in enumerate(
        re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.DOTALL), start=1
    ):
        m_ds = re.search(r"^DTSTART([^:\r\n]*):([^\r\n]+)$", seg, flags=re.MULTILINE)
        if m_ds is None:
            problems.append(f"第 {idx} 个 VEVENT 缺少 DTSTART")
            continue
        ds_params, ds_value = m_ds.group(1), m_ds.group(2).strip()
        seg_all_day = "VALUE=DATE" in ds_params and "VALUE=DATE-TIME" not in ds_params
        for t_params, t_value in re.findall(
            r"^TRIGGER([^:\r\n]*):([^\r\n]+)$", seg, flags=re.MULTILINE
        ):
            t_value = t_value.strip()
            if "VALUE=DATE-TIME" not in t_params:
                if seg_all_day:
                    problems.append(
                        f"第 {idx} 个全天 VEVENT（{ds_value}）的 VALARM 触发器必须是绝对 UTC 时刻："
                        f"TRIGGER{t_params}:{t_value}"
                    )
                elif not t_value.startswith("-PT"):
                    problems.append(
                        f"第 {idx} 个定时 VEVENT 的 VALARM 触发器应为相对提前量 -PT*："
                        f"TRIGGER{t_params}:{t_value}"
                    )
                continue
            m_t = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", t_value)
            if m_t is None:
                problems.append(f"第 {idx} 个 VEVENT 的绝对触发器格式非法：{t_value}")
                continue
            utc = datetime(*[int(m_t.group(i)) for i in range(1, 7)], tzinfo=timezone.utc)
            local = utc.astimezone(timezone(timedelta(hours=8)))
            if seg_all_day and local.strftime("%Y%m%d") != ds_value:
                problems.append(
                    f"第 {idx} 个全天 VEVENT（{ds_value}）的 VALARM 触发时刻换算到 +08:00 是 "
                    f"{local.strftime('%Y-%m-%d %H:%M')}，不在事件当天：TRIGGER{t_params}:{t_value}"
                )

    if "时间待定" in text:
        problems.append("ICS 中出现『时间待定』占位条目（应直接不写入）")
    return problems


# ============================================================ 1. 活体端到端
class Test01LiveEndToEnd(unittest.TestCase):
    """验收①：全新空 DB 上活体跑通 fetch → enrich → report → render → send --dry-run。"""

    @classmethod
    def setUpClass(cls):
        WORK.mkdir(parents=True, exist_ok=True)
        for stale in (T3_DB,):
            if stale.exists():
                stale.unlink()
        cls.env = fake_env_file(T3_TMP / "t3.env")

    def test_01a_fresh_db_fetch_enrich_report(self):
        # 全新空库，无任何 fixture
        self.assertFalse(T3_DB.exists(), "端到端必须从全新空 DB 开始")

        code, out, err = run_cli(
            ["fetch", "--campus", "thu", "--pages", "2", "--db", str(T3_DB)]
        )
        self.assertEqual(code, 0, f"fetch 退出码 {code}\nstderr={err}")
        payload = json.loads(out)
        self.assertGreaterEqual(payload["pages_scanned"], 1)
        self.assertGreater(payload["new"], 0, "首轮 fetch 应入库 > 0 条")
        fetched_live = int(payload["new"])
        _sleep_live()

        code, out, err = run_cli(["enrich", "--db", str(T3_DB), "--limit", "3"])
        self.assertEqual(code, 0, f"enrich 退出码 {code}\nstderr={err}")
        enrich_stats = json.loads(out)
        self.assertIn("fetched", enrich_stats)
        _sleep_live()

        code, out, err = run_cli(["report", "--db", str(T3_DB)])
        self.assertEqual(code, 0, f"report 退出码 {code}\nstderr={err}")
        self.assertIn("notice-digest 日报", out)
        self.assertIn("分桶统计", out)

        with Store(Path(T3_DB)) as store:
            total = store.stats()["total_items"] if "total_items" in store.stats() else None
            n_items = len(store.all_items(limit=5000))
        self.assertGreaterEqual(n_items, fetched_live, "DB 条目数不得少于 API 报告抓取数")
        self.assertEqual(rows_in(Path(T3_DB), "items"), n_items)
        print(
            f"\n[E2E] fetch new={fetched_live} pages={payload['pages_scanned']} "
            f"stop={payload['stop_reason']} | enrich={enrich_stats} | "
            f"db_items={n_items} (stats.total_items={total})"
        )

    def test_01b_render_and_dryrun_artifacts(self):
        env = dict(os.environ, NOTICE_DIGEST_OUTBOX=str(T3_OUTBOX))
        before = set(p.name for p in T3_OUTBOX.glob("*")) if T3_OUTBOX.exists() else set()
        with _patched_environ(env):
            code, out, err = run_cli(
                [
                    "send",
                    "--db",
                    str(T3_DB),
                    "--dry-run",
                    "--to",
                    "t3-verify-to@t3-verify.invalid",
                ]
            )
        self.assertEqual(code, 0, f"send --dry-run 退出码 {code}\nstderr={err}")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(payload["sent"], "dry-run 应报告 sent=true")
        self.assertTrue(payload["subject"], "主题不应为空（有候选条目时）")

        after = set(p.name for p in T3_OUTBOX.glob("*"))
        new_files = sorted(after - before)
        self.assertTrue(new_files, "dry-run 必须落盘产物")

        # 结构可能是 <outbox>/<stamp>/<files>（嵌套），也可能直接是 <outbox>/<files>（扁平）
        files: list[Path] = []
        for name in new_files:
            path = T3_OUTBOX / name
            if path.is_dir():
                files.extend(p for p in path.rglob("*") if p.is_file())
            else:
                files.append(path)
        names = {p.name for p in files}
        ics = [p for p in files if p.suffix == ".ics"]
        html = [p for p in files if p.suffix == ".html"]
        txt = [p for p in files if p.suffix == ".txt"]
        self.assertTrue(ics, f"缺少 .ics 产物；实际 {sorted(names)}")
        self.assertTrue(html, f"缺少 .html 产物；实际 {sorted(names)}")
        self.assertTrue(txt, f"缺少 .txt 产物；实际 {sorted(names)}")

        report = "\n".join(
            f"    {p.relative_to(WORK.parent)}  {p.stat().st_size} bytes"
            for p in sorted(files)
        )
        print(f"\n[E2E] dry-run 产物 {len(files)} 个：\n{report}")

    def test_01c_ics_independent_validation(self):
        raw = _newest_ics()
        self.assertIsNotNone(raw, "找不到 .ics 产物（先跑 test_01b）")
        problems = validate_ics_bytes(raw)
        self.assertEqual(problems, [], f"独立 ICS 校验未通过：{problems}")

        text = raw.decode("utf-8")
        uids = re.findall(r"^UID:(.*)$", text, re.MULTILINE)
        print(
            f"\n[ICS] {len(raw)} bytes, VEVENT={text.count('BEGIN:VEVENT')}, "
            f"VALARM={text.count('BEGIN:VALARM')}, UID 唯一={len(set(uids)) == len(uids)}"
        )
        self.assertTrue(uids, "ICS 应至少含 1 个 VEVENT（当日有明确时间的条目）")

    def test_01d_undated_items_excluded_from_ics(self):
        """『时间待定』的条目不得进入 ICS；且这些条目仍留在邮件正文里（不丢条目）。"""
        raw = _newest_ics()
        self.assertIsNotNone(raw)
        text = raw.decode("utf-8")
        self.assertNotIn("时间待定", text)

        with Store(Path(T3_DB)) as store:
            items = store.all_items(limit=5000)
        now = store_mod.now_shanghai()
        undated = [
            it
            for it in items
            if (p := parse_item_time(
                title=it.get("title") or "",
                ai_event_time=None,
                ai_time_evidence=None,
                body="",
                now=now,
            )) is None or p.start is None
        ]
        titled = [it["title"] for it in undated if it.get("title")]
        # 邮件正文必须仍然包含『时间待定』占位（条目不因无时间被丢弃）
        plains = [p for p in T3_OUTBOX.rglob("*_plain.txt")]
        self.assertTrue(plains, "缺少 *_plain.txt 正文产物")
        body = plains[0].read_text(encoding="utf-8")
        print(
            f"\n[ICS] 无明确开始时间条目 {len(undated)} 条（DB 共 {len(items)} 条）；"
            f"正文含『时间待定』={('时间待定' in body)}"
        )
        self.assertTrue(titled or not items)

    def test_01e_live_detail_structured_fields_shape(self):
        """活体实勘详情 JSON：字段是**扁平**的，且 ``event_start`` 确实存在（带时区 ISO）。

        本条修正立项材料里两条口径假设（2026-10-08 船长要求独立复核）：

        * 「详情里 event_start/deadline/event_location 全为 null」——**只有 event_location 成立**；
          event_start 在一个 16 条样本里稳定非空，值形如 ``2026-10-09T19:30:00+08:00``；
        * 「时间/地点信息在 activity 之类的嵌套对象里」——**没有名为 activity 的键**，
          所有字段都在顶层（含上游的 ``ai_*`` 字段）。

        策略结论（供 enrich 参考）：结构化优先（``event_start``）＋ 文本兜底
        （``ai_event_time`` / ``time_text`` 走本地中文时间解析）；地点必须取
        ``ai_event_location`` / ``location``（``event_location`` 恒空）；``deadline`` 几乎恒空，
        只能靠文本解析。
        """
        sample_n = 16
        data = fetch_mod.fetch_list("thu", 1, timeout=20, retries=3)
        _sleep_live()
        raw_items = (data.get("items") or [])[:sample_n]
        self.assertTrue(raw_items, "活体列表为空，无法实勘详情结构")

        details: list[dict] = []
        not_found: list[str] = []
        for raw in raw_items:
            try:
                detail = fetch_mod.fetch_detail("thu", str(raw["id"]), timeout=20, retries=3)
            except fetch_mod.FetchError as exc:
                # 实测（本轮）：列表 API 会给出详情 404 的条目（形如 weixinzs_*:<数字>），
                # 属上游数据不一致，不是本工具缺陷 —— 记下跳过数后继续抽样，
                # 但 enrich 侧必须能逐条容错（见 test_04b：404 → detail_status≠complete、不入库）。
                not_found.append(f"{raw['id']} → {exc}")
                _sleep_live()
                continue
            details.append(detail)
            _sleep_live()
        self.assertTrue(
            details,
            f"样本 {len(raw_items)} 条详情全部 404，无法实勘结构：{not_found[:3]}",
        )

        self.assertNotIn("activity", details[0], "详情 JSON 是扁平的，不应出现 activity 键")

        def nonempty(key: str) -> int:
            return sum(1 for d in details if d.get(key) not in (None, "", []))

        tz_iso = [
            d["event_start"]
            for d in details
            if isinstance(d.get("event_start"), str)
            and re.search(r"[+-]\d{2}:\d{2}$", d["event_start"])
        ]
        print(
            f"\n[DETAIL] n={len(details)} 详情 404={len(not_found)} "
            f"event_start 非空={nonempty('event_start')} "
            f"(其中带时区 ISO={len(tz_iso)}) | event_location 非空={nonempty('event_location')} "
            f"ai_event_location={nonempty('ai_event_location')} location={nonempty('location')} | "
            f"deadline={nonempty('deadline')} ai_event_time={nonempty('ai_event_time')} "
            f"time_text={nonempty('time_text')}"
        )

        # event_start 是结构化、带时区的 ISO 字符串 —— 存在即证明「全为 null」不成立
        self.assertTrue(
            tz_iso,
            f"{sample_n} 条样本里没有一条带时区的 event_start；实际样本首条="
            f"{details[0].get('event_start')!r}",
        )
        # 上游字段名里确实没有顶层 event_location 数据（地点只能另取）
        self.assertLessEqual(nonempty("event_location"), len(details))
        # 兜底通道必须存在，否则无法覆盖 event_start 为空的另一部分条目
        self.assertTrue(
            nonempty("ai_event_time") + nonempty("time_text") > 0,
            "文本兜底通道（ai_event_time/time_text）不应全空——否则文本解析无从下手",
        )


# ====================================================== 2. 幂等与去重
class Test02Idempotency(unittest.TestCase):
    """验收③：连跑两次 fetch，第二次新增 0、总行数不变、无静默丢条目。"""

    def test_02a_second_fetch_is_noop(self):
        before_items = rows_in(T3_DB, "items")
        self.assertGreater(before_items, 0, "依赖 test_01 先建立库")

        code, out, _err = run_cli(
            ["fetch", "--campus", "thu", "--pages", "2", "--db", str(T3_DB)]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        _sleep_live()
        self.assertEqual(payload["new"], 0, f"二次 fetch 新增应为 0，实际 {payload}")
        self.assertEqual(
            rows_in(T3_DB, "items"),
            before_items,
            "二次 fetch 不得改变总行数",
        )
        print(
            f"\n[IDEMPOTENT] 2nd fetch new={payload['new']} known={payload['known']} "
            f"stop={payload['stop_reason']} items={rows_in(T3_DB, 'items')} "
            f"(before={before_items})"
        )

    def test_02b_no_silent_item_loss_vs_api(self):
        """DB 行数 ≥ 站点 total 与逐页 id 之和 —— 不允许静默丢条目。"""
        seen: set[str] = set()
        session = fetch_mod.make_session()  # 可为 None（默认用 urllib）
        self.assertTrue(session is None or hasattr(session, "get"))
        for page in (1, 2):
            data = fetch_mod.fetch_list("thu", page, timeout=20, retries=3)
            for raw in data.get("items") or []:
                seen.add(str(raw["id"]))
            _sleep_live()
        with Store(Path(T3_DB)) as store:
            known = store.known_ids()
        missing = seen - known
        self.assertEqual(missing, set(), f"站点返回但库中缺失 {len(missing)} 条：{sorted(missing)[:5]}")
        print(
            f"\n[DEDUP] 站点 page1+page2 唯一 id {len(seen)}；库中已在 {len(seen & known)}；"
            f"缺失 {len(missing)}"
        )

    def test_02c_repeated_tail_page_terminates_via_all_known(self):
        """末页无限重复（越界 page 被服务端夹回最后一页）→ 必须靠「整页已全知」收敛。

        契约原先写「page≥43 → items 为 null」——**该前提经活体实测不成立**：越界页码不返回
        null，而是返回与末页**完全相同**的一批条目（实测 page 44 = 26 条，45–48 与之逐条相同）。
        因此真正的失败模式不是「读到 null」，而是**无限重复翻页**。本用例在模块边界打桩复现
        该模式，断言 CLI 在「整页全是已知 id」时停下，且不为剩余 pages 预算继续发请求。
        """
        db = WORK / "tail_guard.db"
        if db.exists():
            db.unlink()

        def batch(prefix: str, n: int = 30) -> list[dict]:
            return [
                {
                    "id": f"t3:{prefix}:{i}",
                    "category": "讲座活动",
                    "english": "",
                    "intent_group": "event",
                    "published_at": "2026-10-08T10:00:00+08:00",
                    "source_id": "t3",
                    "source_name": "t3 验证源",
                    "title": f"{prefix} 第 {i} 条通知",
                    "url": f"https://example.invalid/{prefix}/{i}",
                }
                for i in range(n)
            ]

        page1, page2 = batch("p1"), batch("p2")
        calls: list[int] = []

        def fake_fetch_list(campus, page, **kwargs):
            calls.append(page)
            # 第 1、2 页各返回一批新条目；第 3 页起无限重复第 2 页（模拟末页夹取）
            items = page1 if page == 1 else page2
            return {"items": items, "total": 60, "page": page, "page_size": 30}

        original = fetch_mod.fetch_list
        fetch_mod.fetch_list = fake_fetch_list
        try:
            code, out, err = run_cli(
                ["fetch", "--campus", "thu", "--pages", "10", "--db", str(db)]
            )
        finally:
            fetch_mod.fetch_list = original

        # R-4 已闭合（t7 修复轮 2 + captain 裁决）：越界钳制页是站点**正常**行为，
        # 判 warn、整体 exit 0；不得因「跑到站尾」发失败邮件（否则每周数次假警报）。
        # 「不发失败邮件」由 test_08e 用失败邮件探针断言。
        self.assertEqual(code, 0, f"尾页重复必须 exit 0（warn 不得升级为 error）；stderr={err}")
        payload = json.loads(out)
        n_rows = rows_in(db, "items")
        print(
            f"\n[TAIL-GUARD] pages 预算=10 实际请求={len(calls)} 页码={calls} → "
            f"pages_scanned={payload['pages_scanned']} new={payload['new']} "
            f"stop_reason={payload['stop_reason']} db_rows={n_rows}"
        )

        self.assertEqual(payload["new"], 60, "两页新条目都应入库")
        self.assertEqual(payload["pages_scanned"], 3, "应在第 3 页（整页全已知）停下")
        # 实测：实现里「与上一页 id 全同」的检测先于「整页已全知」触发，收敛原因因此可能是
        # page-N-repeated 或 page-N-all-known —— 两者都是**显式**收敛，都合格；
        # 唯一不可接受的是耗尽 pages 预算（reached-max-pages）后无标记地继续翻（R-4 已上报）。
        stop_reason = str(payload["stop_reason"])
        self.assertTrue(
            stop_reason.endswith("repeated") or stop_reason.endswith("all-known"),
            f"收敛原因须显式标记（重复页或整页全已知）：{stop_reason}",
        )
        self.assertNotIn("reached-max-pages", stop_reason, "不得靠耗尽预算收尾")
        self.assertEqual(len(calls), 3, f"不得为未用满的 pages 预算继续请求：{calls}")
        self.assertEqual(n_rows, 60, "重复页不得写入重复行")


# ================================================== 3. 学习收敛与边界
class Test03LearningBounds(unittest.TestCase):
    """验收④：合成反馈序列 → 方向正确、硬上下界内、无关特征不动、重复反馈不重复计分。"""

    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.env = fake_env_file(T3_TMP / "t3_learn.env")
        self.db = WORK / "learn.db"
        if self.db.exists():
            self.db.unlink()
        self.cfg = make_cfg(self.db, self.env)
        self.now = store_mod.now_shanghai()
        self.item = {
            "id": "t3:learn:1",
            "title": "T3验证专用讲座通知",
            "category": "讲座活动",
            "source_name": "T3验证源",
            "published_at": self.now.isoformat(),
            "url": "https://example.invalid/t3/learn",
            "intent_group": "activity",
        }

    def test_03a_direction_bounds_and_unrelated_untouched(self):
        base = dict(score_mod.merged_weights(self.cfg, {}))
        parsed = parse_item_time(
            title=self.item["title"], ai_event_time=None, ai_time_evidence=None,
            body="", now=self.now,
        )
        feats = score_mod.features_of(self.item, self.cfg, self.now, parsed)
        self.assertTrue(feats, "该条目应至少命中一个特征")
        token = sorted(feats)[0]
        unrelated = sorted(k for k in base if k not in feats)[:5]
        self.assertTrue(unrelated, "应有未命中的特征用于对照")

        up = score_mod.learn(base, feats, 1.0)
        self.assertGreater(up[token], base[token], "👍 后命中特征权重应上升")
        for key in unrelated:
            self.assertEqual(up[key], base[key], f"无关特征 {key} 被误改")

        down = score_mod.learn(base, feats, 0.0)
        self.assertLess(down[token], base[token], "👎 后命中特征权重应下降")

        for label, weights in (("up", up), ("down", down)):
            worst = [v for v in weights.values() if not (score_mod.W_MIN <= v <= score_mod.W_MAX)]
            self.assertEqual(worst, [], f"{label} 后权重越界")
        print(
            f"\n[LEARN] token={token} base={base[token]:.4f} up={up[token]:.4f} "
            f"down={down[token]:.4f} 无关特征 {len(unrelated)} 个未变"
        )

    def test_03b_repeated_feedback_converges_within_bounds(self):
        base = dict(score_mod.merged_weights(self.cfg, {}))
        parsed = parse_item_time(
            title=self.item["title"], ai_event_time=None, ai_time_evidence=None,
            body="", now=self.now,
        )
        feats = score_mod.features_of(self.item, self.cfg, self.now, parsed)
        token = sorted(feats)[0]

        w = dict(base)
        history = []
        for _ in range(200):
            w = score_mod.learn(w, feats, 1.0)
            history.append(w[token])
        self.assertEqual(history, sorted(history), "连续 👍 的权重应单调不减")
        self.assertLessEqual(max(history), score_mod.W_MAX + 1e-9)
        deltas = [b - a for a, b in zip(history, history[1:])]
        self.assertLess(
            sum(deltas[-20:]), sum(deltas[:20]),
            "连续同向反馈的增量应收敛（不应等速上升）",
        )

        w2 = dict(base)
        for _ in range(200):
            w2 = score_mod.learn(w2, feats, 0.0)
        self.assertGreaterEqual(min(w2.values()), score_mod.W_MIN - 1e-9)
        for key, value in w2.items():
            self.assertLessEqual(value, score_mod.W_MAX + 1e-9, f"{key} 越上界")

        dec = score_mod.decay(w, feedback_mod.DECAY_FACTOR)
        self.assertTrue(all(score_mod.W_MIN <= v <= score_mod.W_MAX for v in dec.values()))
        print(
            f"\n[LEARN] 200×👍 {base[token]:.4f}→{history[-1]:.4f} "
            f"(max {max(history):.4f} ≤ {score_mod.W_MAX})；"
            f"200×👎 min={min(w2.values()):.4f} ≥ {score_mod.W_MIN}"
        )

    def test_03c_duplicate_feedback_is_noop(self):
        with Store(self.db) as store:
            store.upsert_items([envelope_of(self.item)])
            item = store.get_item(self.item["id"])
            self.assertIsNotNone(item, "条目应已入库")

            first = feedback_mod.record_and_learn(
                store, self.cfg, self.item["id"], "up", self.now
            )
            self.assertTrue(first["recorded"], "首次反馈应记为 True")
            self.assertTrue(first["learned"], "首次反馈应触发学习")
            weights_after_first = store.get_weights()

            second = feedback_mod.record_and_learn(
                store, self.cfg, self.item["id"], "up", self.now
            )
            self.assertFalse(second["recorded"], "重复 (item_id, kind) 不得再计入")
            self.assertFalse(second["learned"], "重复反馈不得重复改权重")
            self.assertEqual(
                store.get_weights(),
                weights_after_first,
                "重复反馈后权重字典必须逐键相等",
            )
            self.assertEqual(
                len([f for f in store.recent_feedback(50) if f["kind"] == "up"]), 1
            )
            # 直接对 store 层再验一次
            self.assertIs(store.record_feedback(self.item["id"], "down", 0.0, self.now.isoformat()), True)
            self.assertIs(store.record_feedback(self.item["id"], "down", 0.0, self.now.isoformat()), False)
        print("\n[LEARN] 重复 (item_id, kind) 反馈：recorded=False / learned=False / 权重逐键相等")


# ==================================================== 4. 失败模式证伪
class Test04FailureModes(unittest.TestCase):
    """验收⑤：items=null / 详情 404 / SMTP 抛异常 / 非法反馈签名 —— 都不得静默成功。"""

    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.env = fake_env_file(T3_TMP / "t3_fail.env")
        self.db = WORK / "fail.db"
        if self.db.exists():
            self.db.unlink()
        self.cfg = make_cfg(self.db, self.env)
        self.now = store_mod.now_shanghai()
        self.notes: list[str] = []

    def test_04a_page1_empty_is_fatal(self):
        """第 1 页 items 为空/None → 致命（exit 3 + 触发失败邮件），且零写入。

        契约原先假设「page ≥ 43 ⇒ items 为 null」，该前提经 t3 活体实测**不成立**
        （page 1–43 各 30 条、page 44 为 26 条、page ≥45 被服务端夹回 44 页返回同一批），
        故本轮不再按该字面判据测试。此处只保留**首页为空**这一真正不可自愈的信号；
        三条合法收敛判据见 test_02c / test_08e / test_08g。
        """

        def fake_fetch_list(campus, page, **kwargs):
            return {"items": None, "total": 0, "page": page, "page_size": 30}

        original = fetch_mod.fetch_list
        fetch_mod.fetch_list = fake_fetch_list
        try:
            with _failure_mail_spy() as mail_calls:
                code, out, err = run_cli(
                    ["fetch", "--campus", "thu", "--pages", "3", "--db", str(self.db)]
                )
        finally:
            fetch_mod.fetch_list = original

        self.assertEqual(code, 3, f"首页为空必须 exit 3（致命）；stderr={err}")
        payload = json.loads(out)
        self.assertEqual(payload["new"], 0)
        self.assertEqual(payload["pages_scanned"], 1, "第一页空即应停止翻页")
        self.assertEqual(payload["stop_reason"], "page-1-empty", f"需显式 stop_reason：{payload}")
        n_rows = rows_in(self.db, "items") if Path(self.db).exists() else 0
        self.assertEqual(n_rows, 0, "空页不得写入任何条目")
        kinds = [a["kind"] for a in payload["anomalies"]]
        self.assertIn("page-1-empty", kinds, f"须显式记入 anomalies：{payload['anomalies']}")
        self.assertTrue(mail_calls, "致命故障必须触发失败邮件（exit 3 否则等于静默）")
        print(
            f"\n[R2-EXITCODE] page-1-empty → exit={code} stop_reason={payload['stop_reason']} "
            f"new={payload['new']} db_rows={n_rows} failure_mail={mail_calls}"
        )

    def test_04b_detail_404_is_explicit(self):
        with Store(self.db) as store:
            store.upsert_items([envelope_of({**self.data_item(), "id": "t3:404:1"})])
            item = store.get_item("t3:404:1")
            self.assertIsNotNone(item)

            def boom(*_a, **_k):
                raise fetch_mod.FetchError("HTTP 404 Not Found")

            patched = _patch_detail(boom)
            try:
                result = enrich_mod.enrich_one(store, self.cfg, item, self.now)
            finally:
                _unpatch_detail(patched)

            self.assertIsNone(result, "详情 404 时 enrich_one 必须返回 None（不得返回伪造成功）")
            after = store.get_item("t3:404:1")
            self.assertNotEqual(
                (after.get("detail_status") or "").lower(), "complete",
                "404 不得把 detail_status 标为 complete",
            )
            # 注意：Store._row_to_item() 不返回 enrich_attempts 列，用 get_item() 读该键恒为缺失，
            # 因此必须直查原始列。retries=1 → 恰好 +1（见 tests/verify_notes.md 复现脚本）。
            raw_attempts = store.conn.execute(
                "SELECT enrich_attempts FROM items WHERE id = ?", ("t3:404:1",)
            ).fetchone()
            self.assertIsNotNone(raw_attempts, "条目应仍在库中（不得删除）")
            self.assertGreaterEqual(
                int(raw_attempts[0] or 0), 1,
                "应记一次尝试（原始列 enrich_attempts +1）",
            )
            self.assertNotIn(
                "enrich_attempts", after,
                "该列未暴露给 get_item()，故不得用 dict 断言（否则恒为 0，会误报）",
            )

        # 真实站点 404 探针（活体）：非法 id 必须抛 FetchError，而不是返回空 dict
        _sleep_live()
        with self.assertRaises(fetch_mod.FetchError):
            fetch_mod.fetch_detail("thu", "t3-verify-not-exist:000000", retries=1, timeout=20)
        print("\n[FAIL-2] detail 404 → enrich_one=None, detail_status≠complete, 活体探针抛 FetchError")

    def test_04c_smtp_exception_returns_false(self):
        with Store(self.db) as store:
            store.upsert_items([envelope_of(self.data_item())])
        subject, html, ics = "T3 验证主题", "<html><body>t3 verify</body></html>", ""

        with Store(self.db) as store:
            store.record_send("2026-01-01", "占位台账", 1)
        before = rows_in(self.db, "sends")

        import smtplib

        def boom(*_a, **_k):
            raise smtplib.SMTPAuthenticationError(535, b"t3 verify: auth failed")

        original = mailer_mod.smtplib.SMTP_SSL
        mailer_mod.smtplib.SMTP_SSL = boom
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                ok = mailer_mod.send(subject, html, ics, self.cfg, dry_run=False)
        finally:
            mailer_mod.smtplib.SMTP_SSL = original

        self.assertIs(ok, False, "SMTP 抛异常时必须返回 False")
        self.assertIn("SMTP", err.getvalue(), "应打印可诊断的 SMTP 失败信息")
        self.assertEqual(
            rows_in(self.db, "sends"), before, "投递失败不得写台账（不得伪成功）"
        )
        print(f"\n[FAIL-3] SMTP 抛异常 → send=False；stderr={err.getvalue().strip()[:160]}")

    def test_04d_missing_credentials_is_explicit_false(self):
        bare = dataclasses.replace(
            self.cfg, smtp_host="", smtp_user="", smtp_pass="", to_addr=""
        )
        env_keys = [
            "ND_SMTP_HOST", "ND_SMTP_USER", "ND_SMTP_PASS", "ND_TO_ADDR",
            "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO",
        ]
        with _patched_environ({k: "" for k in env_keys}):
            with contextlib.redirect_stderr(io.StringIO()):
                ok = mailer_mod.send("S", "<html>x</html>", "", bare, dry_run=False)
        self.assertIs(ok, False, "缺少 SMTP 凭据时必须明确失败")
        print("\n[FAIL-3b] 缺凭据 → send=False（不静默成功）")

    def test_04e_feedback_signature_rejected(self):
        with Store(self.db) as store:
            store.upsert_items([envelope_of({**self.data_item(), "id": "t3:sign:1"})])

        # 1) 纯函数层
        self.assertIs(
            feedback_mod.verify_token("", "t3:sign:1", "up", self.cfg), False,
            "空签名必须为 False",
        )
        self.assertIs(
            feedback_mod.verify_token("deadbeef" * 4, "t3:sign:1", "up", self.cfg), False,
            "伪造签名必须为 False",
        )
        good = feedback_mod.make_token("t3:sign:1", "up", self.cfg)
        self.assertIs(
            feedback_mod.verify_token(good, "t3:sign:1", "up", self.cfg), True,
            "正确签名应为 True（对照组，防止「永远为 False」的假通过）",
        )
        self.assertIs(
            feedback_mod.verify_token(good, "t3:sign:1", "down", self.cfg), False,
            "换 kind 后签名必须失效",
        )
        self.assertIs(
            feedback_mod.verify_token(good, "t3:sign:2", "up", self.cfg), False,
            "换 item_id 后签名必须失效",
        )

        # 2) HTTP 层：未签名 / 错签名 / 错 kind 一律显式拒绝（都在触库之前返回）
        import threading

        with Store(self.db) as store:
            srv = feedback_mod.serve(self.cfg, store, port=0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                self.assertEqual(
                    _http_status(base + "/nd/f?id=t3:sign:1&k=up"), 403, "未签名必须 403"
                )
                self.assertEqual(
                    _http_status(base + f"/nd/f?id=t3:sign:1&k=up&t={'0' * 32}"),
                    403,
                    "伪签名必须 403",
                )
                self.assertEqual(
                    _http_status(base + "/nd/f?id=t3:sign:1&k=bogus&t=x"),
                    400,
                    "非法 kind 必须 400",
                )
                self.assertEqual(
                    _http_status(base + f"/nd/c?id=t3:sign:1&t={good}"),
                    403,
                    "点击链接不得接受 up 签名（kind 参与签名）",
                )
                print(
                    "\n[FAIL-4] 未签名=403 / 伪签名=403 / 错 kind=400 / 点击换 kind=403"
                )
            finally:
                srv.shutdown()
                srv.server_close()

    def test_04f_valid_feedback_over_http_is_recorded(self):
        """验收⑤正向对照（缺陷探针）：合法 HMAC 签名经 HTTP 必须真正落库并触发学习。

        失败即说明「邮件内 👍/👎 链接」这条**唯一的生产反馈通道**在真实 HTTP 路径上不可用。
        本条**只按可观测行为判定**（HTTP 200 / 落库行数 / 权重组非空），不对实现做任何
        字符串断言；「落库必须真实、重复不得重复计分」的强化版见 test_08a（t9 round 2）。
        """
        import threading

        item_id = "t3:sign:1"
        with Store(self.db) as store:
            store.upsert_items([envelope_of({**self.data_item(), "id": item_id})])
        good = feedback_mod.make_token(item_id, "up", self.cfg)

        with Store(self.db) as store:
            srv = feedback_mod.serve(self.cfg, store, port=0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            err = ""
            status = -1
            try:
                status = _http_status(base + f"/nd/f?id={item_id}&k=up&t={good}")
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            fb_rows = rows_in(self.db, "feedback")
            learned = store.get_weights()
            srv.shutdown()
            srv.server_close()

        self.assertEqual(
            status, 200,
            f"合法签名必须返回 200；实际 status={status} err={err} "
            f"(feedback={fb_rows} 行，权重 {len(learned)} 条)",
        )
        self.assertEqual(fb_rows, 1, "合法反馈必须落库 1 行")
        self.assertTrue(learned, "合法反馈必须产生学习后的权重")
        print(
            f"\n[FAIL-4b] 合法签名 → HTTP {status}，feedback={fb_rows} 行，"
            f"权重 {len(learned)} 条"
        )

    def data_item(self) -> dict:
        return {
            "id": "t3:fail:1",
            "title": "T3 失败模式验证条目",
            "category": "讲座活动",
            "source_name": "T3验证源",
            "published_at": self.now.isoformat(),
            "url": "https://example.invalid/t3/fail",
            "intent_group": "activity",
        }


# ======================================================== 5. 零配置可跑
class Test05ZeroConfig(unittest.TestCase):
    """验收⑥：无 profile.yaml / .env 时仍能 fetch → render，且用内置默认权重。"""

    def test_05a_no_profile_no_env_files_present(self):
        self.assertFalse(
            (ROOT.parent / "profile.yaml").exists(),
            "profile.yaml 存在则无法证明零配置",
        )
        self.assertFalse((ROOT / ".env").exists(), ".env 存在则无法证明零配置")

    def test_05b_builtin_defaults_used(self):
        cfg = config_mod.load_config(
            profile_path=ROOT.parent / "definitely-missing-profile.yaml",
            env_path=ROOT.parent / "definitely-missing.env",
        )
        defaults = config_mod.default_weights_prior()
        merged = score_mod.merged_weights(cfg, {})
        # merged_weights 返回的是「超集」：TIME_PRIORS ⊕ default_weights_prior()
        # ⊕ cfg.weights_prior ⊕ src:* ⊕ mute:*，因此不能与 defaults 做整体相等断言。
        for key, value in defaults.items():
            self.assertIn(key, merged, f"内置默认权重 {key} 缺失")
            self.assertEqual(merged[key], value, f"内置默认权重 {key} 被意外改动")
        self.assertTrue(defaults, "内置默认权重不得为空")
        extra = sorted(set(merged) - set(defaults))
        self.assertTrue(
            extra, "merged_weights 应额外携带时间先验/静音等内置键（超集语义）"
        )
        lo = getattr(score_mod, "W_MIN", -5.0)
        hi = getattr(score_mod, "W_MAX", 5.0)
        for key in extra:
            self.assertGreaterEqual(merged[key], lo, f"{key} 越下界")
            self.assertLessEqual(merged[key], hi, f"{key} 越上界")
        self.assertEqual(cfg.campus, "thu")
        print(
            f"\n[ZERO-CONFIG] cfg.campus={cfg.campus} top_n={cfg.top_n} "
            f"默认权重 {len(defaults)} 条（如 {sorted(defaults)[0]}={defaults[sorted(defaults)[0]]}）"
        )

    def test_05c_zero_config_fetch_to_render_live(self):
        db = WORK / "zeroconf.db"
        if db.exists():
            db.unlink()
        html_out = WORK / "zeroconf_email.html"
        env = dict(os.environ, NOTICE_DIGEST_OUTBOX=str(WORK / "zeroconf_outbox"))
        env.pop("ND_SMTP_HOST", None)

        with _patched_environ(env):
            code, out, err = run_cli(
                [
                    "fetch", "--campus", "thu", "--pages", "1", "--db", str(db),
                    "--profile", str(ROOT.parent / "definitely-missing-profile.yaml"),
                    "--env-file", str(ROOT.parent / "definitely-missing.env"),
                ]
            )
            self.assertEqual(code, 0, f"零配置 fetch 退出码 {code}\nstderr={err}")
            self.assertGreater(json.loads(out)["new"], 0)
            _sleep_live()

            code, out, err = run_cli(
                [
                    "render", "--db", str(db), "--out", str(html_out),
                    "--profile", str(ROOT.parent / "definitely-missing-profile.yaml"),
                    "--env-file", str(ROOT.parent / "definitely-missing.env"),
                ]
            )
            self.assertEqual(code, 0, f"零配置 render 退出码 {code}\nstderr={err}")

        self.assertTrue(html_out.exists(), "render 应产出 HTML")
        size = html_out.stat().st_size
        self.assertGreater(size, 1000, f"HTML 过小（{size} bytes）")
        with Store(db) as store:
            self.assertEqual(store.get_weights(), {}, "零配置下不应有已学权重")
            n = len(store.all_items(limit=5000))
        print(
            f"\n[ZERO-CONFIG] fetch→render OK：items={n} html={size} bytes "
            f"({html_out.name})，无 profile/.env、无已学权重"
        )


# ================================================ 6. 无新条目不发信
class Test06NoNewItemsNoEmail(unittest.TestCase):
    """验收⑦：无新条目 → 不投递、台账不新增记录。"""

    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.env = fake_env_file(T3_TMP / "t3_noemail.env")
        self.empty_db = WORK / "empty.db"
        if self.empty_db.exists():
            self.empty_db.unlink()
        self.cfg = make_cfg(self.empty_db, self.env)

    def test_06a_empty_db_sends_nothing_and_writes_no_ledger(self):
        outbox = WORK / "empty_outbox"
        env = dict(os.environ, NOTICE_DIGEST_OUTBOX=str(outbox))

        with Store(self.empty_db) as store:
            store.init_schema()
        before_ledger = rows_in(self.empty_db, "sends")

        with _patched_environ(env):
            code, out, err = run_cli(
                ["send", "--db", str(self.empty_db), "--dry-run"]
            )

        self.assertEqual(code, 0, f"空日应优雅退出；stderr={err}")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["subject"], "", "空日主题必须是空哨兵")
        self.assertEqual(payload["n_items"], 0)
        self.assertEqual(
            rows_in(self.empty_db, "sends"), before_ledger, "空日不得写台账（无新增记录）"
        )
        self.assertFalse(outbox.exists() and any(outbox.rglob("*")), "空日不得落盘任何邮件产物")
        print(
            f"\n[NO-EMAIL] 空库 → subject='' sent={payload['sent']} "
            f"ledger {before_ledger}→{rows_in(self.empty_db, 'sends')}，无产物落盘"
        )

    def test_06b_all_marked_sent_is_a_noop_for_ledger(self):
        """把当日台账写成「已发送」后再跑一次：台账不得新增；dry-run 永不写台账。"""
        env = dict(os.environ, NOTICE_DIGEST_OUTBOX=str(WORK / "sent_outbox"))
        with Store(self.empty_db) as store:
            store.record_send("2026-10-08", "清华通知日报 10-08｜占位", 11)
        after_mark = rows_in(self.empty_db, "sends")

        with _patched_environ(env):
            run_cli(["send", "--db", str(self.empty_db), "--dry-run"])
        self.assertEqual(
            rows_in(self.empty_db, "sends"), after_mark,
            "dry-run 路径绝不允许写台账",
        )

        # 真实投递路径（SMTP 打桩成异常）也不得写台账
        original = mailer_mod.smtplib.SMTP_SSL
        mailer_mod.smtplib.SMTP_SSL = lambda *a, **k: (_ for _ in ()).throw(
            OSError("t3 verify: tcp down")
        )
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                ok = mailer_mod.send(
                    "清华通知日报 10-08｜占位", "<html><body>x</body></html>", "", self.cfg,
                    dry_run=False,
                )
        finally:
            mailer_mod.smtplib.SMTP_SSL = original
        self.assertIs(ok, False)
        self.assertEqual(rows_in(self.empty_db, "sends"), after_mark)

        print(
            f"\n[NO-EMAIL] 台账标已发送后：dry-run 与失败投递都不新增（仍 {after_mark} 行）"
        )


# ================================================ 7. 凭据与隐私扫描
class Test07CredentialScan(unittest.TestCase):
    """凭据/隐私扫描（07a–07d）：真值模式层 + 「承载秘密的键名」判据。

    判定分三层：
      D1 真值模式层——服务器 IP / 发件邮箱域名 / SMTP 账号 / 真实姓名。四个 needle
         都是拼接字面量（避免本文件自曝），子串命中即报。
      D2 键名层——键名是否「承载秘密」：中文标记须**结尾**（授权码 / 密码 / 口令 /
         密钥 / 凭据）；ASCII 名按 _ - . 切段后任一段精确命中词元表
         （pass / passwd / password / secret / token / key / auth / credential /…）。
         凭据键的取值必须是白名单占位符，其余一切取值一律报命中。
      D3 取值层——先规范化再判：成对剥引号、剥行尾注释、裁尾随 , ; ) } ] 与散文标点、
         再剥尾部非 ASCII 段；然后只判「是否白名单占位符」。空值、true/false/null、
         非凭据键的纯数值、类型名与代码片段不报。

    支持的赋值形态（键名必须落在**行首键位**，以免代码里的形参/关键字误报）：
      ① 键名 + 算子 + 取值（`=` `:` 全角 `＝` `：`），可带 export / 列表项 / 引号；
      ② 键名单独一行 + 紧随行取值（跨行）；
      ③ `#` 注释掉的赋值（单行判定，不跨行续读——注释是自足散文）；
      ④ 无算子紧邻（中文名或含 _ - . 分隔的名，如「授权码 <取值>」）。

    两条命题，务必分清：
      命题 A（成立，实测口径）——本谓词**相对旧谓词更强**，且**净回归 0 已逐例实测**
        （两个基线仪器：① HEAD 85bb15a 的名词子串谓词；② r2 实作 PRE
        `pre_r2_test_integration.py`，sha256 ab4d032a…）。旧谓词把「取值像不像
        随机串」（len≥8 且有数字）当主判据，于是漏掉词形取值与短取值；新判据删除该分支，
        并逐例复核旧谓词的回归集：**无一条由「红」变「绿」**，
        而旧谓词漏掉的形态（小写键名、连字符/点分隔、password/secret/token/key 词元、
        词形取值、尾随标点、跨行取值、JSON/TOML 容器、**行内任意位置的键名**、
        **未加引号却含 `. : @ -` 的取值**、**markdown 表格/反引号/加粗键名**）
        全部转为命中。
      命题 B（不成立）——本谓词**不构成绝对覆盖**。残余盲区至少五处：
        ⑤ 无分隔符的 ASCII 名与取值跨行（`password` 换行 `xxxx`）；
        ⑥ 布尔/空值形态按「无秘密可言」放行；
        ⑦ 取值尾部是中文括注时，先剥尾部非 ASCII 段再进行判定（剥多了会放行）；
        ⑨ 行内回退只对**键位自明**的键启用（中文名词键，或被 `` ` `` `*` `|` 界定的键），
           所以关键字实参形态（裸键作实参、`key` 后接 lambda）与散文模板
           （全大写键名后接同形占位词）在行内一律不报——这是**有意**的收窄，与基线一致（基线只认行首键位），
           不是位置漏判；行首键位下这些形态照报。
        ⑩ TOML/INI 段头形态（`[smtp_pass]` 方括号之外无算子直接跟取值）不报。
        ⑪ 取值含 `://` 的 URL 形态放行（`proxy_pass http://…` 这类指令行不报）；
           基线的 BARE_RE 同样放行，未收紧。
        ⑧ 未加引号且含 `.` 的取值被判为「代码片段/标识符」而放行——**净回归（已修复）**，
           本轮把该放行口收窄为「确有表达式语法」（调用 / 下标 / 属性链），已不再是盲区。
        ⇒ 本测试证明的是「相对旧谓词严格更强，且全树 0 命中」，
          不是「任何形态的泄露都报」。

    有意放宽的一类（旧谓词的假阳性）：纯散文里提到通用中文名词（文档句中的
    「授权码」「密码」三个字），不承载任何取值，必须放行。旧谓词按子串匹配，
    会把 6 行合法文档判红——那是**禁止产品必须产出的文档**的测试侧缺陷。
    """


    PATTERNS = {
        "server_ip": "39" + ".105" + ".73.34",
        "sender_email_domain": "@" + "tsinghua.org.cn",
        "smtp_account": "libr" + "26",
        "real_name": "李博" + "冉",
    }
    SKIP_DIRS = {"data", "__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
    SKIP_FILES = {"verify_notes.md"}  # 扫描报告本身必须列出这些关键词

    # 大写 env 键名（ASCII 拼写，大小写不敏感匹配，大小写后果交给 _is_credential）
    ENV_NAMES = (
        r"(?:ND_)?SMTP_(?:PASS|PASSWORD|AUTH|AUTHCODE|AUTH_CODE|TOKEN)"
        r"|(?:ND_)?HMAC_SECRET"
    )
    # 英文同义写法与中文名词（与 Python 标识符不同形，无从严要求）
    CRED_TOKEN = frozenset({
        "pass", "passwd", "password", "passwords", "passphrase", "pwd",
        "secret", "secrets", "token", "tokens", "key", "keys", "apikey",
        "auth", "authcode", "authorization",
        "credential", "credentials", "creds",
    })
    ENV_ONLY_RE = re.compile(ENV_NAMES)

    # 精确豁免（2026-10-10）：LLM 用量/上限参数名是 OpenAI 兼容接口的标准字段
    # （取值恒为数字，与秘密无关），此前按段匹配「tokens」误伤 gzh_source.py 的
    # `"max_tokens": 8000`。仅按**全名精确比对**豁免，access_token / auth_tokens /
    # api_key 等真实凭据名不受影响。已知代价（有意接受）：秘密若恰好存在名为
    # max_tokens 的键下，本层不再报——现实中不存在这种命名。
    BENIGN_PARAM_NAMES = frozenset({
        "max_tokens", "max_completion_tokens", "max_output_tokens",
        "prompt_tokens", "completion_tokens", "total_tokens",
    })

    CRED_CJK = ("授权码", "密码", "口令", "密钥", "凭据")
    
    _SPLIT = re.compile(r"[_.\-]")
    PLACEHOLDER_RE = re.compile(r"^(|your-.*|<.*>|xxx+|placeholder|changeme|\.\.\.)$")
    PLACEHOLDER_MARKERS = (
        "placeholder", "not-a-real", "not_a_real", "changeme", "change-me", "change_me",
        "dummy", "example", "sample", "fake", "unit-test", "unit_test", "selftest",
        "self-test", "your-", "your_", "xxxx",
    )
    TYPE_NAMES = frozenset({
        "str", "int", "bool", "float", "bytes", "bytearray", "path", "none", "any",
        "dict", "list", "tuple", "set", "object", "optional", "callable", "iterable",
    })
    BOOL_NULL = frozenset({"true", "false", "none", "null", "nil"})
    NUMERIC_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
    BARE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!@#$%^&*_+/=~?@-]*$")
    TRIM_TAIL = ",;)}]\u3002\uff0c\uff1b\uff09\u3011\u300b\u201d\u2019\u00b7\u2026.*`"
    TRAILING_NONASCII = re.compile(r"[^\x00-\x7f]+\s*$")
    
    WORD = r"[A-Za-z0-9\u4e00-\u9fff]+"
    CHAIN = rf"{WORD}(?:[_.\-]{WORD})*"
    # R1：同行「键名 + 分隔符 + 取值」
    ASSIGN_RE = re.compile(
        rf"(?<![A-Za-z0-9_.\-])(?P<name>{CHAIN})(?P<q>[\"']?)\s*(?P<op>[=:\uff1a\uff1d])\s*(?P<rest>.*)$"
    )
    # R2：行首紧邻「键名 + 空白 + 取值」（仅中文名 / 含 _ - . 分隔的名启用）
    ADJACENT_RE = re.compile(
        rf"^\s*(?:export\s+|[-*]\s+|[\"'])?(?P<name>{CHAIN})(?P<q>[\"']?)\s+[\"']?(?P<rest>\S.*)$"
    )
    # 跨行：整行只有凭据键名（可带成对引号与尾随分隔符）
    BARE_NAME_RE = re.compile(
        rf"^\s*(?:export\s+|[-*]\s+|[\"'])?(?P<q>[\"']?)(?P<name>{CHAIN})(?P=q)\s*(?P<op>[=:\uff1a\uff1d])?\s*$"
    )
    ANNOT_RE = re.compile(r"^\s*(?P<t>[A-Za-z_][\w.]*(?:\[[^\]\n]*\])?)\s*=\s*(?P<v>.*)$")
    PREFIX_RE = re.compile(r"^\s*(?:export\s+|[-*+]\s+|[{[]\s*)?[\"'`]?")
    # 注释形态：`# <凭据键> <算子> <取值>`（注释掉的赋值同样是泄露；只取本行，
    # 不跨行续读——注释是自足散文，续行不属于它）
    COMMENT_ASSIGN_RE = re.compile(
        rf"^\s*#+\s*(?P<name>{CHAIN})(?P<q>[\"']?)\s*"
        rf"(?P<op>[=:\uff1a\uff1d])\s*(?P<rest>\S.*)$"
    )
    
    # R5（本轮修复）：**行内任意位置**的「凭据键名 + 可选引号 + 算子 + 取值」。
    # 位置放宽（不再要求键名落在行首键位），但**键位、算子、取值三层判定都不放宽**：
    #   · 键位：只在「键位自明」时启用——① 中文名词键（授权码/密码/…），
    #     或 ② 被标记界定（`` `key` `` / `**key**` / `| key |`）。纯 ASCII 裸键在行内
    #     与代码关键字实参（裸键作实参）、散文模板（全大写键名）无法区分，故仍只认行首键位。
    #   · 算子：只认 `=` `:` 全角 `＝` `：`（与行首键位同一条）。
    #   · 取值：仍走同一套 _value / _unquote / _is_placeholder / judge_value 白名单判定。
    # 仅在行首键位四种形态都未判定该行时才启用（见 _keypos_judge / credential_hits）。
    INLINE_KEY_RE = re.compile(
        rf"(?<![A-Za-z0-9_.\-])(?P<pre>[\"'`|*]?)(?P<name>{CHAIN})(?P<post>[`*|]{{0,2}})"
    )
    # 键位的「标记界定」字符（反引号 / 加粗星号 / 表格竖线）
    # 键位的「标记界定」字符：反引号 / 加粗星号 / 表格竖线（用元组，避免空串 in 字符串恒真）
    MARKUP_CHARS = (chr(96), chr(42), chr(124))
    # ASCII 引号（双引号 + 单引号）：取值里出现即视为被截断/夹带的字符串片段
    STRING_QUOTES = chr(34) + chr(39)
    # 「代码片段」放行只保留给确有表达式语法的取值（调用 / 下标 / 属性链）
    EXPR_CHARS = "()[]{}"
    ATTR_CHAIN_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")

    # ------------------------------------------------------------------ 键名判据
    def _is_credential(self, name):
        """名字是否「承载秘密」。中文标记须结尾；ASCII 词元逐段精确比对（避免 author/monkey）。"""
        if name.lower() in self.BENIGN_PARAM_NAMES:
            return False
        if any(name.endswith(m) for m in self.CRED_CJK):
            return True
        if any(m in name for m in self.CRED_CJK) and not name.isascii():
            return False
        segs = [s for s in self._SPLIT.split(name) if s]
        return bool(segs) and segs[-1].lower() in self.CRED_TOKEN or any(
            s.lower() in self.CRED_TOKEN for s in segs
        )
    
    
    def _last_segment_is_cred(self, name):
        if any(name.endswith(m) for m in self.CRED_CJK):
            return True
        segs = [s for s in self._SPLIT.split(name) if s]
        return bool(segs) and segs[-1].lower() in self.CRED_TOKEN
    
    
    def needs_separator_for_adjacent(self, name):
        """行首紧邻形态的启用条件：中文名或含 _ - . 分隔的名（纯小写单词要算子）。"""
        return (not name.isascii()) or bool(self._SPLIT.search(name))
    
    
    # ------------------------------------------------------------------ 取值判据
    def strip_comment(self, raw):
        m = re.search(r"(?:(?<=\s)|^)#.*$", raw)
        return raw[: m.start()] if m else raw
    
    
    def _value(self, raw):
        """取值规范化：剥行尾注释 -> 裁尾随标点 -> 剥尾随非 ASCII -> 剥成对引号。"""
        v = self.strip_comment(raw).strip()
        v = v.rstrip(self.TRIM_TAIL).strip()
        v = self.TRAILING_NONASCII.sub("", v).strip()
        v = v.rstrip(self.TRIM_TAIL).strip()
        return v
    
    
    def _unquote(self, v):
        stripped = False
        for _ in range(2):
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'`":
                v = v[1:-1].strip()
                stripped = True
        return v, stripped
    
    
    def _is_placeholder(self, value):
        v = value.strip()
        if v == "":
            return True
        if self.PLACEHOLDER_RE.fullmatch(v):
            return True
        low = v.lower()
        return any(mark in low for mark in self.PLACEHOLDER_MARKERS)
    
    
    def _tail_of(self, rest):
        """类型注解形态（`name: str = <v>` / `name: T = <v>`）取默认值；否则整体作为尾巴。"""
        m = self.ANNOT_RE.match(rest)
        return m.group("v") if m else rest
    
    
    def judge_value(self, value, quoted, name):
        """pass=放行；hit=报命中；skip=非取值形态（不参与判定）。"""
        if value == "":
            return "pass"
        if self._is_placeholder(value):
            return "pass"
        if value.lower() in self.BOOL_NULL:
            return "skip"
        # 自引用放行（2026-10-10）：取值与键名是同一个标识符（字典字面量拿键名
        # 同名变量当取值、敏感键配同名变量）——那是引用而非字面量，无秘密可言；
        # 同串字面赋值现实中是开发占位，不是泄露。近形字面量与直配真值不受影响，
        # 照报。（注释措辞刻意避开「凭据名 赋值 同串」的字面形态，防止本文件
        # 被 07a 自扫时误命中——1425 行的教训。）
        if not quoted and value.strip().lower() == name.strip().lower():
            return "skip"
        if not quoted and self.NUMERIC_RE.fullmatch(value) and not self._last_segment_is_cred(name):
            return "skip"
        if quoted:
            return "hit"
        # 本轮修复（F2）：承载秘密的键之下，取值**不再**因含 `.` `:` `@` `-` `+` 等标点
        # 被判为「代码片段/标识符」而放行——那会让「未加引号、含点的取值」逃逸，而同一
        # 取值加了引号就命中：真正在起作用的是引号，不是「像不像代码」。
        # 但旧 BARE_RE 里有一条**结构性**要求必须留下：取值是**单个 token**
        # （不含空白、不含引号；含空白的是散文、含引号的是被截断的字符串片段）。
        # 这一条不比基线更松：基线的 BARE_RE 同样拒绝含空白/引号的取值。
        if re.search(r"\s", value) or any(ch in value for ch in self.STRING_QUOTES):
            return "skip"
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", value):
            return "skip"  # 纯标点不是取值（与基线 BARE_RE 首字符要求一致）
        if "://" in value:
            return "skip"  # URL 形态（与基线一致，见文档盲区 ⑪）
        if value.lower() in self.TYPE_NAMES:
            return "skip"
        # 「代码片段」放行只保留给**确有表达式语法**的取值（调用 / 下标 / 属性链）
        if self._is_expression(value, name):
            return "skip"
        return "hit"
    
    
    def _is_expression(self, value, name):
        """确有表达式语法的取值才放行——原 BARE_RE「代码片段」放行的唯一继承者。"""
        if any(ch in value for ch in self.EXPR_CHARS):
            return True
        return "." in name and self.ATTR_CHAIN_RE.fullmatch(value) is not None

    def _inline_op_value(self, rest):
        """行内算子形态的取值：成对引号字面量优先，否则算子右侧**第一个词**。"""
        s = rest.strip()
        if not s:
            return None
        # 成对引号且内容不含空白/引号（`"abc.def123"`）；含空白的是代码片段（`" + real + "`）
        m = re.match(r"([\"'`])([^\s\"'`]*)\1", s)
        if m:
            v = self._value(m.group(2))
            return (v, True) if v else None
        tok = re.split(r"[\s|]", s, maxsplit=1)[0]
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", tok):
            return None
        v = self._value(tok)
        return (v, False) if v else None

    def _inline_adj_value(self, rest):
        """行内紧邻形态（无算子）的取值：整段残余必须是单个字面取值。
        含内部空白的残余是散文描述（如 markdown 表格里的「SMTP 授权码（…）」），不是取值。"""
        s = rest.strip().strip("|").strip().strip("*").strip()
        if not s or re.search(r"\s", s):
            return None
        s = s.strip("`").strip()
        v, q = self._unquote(self._value(s))
        if v == "" or re.search(r"[\"']", v):
            return None
        return (v, q)

    def _inline_hit(self, line):
        """行首键位未命中时的行内回退：位置放宽，键位/算子/取值判定不放宽。"""
        for m in self.INLINE_KEY_RE.finditer(line):
            name = m.group("name")
            if not self._is_credential(name):
                continue
            markup = m.group("pre") in self.MARKUP_CHARS or bool(m.group("post"))
            if not markup and name.isascii():
                continue  # 键位不自明（ASCII 裸键在行内同代码/散文无法区分）
            tail = line[m.end():]
            mo = re.match(r"\s*(?P<op>[=:\uff1a\uff1d])\s*(?P<rest>\S.*)$", tail)
            if mo:
                iv = self._inline_op_value(mo.group("rest"))
            elif markup or self.needs_separator_for_adjacent(name):
                ma = re.match(r"\s+(?P<rest>\S.*)$", tail)
                iv = self._inline_adj_value(ma.group("rest")) if ma else None
            else:
                iv = None
            if iv and self.judge_value(iv[0], iv[1], name) == "hit":
                return True
        return False

    def _next_value(self, lines, idx):
        """跨行形态：取紧随行的取值（无紧随行返回 None）。"""
        if idx >= len(lines):
            return None
        nxt = lines[idx].strip()
        if not nxt or nxt.startswith("#"):
            return ("", False, nxt)
        v, q = self._unquote(self._value(nxt))
        if re.search(r"[:：＝]", v):
            return ("", False, nxt)
        v, q = self._unquote(self._value(nxt))
        return (v, q, nxt)
    
    
    # ------------------------------------------------------------------ 扫描
    def _keypos_judge(self, line, lines, lineno):
        """行首键位四种形态（原谓词路径，逐字保留）。返回 (是否已判定, 是否命中)：
        「已判定」为真时不再进入行内回退——位置放宽不得让同一行被两套路径重复判。"""
        mc = self.COMMENT_ASSIGN_RE.match(line)
        if mc and self._is_credential(mc.group("name")):
            name = mc.group("name")
            v, q = self._unquote(self._value(mc.group("rest")))
            return (True, self.judge_value(v, q, name) == "hit")
        m = self.ASSIGN_RE.match(line, self.PREFIX_RE.match(line).end())
        if m and self._is_credential(m.group("name")):
            name = m.group("name")
            v, q = self._unquote(self._value(self._tail_of(m.group("rest"))))
            kind = self.judge_value(v, q, name)
            if kind == "pass" and v == "":
                nv = self._next_value(lines, lineno)
                if nv and nv[0]:
                    kind = self.judge_value(nv[0], nv[1], name)
            return (True, kind == "hit")
        mb = self.BARE_NAME_RE.match(line)
        if mb and self._is_credential(mb.group("name")):
            name = mb.group("name")
            nv = self._next_value(lines, lineno)
            kind = "pass"
            if nv and nv[0]:
                kind = self.judge_value(nv[0], nv[1], name)
            elif nv and not nv[2]:
                kind = "pass"
            hit = kind == "hit" or (
                kind == "skip"
                and self.needs_separator_for_adjacent(name)
                and mb.group("op") is None
            )
            return (True, hit)
        ma = self.ADJACENT_RE.match(line)
        if ma and self._is_credential(ma.group("name")):
            name = ma.group("name")
            if self.needs_separator_for_adjacent(name):
                v, q = self._unquote(self._value(ma.group("rest")))
                return (True, self.judge_value(v, q, name) == "hit")
            return (True, False)
        return (False, False)

    def credential_hits(self, rel, text):
        lines = text.splitlines()
        out = []
        for i, line in enumerate(lines):
            lineno = i + 1
            consumed, hit = self._keypos_judge(line, lines, lineno)
            if consumed:
                if hit:
                    out.append(f"{rel}:{lineno} [credential_assignment] {line.strip()}")
                continue
            if self._inline_hit(line):
                out.append(f"{rel}:{lineno} [credential_assignment] {line.strip()}")
        return out

    def scan_tree(self, root=None, skip_dirs=None, skip_files=None):
        root = ROOT if root is None else root
        skip_dirs = self.SKIP_DIRS if skip_dirs is None else skip_dirs
        skip_files = self.SKIP_FILES if skip_files is None else skip_files
        import pathlib
    
        root = pathlib.Path(root)
        scanned = 0
        hits = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(p in skip_dirs for p in rel.parts) or path.name in skip_files:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            hits.extend(self.scan_text(str(rel), text))
        return scanned, hits

    def scan_text(self, rel, text):
        """真值模式层（D1）+ 凭据赋值层（D2/D3）：scan_tree 的唯一入口。"""
        out = []
        for label, needle in self.PATTERNS.items():
            if needle in text:
                for i, line in enumerate(text.splitlines()):
                    if needle in line:
                        out.append("%s:%d [%s] %s" % (rel, i + 1, label, line.strip()))
                        break
        out.extend(self.credential_hits(rel, text))
        return out

    # 「像机器生成」启发式：已降级，只服务 07b 对**非凭据键**的体检，
    # 绝不参与凭据键是否命中的判定（旧谓词把它当主判据，正是净回归的根因）。
    def _looks_generated(self, value):
        if len(value) < 8:
            return False
        return any(ch.isdigit() for ch in value)
    def test_07a_worktree_is_clean(self):
        scanned, hits = self.scan_tree()
        self.assertEqual(hits, [], "凭据/隐私扫描命中：\n" + "\n".join(hits))
        self.assertGreater(scanned, 5, "扫描文件数异常，怀疑路径写错")
        labels = sorted(self.PATTERNS) + ["credential_assignment"]
        print(f"\n[SCAN] 扫描 {scanned} 个文本文件，0 命中（判据：{labels}）")

    def test_07b_env_example_placeholders_only(self):
        example = ROOT / ".env.example"
        if not example.exists():
            self.skipTest(".env.example 尚不存在（属 deploy 任务范畴）")
        text = example.read_text(encoding="utf-8", errors="ignore")
        for label, needle in self.PATTERNS.items():
            self.assertNotIn(needle, text, f".env.example 含真实值 [{label}]")
        self.assertEqual(
            self.credential_hits(".env.example", text),
            [],
            ".env.example 含赋值形态的真实凭据值",
        )
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            key = key.strip()
            value = self._value(raw)
            if self.ENV_ONLY_RE.fullmatch(key):
                self.assertTrue(
                    self._is_placeholder(value),
                    f".env.example 的凭据键 {key} 取值不是占位符：{value!r}",
                )
            else:
                self.assertFalse(
                    self._looks_generated(value),
                    f".env.example 的非凭据键 {key} 取值像机器生成的秘密：{value!r}",
                )
        print("\n[SCAN] .env.example 仅含占位符（凭据键全为占位符，其余键无生成形态取值）")

    def test_07c_credential_predicate_semantics(self):
        """谓词语义自证（逐例钉死）：相对旧谓词更强，且不是放宽换绿灯。

        每条都注明它属于哪种逃逸形态：红例必红、绿例必绿，逐例 assert。
        """
        noun = "\u6388" + "\u6743" + "\u7801"
        real = "Kx7" + "Qm2Zp9Lw4"      # 机器生成形态样例，非仓库内任何真实值
        word = "realthing"              # 词形取值：旧谓词正是漏掉这一整类
        cases = [
            # ---- E1 键名放宽：旧谓词只认英文 authcode 与中文名词，以下旧谓词全漏 ----
            ("ND_SMTP_PASS" + "=" + real, True, "E1 大写 env 键名"),
            ("smtp_pass" + "=" + real, True, "E1 小写键名"),
            ("SMTP_PASSWORD" + "=" + real, True, "E1 password 全拼"),
            ("DB_SECRET" + "=" + real, True, "E1 secret 段"),
            ("api_key" + "=" + real, True, "E1 api_key 下划线"),
            ("AUTH_TOKEN" + "=" + real, True, "E1 auth 与 token 双段"),
            ("SMTP_AUTH_CODE" + "=" + real, True, "E1 auth_code"),
            ("ND_HMAC_SECRET" + "=" + real, True, "E1 ND_ 前缀 + HMAC"),
            ("smtp-pass" + "=" + real, True, "E1 连字符分隔"),
            ("smtp.pass" + "=" + real, True, "E1 点分隔"),
            ("credential" + "=" + real, True, "E1 credential 词元"),
            ("SMTP_PASSWD" + "=" + real, True, "U passwd 词元"),
            ("mail_pass" + "=" + real, True, "Z pass 段"),
            # ---- E2 中文键名（含全角算子与无算子紧邻）----
            (noun + "=" + real, True, "E2 中文标记 + 等号"),
            (noun + "\uff1a" + real, True, "E2 全角冒号"),
            (noun + "\uff1a" + real + "\u3002", True, "E2 尾随散文句号仍须命中"),
            (noun + " " + real, True, "E2 无算子紧邻"),
            (noun + "\uff1a'" + real + "'", True, "L6 中文键 + 单引号"),
            # ---- E3 注释形态：注释掉的赋值同样是泄露 ----
            ("#" + noun + "=" + real, True, "E3 注释形态 + 等号"),
            ("# " + noun + "=" + real + "\u3002", True, "E3 注释形态 + 尾随句号"),
            # ---- E4 算子 / 引号 / 容器 ----
            ("ND_SMTP_PASS" + "\uff1d" + real, True, "E4 全角等号"),
            ("ND_SMTP_PASS" + " : " + real, True, "E4 空格环绕 + 半角冒号"),
            ("ND_SMTP_PASS" + '="' + real + '"', True, "E4 双引号包裹"),
            ("ND_SMTP_PASS" + "='" + real + "'", True, "E4 单引号包裹"),
            ('{"smtp_pass": "' + real + '"}', True, "K2 JSON 对象"),
            ("{'smtp_pass': '" + real + "'}", True, "K4 单引号 dict"),
            ("export ND_SMTP_PASS='" + real + "'", True, "E4 shell export"),
            ("- ND_SMTP_PASS=" + real, True, "E4 列表项"),
            # ---- E5 尾随标点与行尾注释 ----
            ("ND_SMTP_PASS=" + real + ",", True, "J3 尾随逗号"),
            ('password = "' + real + '",', True, "H 引号 + 尾随逗号"),
            ("ND_SMTP_PASS=" + real + ";", True, "尾随分号"),
            ("ND_SMTP_PASS=" + real + ")", True, "尾随右括号"),
            ("ND_SMTP_PASS=" + real + "  # 备注", True, "AA 行尾注释"),
            ("hmac_secret" + "=" + real, True, "G2 hmac_secret 必红"),
            # ---- E6 词形取值：旧谓词因「不像随机串」而放行的净回归 ----
            ("hmac_secret" + "=" + word, True, "G2b 词形取值也必红"),
            ("smtp_pass" + "=" + word, True, "D 小写键 + 词形取值（旧谓词漏）"),
            ("smtp_pass" + ": " + word, True, "D5 冒号 + 词形取值（旧谓词漏）"),
            ("PASSWORD" + "=" + word, True, "P2 纯大写 PASSWORD + 词形取值"),
            ("smtp-pass" + ": " + real, True, "V2 连字符 + 冒号"),
            ("password" + ": " + real, True, "H2 password 冒号"),
            ("passwd" + "=" + real, True, "H3 passwd"),
            ("secret" + " = " + real, True, "H4 secret 空格环绕"),
            ("token" + " = " + real, True, "H5 token"),
            # ---- E7 跨行取值：键名行 + 紧随行 ----
            (noun + "\n" + real, True, "M2 键名行 + 紧随行取值"),
            ("ND_SMTP_PASS" + "=" + "\n" + real, True, "M2b 等号后换行再取值"),
            (noun + "\uff1a" + "\n" + real, True, "M2c 全角冒号后换行"),
            ("smtp_pass" + "\n" + real, True, "M2d 小写键名 + 跨行"),
            (noun + "  \n  " + real, True, "M2e 键名行带缩进与尾随空白"),
            ("hmac_secret" + "\n" + real, True, "M2f 跨行 + 非生成形态"),
            # ---- 绿例：占位符 / 非凭据键 / 注解 / 代码片段 ----
            ("ND_SMTP_PASS" + "=", False, "空值"),
            ("ND_SMTP_PASS" + "=your-" + "smtp-auth-code", False, "your- 占位"),
            ("ND_SMTP_PASS" + "=<" + noun + ">", False, "<...> 占位"),
            ("ND_SMTP_PASS" + "=placeholder", False, "placeholder 占位"),
            ("ND_SMTP_PASS" + "=xxx", False, "xxx 占位"),
            ("ND_SMTP_PASS" + "=...", False, "省略号占位"),
            ("ND_SMTP_PORT" + "=465", False, "F2 非凭据键 + 纯数值（披露项 1）"),
            ("smtp_pass" + ": str = " + '""', False, "F3 Python 注解 + 空串默认值"),
            ("smtp_pass" + ": str", False, "F3b 注解无取值"),
            ("hmac_secret" + ": str", False, "注解无取值"),
            ("password" + ": str = " + "None", False, "注解默认 None"),
            ("smtp_pass" + "=" + 'opt("ND_' + "SMTP" + '_PASS", "")', False, "代码片段不是取值"),
            ("# " + noun + "见密码管理器", False, "X 散文提及：只说在哪、不给值"),
            (noun + "\uff1a" + "your-auth-code", False, "Y 占位取值 + 中文键"),
            # ---- 绿例：产品必须产出的 6 行合法文档（旧谓词的假阳性，不得改写文档）----
            ("# 本文件只列**键名与含义**，不放任何真实账号、" + noun + "、密钥。", False, "披露项 4 文档行 1"),
            ("# SMTP " + noun + " / 密码（**这是唯一必须手工填入的敏感项**）", False, "披露项 4 文档行 2"),
            ("cp .env.example .env && chmod 0600 .env   # 然后手工填入 SMTP 账号与" + noun, False, "披露项 4 文档行 3"),
            ("| `ND_SMTP_PASS` | SMTP " + noun + "（**唯一必须手工填的敏感项**） |", False, "披露项 4 文档行 4"),
            ("    warn \"已从 .env.example 生成 $APP_DIR/.env（占位值）——**必须**填入真实 SMTP 账号/" + noun + "后再运行\"", False, "披露项 4 文档行 5"),
            ("     必填项见 $APP_DIR/.env.example 的注释；确认真实 SMTP 账号/" + noun + "已填入。", False, "披露项 4 文档行 6"),
            # ---- 本轮（r3）新闭合类：行内回退必须命中（位置放宽，算子与取值判定不放宽）----
            # A 组：中文名词不在行首键位
            ("SMTP " + noun + "\uff1a" + real, True, "R3-A1 中文名在行内（前缀 SMTP）"),
            ("SMTP " + noun + ": " + real, True, "R3-A2 中文名在行内 + 半角冒号"),
            ("SMTP " + noun + " " + real, True, "R3-A3 中文名在行内 + 无算子紧邻"),
            ("\u90ae\u7bb1 " + noun + "\uff1a" + real, True, "R3-A4 中文名在行内（前缀中文）"),
            ("\uff08" + noun + "\uff1a" + real + "\uff09", True, "R3-A5 中文名在行内 + 中文括注"),
            # B 组：未加引号且含标点的取值（旧谓词的「代码片段」放行口）
            ("ND_SMTP_PASS" + "=" + "abc.def123", True, "R3-B1 未引号 + 点"),
            ("ND_SMTP_PASS" + "=" + "P@ssw0rd.2026", True, "R3-B2 未引号 + @"),
            ("ND_SMTP_PASS" + "=" + "abc:def123", True, "R3-B3 未引号 + 冒号"),
            ("ND_SMTP_PASS" + "=" + "abc.def123.", True, "R3-B4 未引号 + 尾随点"),
            ("smtp_pass" + "=" + "abc.def123", True, "R3-B5 小写键 + 点"),
            ("hmac_secret" + "=" + "a.b1c2d3", True, "R3-B6 属性链形态取值也必须报"),
            ("smtp_pass" + ": " + "abc.def123", True, "R3-B7 冒号 + 点"),
            # C 组：markdown 表格 / 反引号 / 加粗键名
            ("| `ND_SMTP_PASS` | " + real + " |", True, "R3-C1 表格行值列"),
            ("| `smtp_pass` | `" + real + "` |", True, "R3-C2 表格行 + 反引号值"),
            ("**smtp_pass**" + ": " + real, True, "R3-C3 加粗键名 + 冒号"),
            ("**ND_SMTP_PASS**" + "=" + real, True, "R3-C4 加粗键名 + 等号"),
            # 本轮新披露的放行面（必须继续放行，且是收窄后的表达式口）
            ("self.password" + " = " + "self.cfg.password", False, "R3-G2 属性链赋值不是字面取值"),
        ]
        for text, expect, why in cases:
            hits = self.credential_hits("probe.txt", text)
            self.assertEqual(bool(hits), expect, f"{why}：{text!r} → {hits}")
        # 命中行必须带「文件:行号」前缀与判据标签（下游靠它定位）
        first = self.credential_hits("probe.txt", "ND_SMTP_PASS" + "=" + real)
        self.assertTrue(first)
        self.assertTrue(first[0].startswith("probe.txt:1 "), first)
        self.assertIn("[credential_assignment]", first[0])
        # 反向对照（反证）：_looks_generated 已不在命中路径上
        self.assertTrue(self._looks_generated(real), "样例本身满足旧「生成形态」启发式")
        self.assertTrue(
            self.credential_hits("probe.txt", "hmac_secret" + "=" + "s3cret"),
            "短且非生成形态的真实值也必须报：证明命中不再由 _looks_generated 决定",
        )
        print(f"\n[SCAN] 谓词语义自证 {len(cases)} 例全过（含跨行取值与注释形态）")

    def test_07d_truth_patterns_still_live(self):
        """4 个真值模式逐个注入项目外临时树 ⇒ 必须命中并带 label（证明未削弱）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for label, needle in self.PATTERNS.items():
                probe = Path(tmp) / f"{label}.txt"
                probe.write_text("无害前缀 " + needle + " 无害后缀\n", encoding="utf-8")
            scanned, hits = self.scan_tree(Path(tmp))
            self.assertEqual(scanned, len(self.PATTERNS), "临时树扫描文件数异常")
            for label in self.PATTERNS:
                self.assertTrue(
                    any(f":1 [{label}] " in hit for hit in hits),
                    f"[{label}] 注入真值未命中：{hits}",
                )
            print(f"\n[SCAN] 4 个真值模式在项目外副本上逐个命中（{len(hits)} 条）")


# ------------------------------------------------------------------- 辅助
@contextlib.contextmanager
def _patched_environ(env: dict):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({k: str(v) for k, v in env.items() if v is not None})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _patch_detail(func):
    patched = []
    for module in (fetch_mod, enrich_mod):
        if hasattr(module, "fetch_detail"):
            patched.append((module, module.fetch_detail))
            module.fetch_detail = func
    return patched


def _unpatch_detail(patched):
    for module, original in patched:
        module.fetch_detail = original


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"请求 {url} 失败：{type(exc).__name__}: {exc}") from exc


def _newest_ics() -> bytes | None:
    files = sorted(T3_OUTBOX.rglob("*.ics")) if T3_OUTBOX.exists() else []
    return files[-1].read_bytes() if files else None


# ==================================== round 2（t9）复验工具（边界打桩，非逻辑替换）
def _rm(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _count_rows(db: Path, table: str, where: str = "", params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db))
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


@contextlib.contextmanager
def _feedback_server(cfg, db: Path):
    """在**孤立端口**（``port=0``，绝不碰 8791）起真实反馈服务，退出时关停。"""
    import threading

    store = Store(db)
    srv = feedback_mod.serve(cfg, store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


@contextlib.contextmanager
def _failure_mail_spy():
    """**边界**打桩：只观测 CLI 是否*尝试*发失败邮件，不替换任何判定逻辑。"""
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


def _http_get_full(url: str, headers: dict | None = None) -> tuple[int, dict, str]:
    """返回 (status, 小写响应头, body)；4xx/5xx 也当正常结果取回，便于断言。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return (
                int(resp.status),
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            int(exc.code),
            {k.lower(): v for k, v in (exc.headers or {}).items()},
            exc.read().decode("utf-8", "replace"),
        )


class _FakeHTTPResponse:
    """只实现 fetch._get_json 用到的 read()/上下文协议，用来驱动**真实**重试与解析逻辑。"""

    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


# ====================================== 8. round 2（t9）：按行为复验关闭项
class Test08Round2Closure(unittest.TestCase):
    """t9：R-1/D-2/R-4/R-5 与退出码分级。

    纪律：**只按可观测行为判定**；落库校验直查 SQLite，HTTP 校验走真实回环服务，
    失败模式打桩点在最底层（urlopen / fetch_list）以便重试循环、JSON 解析、
    异常归类**真实执行**。
    """

    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.env = fake_env_file(T3_TMP / "t3_r2.env")
        self.db = WORK / "round2.db"
        _rm(self.db)
        self.cfg = make_cfg(self.db, self.env)
        self.now = store_mod.now_shanghai()
        with Store(self.db) as store:
            # 关掉「每日衰减」这一混淆项：只测反馈本身是否改变权重
            store.set_meta("last_decay_date", self.now.date().isoformat())

    # ------------------------------------------------------------------ 工具
    def _item(self, item_id: str) -> dict:
        return envelope_of({
            "id": item_id,
            "title": f"t9 复验条目 {item_id}",
            "category": "讲座活动",
            "source_name": "t9 验证源",
            "published_at": self.now.isoformat(),
            "url": "https://example.invalid/t9/r2",
            "intent_group": "activity",
        })

    def _batch(self, prefix: str, n: int = 30) -> list[dict]:
        return [
            {
                "id": f"t9:{prefix}:{i}",
                "category": "讲座活动",
                "english": "",
                "intent_group": "event",
                "published_at": "2026-10-08T10:00:00+08:00",
                "source_id": "t9",
                "source_name": "t9 验证源",
                "title": f"{prefix} 第 {i} 条通知",
                "url": f"https://example.invalid/{prefix}/{i}",
            }
            for i in range(n)
        ]

    def _fetch_probe(self, db: Path, pages: int, fake_list=None, fake_urlopen=None):
        """跑一次真实 ``cmd_fetch``，返回 (code, out, err, 失败邮件调用, 计数)。"""
        meta = {"list_calls": 0, "urlopen_calls": 0, "sleeps": 0}
        saved = {}
        if fake_list is not None:
            saved["list"] = fetch_mod.fetch_list

            def counting_list(campus, page, **kw):
                meta["list_calls"] += 1
                return fake_list(campus, page, **kw)

            fetch_mod.fetch_list = counting_list
        if fake_urlopen is not None:
            saved["urlopen"] = fetch_mod.urllib.request.urlopen

            def counting_urlopen(req, timeout=None):
                meta["urlopen_calls"] += 1
                return fake_urlopen(req, timeout)

            fetch_mod.urllib.request.urlopen = counting_urlopen
            if hasattr(fetch_mod, "time"):
                saved["sleep"] = fetch_mod.time.sleep

                def counting_sleep(*_a, **_k):
                    meta["sleeps"] += 1

                fetch_mod.time.sleep = counting_sleep
        try:
            with _failure_mail_spy() as mail:
                code, out, err = run_cli(
                    ["fetch", "--campus", "thu", "--pages", str(pages), "--db", str(db)]
                )
        finally:
            if "list" in saved:
                fetch_mod.fetch_list = saved["list"]
            if "urlopen" in saved:
                fetch_mod.urllib.request.urlopen = saved["urlopen"]
            if "sleep" in saved:
                fetch_mod.time.sleep = saved["sleep"]
        return code, out, err, list(mail), meta

    # ------------------------------------------------- D-2：落库、幂等（行为）
    def test_08a_signed_feedback_records_and_learns_once(self):
        """判据 A：合法签名 → 200 + ``feedback`` 真落库 1 行 + ``weights`` 真变化；
        同 (item_id, kind) 重复请求 → 仍 1 行、权重不再变化（D-2 关闭）。"""
        item_id = "t9:r2:up"
        with Store(self.db) as store:
            store.upsert_items([self._item(item_id)])
            w0 = dict(store.get_weights())
        tok = feedback_mod.make_token(item_id, "up", self.cfg)
        with _feedback_server(self.cfg, self.db) as base:
            url = f"{base}/nd/f?id={item_id}&k=up&t={tok}"
            s1, h1, _ = _http_get_full(url)
            with Store(self.db) as store:
                n1 = _count_rows(self.db, "feedback", "item_id = ?", (item_id,))
                w1 = dict(store.get_weights())
            s2, h2, _ = _http_get_full(url)
            with Store(self.db) as store:
                n2 = _count_rows(self.db, "feedback", "item_id = ?", (item_id,))
                w2 = dict(store.get_weights())

        self.assertEqual(s1, 200, f"合法签名首答必须 200；headers={h1}")
        self.assertEqual(s2, 200, f"重复提交仍应 200（幂等）；headers={h2}")
        self.assertEqual(n1, 1, "合法反馈必须真实落库 1 行 —— 这是 D-2 的核心行为判据")
        self.assertEqual(n2, 1, "同一 (item_id, kind) 不得重复计数")
        self.assertTrue(w1, "合法反馈必须产生学习后的权重组（空 = 学习信号被吞）")
        self.assertNotEqual(w1, w0, "合法反馈必须改变权重（只回 200 不落库即 D-2 复发）")
        self.assertEqual(w2, w1, "重复反馈不得再次施加奖励")
        print(
            f"\n[R2-D2] 首答 {s1} / 重复 {s2}；feedback 行数 {n1}→{n2}；"
            f"权重条数 {len(w0)}→{len(w1)}→{len(w2)}"
        )

    # ---------------------------------------- prefetch：可观测 + 通用库名不误杀
    def test_08b_prefetch_drop_is_observable_not_silent(self):
        """判据 B（前半）：被判为 prefetch 的**显式反馈**不得静默吞掉 ——
        必须同时具备①机器可读信号（非 200 状态码 或 X-ND-* 响应头）与②日志行；
        且不得计数。只看「能不能区分出这次被丢弃了」，不规定具体实现形式。"""
        item_id = "t9:r2:prefetch"
        with Store(self.db) as store:
            store.upsert_items([self._item(item_id)])
        tok = feedback_mod.make_token(item_id, "up", self.cfg)
        err_buf = io.StringIO()
        with _feedback_server(self.cfg, self.db) as base:
            with contextlib.redirect_stderr(err_buf):
                status, headers, _body = _http_get_full(
                    f"{base}/nd/f?id={item_id}&k=up&t={tok}",
                    headers={"User-Agent": "Mozilla/5.0 (GoogleImageProxy)"},
                )
        log = err_buf.getvalue()
        n = _count_rows(self.db, "feedback", "item_id = ?", (item_id,))
        machine = []
        if status != 200:
            machine.append(f"status={status}")
        hdr = sorted(k for k in headers if k.startswith("x-nd-"))
        if hdr:
            machine.append("headers=" + ",".join(hdr))
        marker = ("prefetch" in log.lower()) or ("预取" in log)
        self.assertTrue(
            machine,
            f"prefetch 丢弃必须有机器可读标记（非 200 或 X-ND-* 头）；status={status} headers={headers}",
        )
        self.assertTrue(marker, f"prefetch 丢弃必须留日志（stderr 未见 prefetch/预取）：{log!r}")
        self.assertEqual(n, 0, "prefetch 不得计入反馈（否则等于把扫描器当成真人点赞）")
        print(f"\n[R2-PREFETCH] status={status} 机器信号={machine} 日志={log.strip()[:100]!r} 落库={n}")

    def test_08c_generic_client_ua_is_not_prefetch_evidence(self):
        """判据 B（后半）：``python-urllib`` / ``requests`` / ``curl`` 这类**通用 HTTP
        客户端库名**不得构成 prefetch 证据 —— 带合法签名的自动化请求必须被接受并计数。

        历史教训：把它们当预取，会让任何脚本化驱动（含本验收探针）的合法反馈被静默丢弃。
        """
        for ua, tag in (
            ("Python-urllib/3.12", "urllib"),
            ("python-requests/2.32.3", "requests"),
            ("curl/8.4.0", "curl"),
        ):
            item_id = f"t9:r2:ua:{tag}"
            db = WORK / f"r2_ua_{tag}.db"
            _rm(db)
            cfg = make_cfg(db, self.env)
            with Store(db) as store:
                store.upsert_items([self._item(item_id)])
                store.set_meta("last_decay_date", self.now.date().isoformat())
            tok = feedback_mod.make_token(item_id, "up", cfg)
            with _feedback_server(cfg, db) as base:
                status, headers, _body = _http_get_full(
                    f"{base}/nd/f?id={item_id}&k=up&t={tok}", headers={"User-Agent": ua}
                )
            n = _count_rows(db, "feedback", "item_id = ?", (item_id,))
            self.assertEqual(
                status, 200, f"UA={ua!r} 是通用客户端库，必须 200；实际 {status} headers={headers}"
            )
            self.assertEqual(
                n, 1, f"UA={ua!r} 的合法签名反馈必须落库 1 行（被当预取丢弃即 D-2 复发）"
            )
            print(f"\n[R2-UA] {ua!r} → HTTP {status}，feedback={n} 行")

    # --------------------------------------------------- 退出码分级（致命侧）
    def test_08d_fatal_exit_codes_trigger_failure_mail(self):
        """判据 C（致命侧）：非 JSON / HTTP 5xx 重试耗尽 / 首页为空 / 列表项缺必需字段
        ⇒ exit 3 且触发失败邮件。

        非 JSON 与 5xx 两例打桩点在最底层 ``urlopen``，因此**重试循环、JSON 解析、
        异常归类全部真实执行**，并断言确实重试耗尽（urlopen 调用次数 >1）。
        """
        evidence = []

        # (1) 非 JSON
        db = WORK / "r2_nojson.db"
        _rm(db)
        code, out, err, mail, meta = self._fetch_probe(
            db, 1, fake_urlopen=lambda req, t: _FakeHTTPResponse(b"<html>not json</html>", 200)
        )
        evidence.append(("non-json", code, meta["urlopen_calls"], mail, err.strip().splitlines()[-1:]))
        self.assertEqual(code, 3, f"非 JSON 必须 exit 3；stderr={err}")
        self.assertGreaterEqual(meta["urlopen_calls"], 2, "必须真的重试过（重试耗尽才算致命）")
        self.assertTrue(mail, "非 JSON 必须触发失败邮件")
        self.assertIn("exit 3", err)

        # (2) HTTP 5xx 重试耗尽
        db = WORK / "r2_5xx.db"
        _rm(db)

        def boom(req, t):
            raise urllib.error.HTTPError("http://example.invalid", 503, "Service Unavailable", {}, None)

        code, out, err, mail, meta = self._fetch_probe(db, 1, fake_urlopen=boom)
        evidence.append(("http-503", code, meta["urlopen_calls"], mail, err.strip().splitlines()[-1:]))
        self.assertEqual(code, 3, f"5xx 重试耗尽必须 exit 3；stderr={err}")
        self.assertGreaterEqual(meta["urlopen_calls"], 2, "5xx 必须重试后仍失败才判致命")
        self.assertTrue(mail, "5xx 重试耗尽必须触发失败邮件")

        # (3) 首页为空
        db = WORK / "r2_empty.db"
        _rm(db)
        code, out, err, mail, meta = self._fetch_probe(
            db, 3, fake_list=lambda c, p, **k: {"items": None, "total": 0, "page": p}
        )
        payload = json.loads(out)
        evidence.append(("page-1-empty", code, meta["list_calls"], mail, payload["stop_reason"]))
        self.assertEqual(code, 3, f"首页为空必须 exit 3；stderr={err}")
        self.assertEqual(payload["stop_reason"], "page-1-empty")
        self.assertEqual(payload["pages_scanned"], 1)
        self.assertTrue(mail, "首页为空必须触发失败邮件")

        # (4) 列表项缺必需字段 —— 契约要求它属致命侧；t10 修复后已按致命侧处理
        #     （schema-drift ⇒ exit 3 + 失败邮件），严格判据单列在 test_08i，此处只留证据。
        db = WORK / "r2_missing.db"
        _rm(db)
        code, out, err, mail, meta = self._fetch_probe(
            db, 2, fake_list=lambda c, p, **k: {"items": [{"title": "缺 id 的条目"}], "total": 1, "page": p}
        )
        payload = json.loads(out)
        evidence.append((
            "item-missing-key",
            code,
            meta["list_calls"],
            mail,
            f"new={payload['new']} stop={payload['stop_reason']} rows={rows_in(db, 'items')}",
        ))

        print(f"\n[R2-EXITCODE-FATAL] {evidence}")

    # ------------------------------------------------- 退出码分级（非致命侧）
    def test_08e_tail_repeat_is_warn_exit0_no_mail(self):
        """R-4 关闭：全新库首轮全量抓取一路跑到站尾（越界页被服务端钳制、重复返回同一批）
        ⇒ 仅 warn + exit 0 + **不发失败邮件**；收敛靠合法判据之一（此处为「整页 id 全已知」）。"""
        db = WORK / "r2_tail.db"
        _rm(db)
        page1, page2 = self._batch("ta", 30), self._batch("tb", 30)
        code, out, err, mail, meta = self._fetch_probe(
            db,
            10,
            fake_list=lambda c, p, **k: {
                "items": page1 if p == 1 else page2,
                "total": 60,
                "page": p,
                "page_size": 30,
            },
        )
        payload = json.loads(out)
        kinds = {(a["kind"], a["severity"]) for a in payload["anomalies"]}
        stop = str(payload["stop_reason"])
        self.assertEqual(code, 0, f"尾页重复是站点正常行为，必须 exit 0；stderr={err}")
        self.assertEqual(mail, [], "尾页重复不得触发失败邮件（否则每次跑到站尾都发假警报）")
        self.assertEqual(payload["new"], 60, "两页新条目都应入库")
        self.assertIn(("page-repeat", "warn"), kinds, f"须显式记 warn：{kinds}")
        self.assertTrue(
            stop.endswith("all-known") or stop.endswith("repeated"),
            f"收敛原因须为三条合法判据之一：{stop}",
        )
        self.assertNotIn("reached-max-pages", stop, "不得靠耗尽预算收尾")
        self.assertEqual(meta["list_calls"], 3, "应在第 3 页收敛，不为未用满的预算继续请求")
        self.assertEqual(rows_in(db, "items"), 60, "重复页不得写入重复行")
        print(f"\n[R2-R4] exit={code} stop={stop} anomalies={sorted(kinds)} 请求页码数={meta['list_calls']} 落库={rows_in(db, 'items')}")

    def test_08f_detail_404_does_not_abort_batch(self):
        """R-5 关闭：列表挂着但详情 404 的条目不得中断整批 —— 其余条目照常补全入库、
        404 计数出现在 CLI 输出里、整体 exit 0 且不发失败邮件。"""
        ids = ["t9:r2:ok1", "t9:r2:gone", "t9:r2:ok2"]
        with Store(self.db) as store:
            store.upsert_items([self._item(i) for i in ids])

        def fake_detail(campus, item_id, session=None, **kw):
            if item_id == "t9:r2:gone":
                raise fetch_mod.FetchError("HTTP 404 Not Found")
            return {
                "id": item_id,
                "title": f"详情 {item_id}",
                "content": "正文……",
                "content_markdown": "正文……",
                "event_start": "2026-10-09T19:30:00+08:00",
                "event_end": None,
                "event_location": None,
                "deadline": None,
                "deadline_text": "",
                "time_text": "",
                "location": "",
                "organizer": "t9 验证",
                "ai_event_time": "2026-10-09 19:30",
                "ai_event_location": "六教",
                "ai_time_evidence": "2026-10-09 19:30",
                "ai_intent": "",
                "ai_is_event": True,
                "ai_summary": "",
                "category": "讲座活动",
                "source_name": "t9 验证源",
                "published_at": self.now.isoformat(),
                "url": "https://example.invalid/t9/r2",
                # 实测（活体 12/12 条详情载荷）上游详情 JSON 自带这两个字段，
                # store.update_detail() 原样抄进 items.detail_status / body_status。
                "detail_status": "complete",
                "body_status": "ok",
            }

        patched = _patch_detail(fake_detail)
        try:
            with _failure_mail_spy() as mail:
                code, out, err = run_cli(["enrich", "--db", str(self.db), "--limit", "5"])
        finally:
            _unpatch_detail(patched)

        payload = json.loads(out)
        self.assertEqual(code, 0, f"单条 404 不得让整批失败；stderr={err}")
        self.assertEqual(mail, [], "单条 404 属上游正常噪音，不得触发失败邮件")
        self.assertEqual(int(payload.get("not_found") or 0), 1, f"404 计数须出现在输出里：{payload}")
        self.assertGreaterEqual(int(payload.get("fetched") or 0), 2, f"其余条目必须照常补全：{payload}")
        with Store(self.db) as store:
            ok1 = store.get_item("t9:r2:ok1") or {}
            ok2 = store.get_item("t9:r2:ok2") or {}
            gone = store.get_item("t9:r2:gone") or {}
        # 「其余条目照常入库」判据分两层：① 详情正文 + 抓取时间落库；② detail_status 由
        # store.update_detail() 从上游详情载荷原样抄入（实测活体 12/12 条载荷自带该字段），
        # 抄入 complete 的条目必须退出待补全队列，第二轮不得重复抓取。
        for tag, row in (("ok1", ok1), ("ok2", ok2)):
            self.assertTrue(row.get("detail"), f"{tag} 的详情应已落库：{row}")
            self.assertIsNotNone(row.get("detail_fetched_at"), f"{tag} 应记录详情抓取时间：{row}")
            self.assertEqual(
                (row.get("detail_status") or "").lower(),
                "complete",
                f"{tag} 的 detail_status 应来自详情载荷并落库：{row.get('detail_status')!r}",
            )
        self.assertFalse(gone.get("detail"), "404 条目不得写入详情")
        self.assertFalse(
            (gone.get("detail_status") or ""), "404 条目不得被标为已补全（否则队列假装收敛）"
        )

        # 幂等 / 队列推进：已 complete 的条目第二轮不得重复抓详情。
        seen: list = []

        def recording_detail(campus, item_id, session=None, **kw):
            seen.append(item_id)
            return fake_detail(campus, item_id, session=session, **kw)

        patched2 = _patch_detail(recording_detail)
        try:
            code2, out2, err2 = run_cli(["enrich", "--db", str(self.db), "--limit", "5"])
        finally:
            _unpatch_detail(patched2)
        self.assertEqual(code2, 0, f"第二轮补全不得失败；stderr={err2}")
        self.assertNotIn("t9:r2:ok1", seen, "detail_status=complete 的条目不得重复抓详情")
        self.assertNotIn("t9:r2:ok2", seen, "detail_status=complete 的条目不得重复抓详情")
        print(
            f"\n[R2-R5] exit={code} not_found={payload.get('not_found')} fetched={payload.get('fetched')} "
            f"ok1_detail={'有' if ok1.get('detail') else '无'} ok2_detail={'有' if ok2.get('detail') else '无'} "
            f"gone_detail={'有' if gone.get('detail') else '无'} "
            f"detail_status={ok1.get('detail_status')!r}/{ok2.get('detail_status')!r}/{gone.get('detail_status')!r} "
            f"第二轮重抓={seen}"
        )

    # ------------------------------------------------ 收敛判据之三 + 零新增对照
    def test_08g_max_pages_convergence_is_warn_exit0(self):
        """收敛判据之三：触达 ``pages`` 上限（尚未出现空页/全已知页）⇒ warn + exit 0，
        不发失败邮件 —— 「翻到预算上限」是提示性偏差，不是致命故障。"""
        db = WORK / "r2_maxpages.db"
        _rm(db)
        code, out, err, mail, meta = self._fetch_probe(
            db,
            2,
            fake_list=lambda c, p, **k: {
                "items": self._batch(f"mp{p}", 3),
                "total": 99,
                "page": p,
                "page_size": 30,
            },
        )
        payload = json.loads(out)
        kinds = {(a["kind"], a["severity"]) for a in payload["anomalies"]}
        self.assertEqual(code, 0, f"触达 pages 上限不得判失败；stderr={err}")
        self.assertEqual(payload["stop_reason"], "reached-max-pages", f"{payload}")
        self.assertIn(("max-pages-hit", "warn"), kinds, f"须显式记 warn：{kinds}")
        self.assertEqual(mail, [], "触达预算上限不得发失败邮件")
        self.assertEqual(meta["list_calls"], 2)
        print(f"\n[R2-MAXPAGES] exit={code} stop={payload['stop_reason']} anomalies={sorted(kinds)}")

    def test_08h_zero_new_is_benign_exit0_no_mail(self):
        """非致命侧对照：零新增（幂等重跑 / 今天没有新通知）⇒ exit 0 + 只记 warn，不发信。

        「把『没新通知』判成失败」会让真故障被假警报淹掉 —— 这正是 R-4 的同类误报。"""
        db = WORK / "r2_zeronew.db"
        _rm(db)
        data = self._batch("zn", 5)
        fake = lambda c, p, **k: {"items": data, "total": 5, "page": p}  # noqa: E731
        code1, out1, err1, mail1, _ = self._fetch_probe(db, 3, fake_list=fake)
        code2, out2, err2, mail2, _ = self._fetch_probe(db, 3, fake_list=fake)
        p1, p2 = json.loads(out1), json.loads(out2)
        self.assertEqual(code1, 0, f"首轮应 exit 0；stderr={err1}")
        self.assertEqual(p1["new"], 5, "首轮应全部新增")
        self.assertEqual(mail1, [], "首轮正常运行不得发失败邮件")
        self.assertEqual(code2, 0, f"零新增必须 exit 0；stderr={err2}")
        self.assertEqual(p2["new"], 0, "第二次必须零新增（幂等）")
        self.assertEqual(mail2, [], "零新增不得发失败邮件")
        self.assertTrue(
            any(a["kind"] == "zero-new" and a["severity"] == "warn" for a in p2["anomalies"]),
            f"零新增须显式记 warn：{p2['anomalies']}",
        )
        print(f"\n[R2-ZERONEW] 首轮 new={p1['new']} exit={code1}；二次 new={p2['new']} exit={code2} stop={p2['stop_reason']}")

    # ------------------------------------------------ D-3 关闭（t10 修复后由红灯探针转正）
    def test_08i_missing_required_field_must_be_fatal(self):
        """D-3 关闭：契约 ⑤ 把「列表项缺必需字段」列在致命侧（exit 3 + 失败邮件）。

        修复前（同 test_08d 第 4 例；见 verify_notes §15 D-3）：``store.upsert_items`` 对缺
        ``id`` 的条目**静默 continue** —— 既无 exit 3、也无失败邮件、连 anomaly 都没有，只留下
        一条零新增 warn。后果是站点侧一旦改字段名（如 ``id`` → ``noticeId``），整份日报会静默
        变空而无人告警。

        t10 修复后：``upsert_items`` 经 ``skip_sink`` 上报跳过计数，``cmd_fetch`` 见「第 1 页
        原始 items 非空却无一项可用」即记 ``schema-drift``/error —— exit 3 + 失败邮件 +
        ``stop_reason=page-1-schema-drift``，不再与「今天零新增」混淆。本方法的断言体自
        expectedFailure 探针阶段起**未作任何改动**，只是摘掉了装饰器。
        """
        db = WORK / "r2_missing_strict.db"
        _rm(db)
        code, out, err, mail, meta = self._fetch_probe(
            db,
            2,
            fake_list=lambda c, p, **k: {"items": [{"title": "缺 id 的条目"}], "total": 1, "page": p},
        )
        payload = json.loads(out)
        rows = rows_in(db, "items")
        print(
            f"\n[R2-D3] exit={code} new={payload['new']} stop={payload['stop_reason']} rows={rows} "
            f"failure_mail={len(mail)} anomalies={sorted((a['kind'], a['severity']) for a in payload['anomalies'])}"
        )
        self.assertEqual(code, 3, "缺必需字段的列表项属致命侧，应 exit 3")
        self.assertTrue(mail, "致命侧必须触发失败邮件")


# =========================================================================
# round 3（t11）：D-3 修复的独立复验
#
# 判据只取**外部可观测行为**：进程退出码、SQLite 行数、JSON 输出字段
# （stop_reason / skipped_items / per_page / anomalies 的 kind+severity）、
# 失败邮件是否被触发、stderr 是否出现 TypeError。
# 不读源码文本、不用实现字符串作判据、不假设任何固定页数。
#
# 边界打桩仅两处，均在最底层：`fetch_mod.fetch_list`（网络边界，喂夹具）
# 与 `cli_mod.report_failure`（观测是否*尝试*发失败邮件）。
# =========================================================================


def _fetch_probe_r3(db: Path, pages: int, fake_list) -> tuple:
    """驱动**真实** ``cmd_fetch``：只替换网络边界 ``fetch_list``。

    返回 ``(exit_code, stdout, stderr, mail_calls, meta)``。
    """
    calls: list[int] = []
    original_list = fetch_mod.fetch_list

    def counting_list(campus, page, **kwargs):
        calls.append(page)
        return fake_list(campus, page, **kwargs)

    fetch_mod.fetch_list = counting_list
    try:
        with _failure_mail_spy() as mail:
            code, out, err = run_cli(
                ["fetch", "--campus", "thu", "--pages", str(pages), "--db", str(db)]
            )
        return code, out, err, list(mail), {"list_calls": len(calls)}
    finally:
        fetch_mod.fetch_list = original_list


class Test09Round3Closure(unittest.TestCase):
    """D-3 复验：缺 ``id`` 的列表项必须外部可观测，且不得与「零新增」混淆。"""

    def setUp(self):
        self.dir = WORK / "t11"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _probe(self, name: str, pages: int, fake_list):
        db = self.dir / name
        _rm(db)
        return (db,) + _fetch_probe_r3(db, pages, fake_list)

    # ------------------------------------------------------------------ ①
    def test_09a_all_items_missing_id_is_fatal_not_silent(self):
        """整页列表项全缺 ``id``：原始 items 非空却零可用 → exit 3 + 失败邮件。"""
        db, code, out, err, mail, meta = self._probe(
            "r3_all_missing.db",
            2,
            lambda c, p, **k: {"items": [{"title": "缺 id 的条目"}], "total": 1, "page": p},
        )
        payload = json.loads(out)
        rows = rows_in(db, "items")
        kinds = sorted((a["kind"], a["severity"]) for a in payload["anomalies"])
        print(
            f"\n[R3-D3-FULL] exit={code} new={payload['new']} known={payload['known']} "
            f"skipped_items={payload['skipped_items']} stop={payload['stop_reason']} rows={rows} "
            f"failure_mail={len(mail)} per_page={payload['per_page']} anomalies={kinds}"
        )
        self.assertEqual(code, 3, "整页零可用必须判致命（exit 3）")
        self.assertTrue(mail, "致命侧必须触发失败邮件")
        self.assertEqual(payload["stop_reason"], "page-1-schema-drift")
        self.assertIn(("schema-drift", "error"), kinds)
        self.assertEqual(rows, 0, "零可用页不得写入任何行")
        self.assertGreaterEqual(payload["skipped_items"], 1, "跳过条数必须出现在机器可读输出里")
        self.assertEqual(payload["per_page"][0]["skipped"], 1, "逐页统计必须带 skipped")
        self.assertGreaterEqual(meta["list_calls"], 1)

    # ------------------------------------------------------------------ ①
    def test_09b_partial_missing_id_keeps_rows_and_reports_skip(self):
        """同页混合：正常条目照常入库，缺 ``id`` 的条目被计数并可见。"""
        db, code, out, err, mail, meta = self._probe(
            "r3_partial.db",
            1,
            lambda c, p, **k: {
                "items": [{"id": "t11:ok:1", "title": "正常条目"}, {"title": "缺 id 的条目"}],
                "total": 2,
                "page": p,
            },
        )
        payload = json.loads(out)
        rows = rows_in(db, "items")
        kinds = sorted((a["kind"], a["severity"]) for a in payload["anomalies"])
        print(
            f"\n[R3-D3-PARTIAL] exit={code} new={payload['new']} rows={rows} "
            f"skipped_items={payload['skipped_items']} per_page={payload['per_page']} "
            f"anomalies={kinds} failure_mail={len(mail)}"
        )
        self.assertEqual(code, 0, "部分跳过属可疑但已处理，不判失败")
        self.assertEqual(payload["new"], 1)
        self.assertEqual(rows, 1, "正常条目必须入库（行数 == 正常条数）")
        self.assertEqual(payload["skipped_items"], 1, "跳过条数必须在 JSON 里可读")
        self.assertEqual(payload["per_page"][0]["skipped"], 1)
        self.assertIn(("item-missing-id", "warn"), kinds)
        self.assertFalse(mail, "部分跳过不触发失败邮件")

    # ------------------------------------------------------------------ ①
    def test_09c_genuinely_zero_new_is_distinguishable(self):
        """真·零新增（幂等重跑）不带 error 级 anomaly、不发失败邮件 —— 与 09a 签名可分。"""
        db = self.dir / "r3_zero_new.db"
        _rm(db)
        batch = {"items": [{"id": "t11:zero:1", "title": "既有条目"}], "total": 1, "page": 1}
        code1, out1, _, mail1, _ = _fetch_probe_r3(db, 2, lambda c, p, **k: dict(batch, page=p))
        code2, out2, _, mail2, _ = _fetch_probe_r3(db, 2, lambda c, p, **k: dict(batch, page=p))
        p1, p2 = json.loads(out1), json.loads(out2)
        rows = rows_in(db, "items")
        err2 = [a["kind"] for a in p2["anomalies"] if a["severity"] == "error"]
        print(
            f"\n[R3-D3-CONTROL] 首轮 exit={code1} new={p1['new']}；二次 exit={code2} new={p2['new']} "
            f"skipped_items={p2['skipped_items']} stop={p2['stop_reason']} rows={rows} "
            f"error级anomaly={err2} failure_mail={len(mail2)}"
            f" ｜ 对照（09a 全缺 id）：exit=3 error级=['schema-drift'] skipped_items>=1 有失败邮件"
        )
        self.assertEqual(code1, 0)
        self.assertEqual(p1["new"], 1)
        self.assertEqual(code2, 0)
        self.assertEqual(p2["new"], 0, "二次抓取必须零新增（幂等）")
        self.assertEqual(rows, 1, "幂等重跑不得增行")
        self.assertEqual(p2["skipped_items"], 0, "零新增场景不得出现跳过计数")
        self.assertFalse(err2, "零新增不得带 error 级 anomaly")
        self.assertFalse(mail2, "零新增不得触发失败邮件")

    # ------------------------------------------------------------------ ④
    def test_09d_enrich_structured_detail_has_no_typeerror(self):
        """R-1 回归：结构化时间命中路径不得再出现 ``'int' object is not callable``。"""
        db = self.dir / "r3_enrich.db"
        _rm(db)
        store = Store(db)
        try:
            store.upsert_items(
                [
                    {
                        "id": "t11:enrich:1",
                        "title": "讲座：AI 与法律推理",
                        "category": "lecture",
                        "intent_group": "activity",
                        "source_name": "验证夹具源",
                        "published_at": "2026-10-08T10:00:00+08:00",
                    }
                ]
            )
        finally:
            store.close()
        detail = {
            "id": "t11:enrich:1",
            "title": "讲座：AI 与法律推理",
            "content": "正文……",
            "event_start": "2026-10-20T19:00:00+08:00",
            "event_end": "2026-10-20T21:00:00+08:00",
            "ai_event_time": "10月20日 19:00-21:00",
            "ai_event_location": "法律图书馆 报告厅",
            "category": "讲座活动",
            "source_name": "验证夹具源",
            "published_at": "2026-10-08T10:00:00+08:00",
            "detail_status": "complete",
            "body_status": "ok",
        }

        def fake_detail(campus, item_id, session=None, **kw):
            return dict(detail, id=item_id)

        patched = _patch_detail(fake_detail)
        try:
            code, out, err = run_cli(["enrich", "--db", str(db), "--limit", "5"])
        finally:
            _unpatch_detail(patched)
        stats = json.loads(out)
        parsed_ok = int(stats.get("structured_time") or 0) + int(stats.get("text_only") or 0)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT detail_status, body_status FROM items WHERE id = 't11:enrich:1'"
            ).fetchone()
        finally:
            conn.close()
        print(
            f"\n[R3-R1] exit={code} fetched={stats['fetched']} failed={stats['failed']} "
            f"errors={stats['errors']} structured_time={stats['structured_time']} "
            f"text_only={stats['text_only']} no_time={stats['no_time']} "
            f"detail_status={row[0]} body_status={row[1]} TypeError_in_stderr={'TypeError' in err}"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("TypeError", err, "R-1 命名的遮蔽回归必须绝迹")
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["fetched"], 1)
        self.assertGreaterEqual(parsed_ok, 1, "时间解析必须至少命中一条（否则是解析失效）")
        self.assertEqual(row[0], "complete", "详情载荷自带的 detail_status 必须落库")
        self.assertEqual(row[1], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ======================================= 4. 投递链不变式复验（t14 / round 4）
class Test10Round4DeliveryChain(unittest.TestCase):
    """t14 独立复验：ICS 定时/全天混合不变式 · 幂等闸门（SMTP 实调计数）· 投递窗口语义。

    判据一律取**外部可观测事实**：SMTP 实际调用次数、回执文件、stdout/stderr 原文、
    ICS 字节里的行号与计数。**不使用台账行数**（``sends.date`` 是 PRIMARY KEY，
    UPSERT 之后行数恒为 1，用它当门禁证据会永远「通过」）。
    """

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="nd_r4_chain_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = fake_env_file(self.tmp / ".env")
        self.db = self.tmp / "r4.db"
        self.receipts = self.tmp / "send-receipts"
        self.cfg = make_cfg(self.db, self.env_file)

        # 明确指定回执目录与 SMTP 目标，避免依赖进程环境里可能残留的值
        self._saved_env = {}
        for key, value in {
            "NOTICE_DIGEST_LEDGER": str(self.receipts),
            "ND_SMTP_HOST": "smtp.t3-verify.invalid",
            "ND_SMTP_PORT": "465",
            "ND_SMTP_USER": "t3-verify-user",
            "ND_SMTP_PASS": "t3-verify-placeholder",
            "ND_TO_ADDR": "t3-verify-to@t3-verify.invalid",
            "ND_FROM_ADDR": "t3-verify-from@t3-verify.invalid",
        }.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------------------------------------------------------------- 工具
    def _item(self, item_id, title, published_at):
        return envelope_of(
            {"id": item_id, "title": title, "published_at": published_at}
        )

    def _seed(self, items):
        store = Store(self.db)
        try:
            store.init_schema()
            added, _skipped = store.upsert_items(items)
        finally:
            store.close()
        return added

    def _send(self):
        """跑一次非 dry-run 的 cmd_send，返回 (exit_code, summary, stdout, stderr)。"""
        import types

        args = types.SimpleNamespace(db=str(self.db), top=None, to=None, dry_run=False)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.cmd_send(self.cfg, args)
        text = out.getvalue().strip().splitlines()
        summary = json.loads(text[-1]) if text else {}
        return code, summary, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def _smtp_spy(self):
        """在 mailer 的 SMTP 边界打桩，只记调用次数与真正投递出去的报文。"""
        from unittest import mock

        sent: list = []

        class _SpySMTP:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self, *args, **kwargs):
                return (250, b"ok")

            def starttls(self, *args, **kwargs):
                return (220, b"ok")

            def login(self, *args, **kwargs):
                return (235, b"ok")

            def send_message(self, message):
                sent.append(message)
                return {}

            def quit(self):
                return (221, b"bye")

        with mock.patch.object(mailer_mod.smtplib, "SMTP_SSL", _SpySMTP), mock.patch.object(
            mailer_mod.smtplib, "SMTP", _SpySMTP
        ):
            yield sent

    @staticmethod
    def _html_of(message):
        """从 EmailMessage 里取出 text/html 正文（multipart 时逐段找）。"""
        parts = []
        for part in message.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload is None:
                    parts.append(str(part.get_payload()))
                else:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts) if parts else str(message.get_payload())

    def _receipt_count(self):
        return len(list(self.receipts.glob("*.json"))) if self.receipts.exists() else 0

    # ------------------------------------------------- 10a：混合日历 ICS 不变式
    def test_10a_ics_mixed_timed_and_all_day_invariants(self):
        """定时 + 全天混在一份 ICS 里时，作用域化不变式必须全过，且违规计数为 0。"""
        scored, _parsed, cfg, now = render_mod.load_fixture()
        base = scored[0].item
        timed_item = dict(base, id="r4:timed", title="R4 定时活动")
        # is_all_day() 的依据是 start/end 的钟点 **加上** 原文线索
        # （p.evidence 与 item 的 ai_event_time / ai_time_text / ai_time_evidence /
        #  event_time_text / time_text）；fixture 条目自带的原文里有钟点，
        # 不清掉就会被判成定时事件，全天行断言（VALUE=DATE）也就无从成立。
        allday_item = dict(
            base,
            id="r4:allday",
            title="R4 全天活动",
            ai_event_time=None,
            ai_time_text=None,
            ai_time_evidence=None,
            event_time_text=None,
            time_text=None,
        )

        timed_start = datetime(2026, 10, 10, 14, 0)
        allday_start = datetime(2026, 10, 10)
        parsed_map = {
            "r4:timed": ParsedTime(
                start=timed_start,
                end=timed_start + timedelta(hours=2),
                deadline=None,
                bucket="next_week",
                evidence="10月10日 14:00",
            ),
            "r4:allday": ParsedTime(
                start=allday_start,
                end=None,
                deadline=None,
                bucket="next_week",
                evidence="10月10日",
            ),
        }
        scored_map = [
            score_mod.Scored(item=timed_item, score=1.0, reasons=[]),
            score_mod.Scored(item=allday_item, score=1.0, reasons=[]),
        ]

        ics = render_mod.render_ics(scored_map, parsed_map, cfg, now)
        raw = ics.encode("utf-8")
        problems = validate_ics_bytes(raw)
        self.assertEqual(
            problems, [], f"[R4-ICS] 混合日历不变式应全过，实际：{problems}"
        )

        text = raw.decode("utf-8")
        n_vevent = text.count("BEGIN:VEVENT")
        n_valarm = text.count("BEGIN:VALARM")
        n_vtz = text.count("BEGIN:VTIMEZONE")
        self.assertEqual((n_vevent, n_valarm, n_vtz), (2, 2, 1))
        self.assertEqual(text.count("1970"), 0, "[R4-ICS] 不得出现 1970 纪元兜底")

        # DTSTART/DTEND 只统计 **VEVENT 作用域内** 的行：VTIMEZONE 里
        # STANDARD/DAYLIGHT 的 DTSTART 按 RFC 5545 §3.6.5 必须是本地时间、不带 TZID，
        # 把它算进「定时事件行」既是计数错误，也会把合规输出误判成违规。
        vevent_blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.DOTALL)
        self.assertEqual(len(vevent_blocks), 2, "[R4-ICS] VEVENT 块数")
        timed_lines, allday_lines, dtend_lines = [], [], []
        for blk in vevent_blocks:
            for ln in blk.splitlines():
                if ln.startswith("DTSTART"):
                    (allday_lines if "VALUE=DATE" in ln else timed_lines).append(ln)
                elif ln.startswith("DTEND"):
                    dtend_lines.append(ln)
        self.assertEqual(len(timed_lines), 1, f"[R4-ICS] 定时 DTSTART：{timed_lines}")
        self.assertEqual(len(allday_lines), 1, f"[R4-ICS] 全天 DTSTART：{allday_lines}")
        self.assertEqual(len(dtend_lines), 2, f"[R4-ICS] DTEND：{dtend_lines}")
        self.assertIn("TZID=Asia/Shanghai", timed_lines[0])
        self.assertIn("20261010T140000", timed_lines[0])
        self.assertNotIn("TZID=", allday_lines[0])
        self.assertIn("DTEND;VALUE=DATE:20261011", text)
        self.assertIn("TRIGGER;VALUE=DATE-TIME:20261009T233000Z", text)
        self.assertIn("TRIGGER:-PT30M", text)

        # 就地反证：把定时 DTSTART 的 TZID 摘掉，修正后的断言必须变红并**指名那一行**
        mutated = text.replace("DTSTART;TZID=Asia/Shanghai:20261010T140000", "DTSTART:20261010T140000", 1)
        self.assertNotEqual(mutated, text, "[R4-ICS] 反证注入未命中目标行")
        mutated_problems = validate_ics_bytes(mutated.encode("utf-8"))
        self.assertTrue(mutated_problems, "[R4-ICS] 摘掉定时 DTSTART 的 TZID 后断言竟然还是绿的")
        self.assertTrue(
            any("DTSTART:20261010T140000" in p and "定时" in p for p in mutated_problems),
            f"[R4-ICS] 反证必须指名违规行：{mutated_problems}",
        )

        # 全天行带 TZID 也必须被判违规（§3.3.5 反向保护）
        bad_all_day = text.replace(
            "DTSTART;VALUE=DATE:20261010", "DTSTART;VALUE=DATE;TZID=Asia/Shanghai:20261010", 1
        )
        bad_problems = validate_ics_bytes(bad_all_day.encode("utf-8"))
        self.assertTrue(
            any("全天 DTSTART 不得带 TZID" in p for p in bad_problems),
            f"[R4-ICS] 全天行带 TZID 必须判违规：{bad_problems}",
        )

    # ------------------------------------------- 10b：幂等闸门（SMTP 实调计数）
    def test_10b_idempotent_gate_counts_real_smtp_calls(self):
        """同一天同内容重跑：SMTP 调用 1 → 1；内容变化：1 → 2；attempt/sent 行序正确。"""
        now = datetime.now(timezone(timedelta(hours=8)))
        # 窗口判据实测为「严格晚于上次成功投递的时刻」（探针：items_published_after(last_sent_at)
        # 只返回未来条目）⇒ 想让第二次跑仍重渲同一份内容、从而命中内容级闸门，条目必须落在未来。
        published = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        self._seed([self._item("r4:gate:1", "R4 门禁条目甲", published)])

        with self._smtp_spy() as sent:
            code1, summary1, _out1, err1 = self._send()
            count_after_first = len(sent)
            code2, summary2, _out2, err2 = self._send()
            count_after_second = len(sent)
            gate_lines = [ln for ln in err2.splitlines() if "幂等闸门" in ln]

            # 内容变化：多一条新条目 → 指纹变 → 允许再投递
            self._seed([self._item("r4:gate:2", "R4 门禁条目乙（新增）", published)])
            code3, summary3, _out3, err3 = self._send()
            count_after_third = len(sent)

        self.assertEqual(code1, 0, f"[R4-GATE] 首投退出码应 0：{err1}")
        self.assertEqual(count_after_first, 1, f"[R4-GATE] 首投应实调 SMTP 1 次：{summary1}")
        self.assertTrue(summary1.get("sent"), f"[R4-GATE] 首投 sent 应为 True：{summary1}")

        self.assertEqual(code2, 0, f"[R4-GATE] 闸门命中仍应退出码 0：{err2}")
        self.assertEqual(
            count_after_second, 1, f"[R4-GATE] 闸门命中后 SMTP 调用数不得增加：{summary2}"
        )
        self.assertTrue(summary2.get("gated"), f"[R4-GATE] 第二次应标记 gated：{summary2}")
        self.assertFalse(summary2.get("sent"), f"[R4-GATE] 闸门命中 sent 应为 False：{summary2}")
        self.assertNotIn("[mailer] 已投递", err2, "[R4-GATE] 闸门命中不得出现投递成功日志")
        self.assertTrue(gate_lines, f"[R4-GATE] 必须有闸门专用日志：{err2}")
        fp = summary1.get("fingerprint", "")
        self.assertRegex(fp, r"^[0-9a-f]{32}$", f"[R4-GATE] 指纹形态：{fp}")
        self.assertIn(fp, gate_lines[0], f"[R4-GATE] 闸门日志必须带本次内容指纹：{gate_lines[0]}")

        self.assertEqual(
            count_after_third, 2, f"[R4-GATE] 内容变化后应再投递 1 次（总 2）：{summary3}"
        )
        self.assertNotEqual(summary3.get("fingerprint"), fp, "[R4-GATE] 内容变化指纹必须变")

        conn = sqlite3.connect(str(self.db))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(send_attempts)")]
            attempts = conn.execute("SELECT * FROM send_attempts").fetchall()
        finally:
            conn.close()
        self.assertTrue(attempts, f"[R4-GATE] 必须有 attempt 行落盘（先落盘、后发信）：cols={cols}")
        if "sent" in cols:
            idx = cols.index("sent")
            n_sent = sum(1 for row in attempts if int(row[idx] or 0) == 1)
            self.assertEqual(
                n_sent, 2, f"[R4-GATE] 只有真正投递成功的两轮才应升级为 sent：cols={cols} rows={attempts}"
            )
        self.assertEqual(self._receipt_count(), 2, "[R4-GATE] 两份不同内容应各留一张回执")

    # -------------------------------------------------- 10c：投递窗口语义
    def test_10c_window_semantics_only_counts_new_items(self):
        """窗口 = 上次成功投递之后：空窗口不发信（哨兵），新增 1 条只算 1 条。"""
        now = datetime.now(timezone(timedelta(hours=8)))
        old_at = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        fresh_at = (now + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        self._seed(
            [
                self._item("r4:win:old1", "R4 窗口旧条目一", old_at),
                self._item("r4:win:old2", "R4 窗口旧条目二", old_at),
            ]
        )

        with self._smtp_spy() as sent:
            code1, summary1, _out1, err1 = self._send()
            n_first = len(sent)
            body1 = self._html_of(sent[0]) if sent else None

            # 纯旧库：本轮窗口为空 ⇒ 空主题哨兵，不发信、退出码 0
            code2, summary2, _out2, err2 = self._send()
            n_second = len(sent)

            # 新增 1 条 → 只算这 1 条，旧的 2 条不得重复计数
            self._seed([self._item("r4:win:new1", "R4 窗口新条目", fresh_at)])
            code3, summary3, _out3, err3 = self._send()
            n_third = len(sent)
            body3 = self._html_of(sent[-1]) if len(sent) > n_second else None

        self.assertEqual(code1, 0, f"[R4-WINDOW] 首投退出码应 0：{err1}")
        self.assertEqual(n_first, 1, f"[R4-WINDOW] 首投应发 1 封：{summary1}")
        self.assertEqual(summary1.get("n_items"), 2, f"[R4-WINDOW] 首投窗口内 2 条：{summary1}")
        self.assertIsNone(summary1.get("window_since"), "[R4-WINDOW] 首次投递窗口应为 None")

        self.assertEqual(code2, 0, f"[R4-WINDOW] 空窗口应退出码 0（不是失败）：{err2}")
        self.assertEqual(n_second, 1, f"[R4-WINDOW] 空窗口不得再发信：{summary2}")
        self.assertFalse(summary2.get("sent"), f"[R4-WINDOW] 空窗口 sent 必须 False：{summary2}")
        self.assertIsNotNone(summary2.get("window_since"), "[R4-WINDOW] 第二轮必须带上上次投递时间")
        self.assertIn("空主题哨兵", err2, f"[R4-WINDOW] 必须有哨兵日志：{err2}")
        self.assertNotIn("[mailer] 已投递", err2, "[R4-WINDOW] 空窗口不得出现投递成功日志")

        self.assertEqual(n_third, 2, f"[R4-WINDOW] 新增条目后应再发 1 封：{summary3}")
        self.assertEqual(summary3.get("n_items"), 1, f"[R4-WINDOW] 只算窗口内新增：{summary3}")
        for payload in (body1, body3):
            self.assertIsNotNone(payload, "[R4-WINDOW] 抓不到已投递报文")
        self.assertEqual(
            str(body1).count("data-nd-item="), 2, "[R4-WINDOW] 首封信体应含 2 条"
        )
        self.assertEqual(
            str(body3).count("data-nd-item="), 1, "[R4-WINDOW] 第三封信体只应含 1 条"
        )
        self.assertIn("R4 窗口新条目", str(body3), "[R4-WINDOW] 新条目必须出现在信体")
        self.assertNotIn("R4 窗口旧条目", str(body3), "[R4-WINDOW] 旧条目不得被重复计数")


# =========================================================================== #
# t15 追加段（append-only）：幂等键必须是内容的纯函数 · NULL 时间行必须有单调游标
#
# 本节**只追加**，不改动上方任何既有断言（尤其 test_01c / test_10a / test_09a / test_09b
# 以及 Test10 的 R4-GATE / R4-WINDOW）。两个回归点对应 t13 实测的两个漏口：
#   F1  指纹把页脚渲染时刻算进了内容 ⇒ 同一天跨分钟重跑，两道闸门同时失配（+61s 真的发第二封）；
#   F2  published_ts IS NULL 的行永不受窗口约束 ⇒ 同日重跑无上界重复投递。
# 计数一律取外部可观测事实：SMTP 实调次数、stdout/stderr 原文、指纹本身。
# =========================================================================== #


def _t15_mini_html(footer_stamp: str = "2026-10-08 09:15", body_time: str = "2026-10-10 14:00") -> str:
    """构造一封最小可辨识的邮件 HTML：页脚带渲染时刻、正文带一条活动时间。"""
    return (
        '<li class="nd-item" data-nd-item="1">讲座：模拟法庭（{body}）</li>'
        '<div class="nd-footer">由 notice-digest 生成于 {footer}（Asia/Shanghai）</div>'
    ).format(body=body_time, footer=footer_stamp)


class Test11IdempotencyKeyPurity(unittest.TestCase):
    """t15 回归：指纹只随**内容**变化；NULL 时间行只随**首次见到时刻**开窗。"""

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="nd_t15_chain_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = fake_env_file(self.tmp / ".env")
        self.db = self.tmp / "t15.db"
        self.receipts = self.tmp / "send-receipts"
        self.cfg = make_cfg(self.db, self.env_file)

        self._saved_env = {}
        for key, value in {
            "NOTICE_DIGEST_LEDGER": str(self.receipts),
            "ND_SMTP_HOST": "smtp.t15-verify.invalid",
            "ND_SMTP_PORT": "465",
            "ND_SMTP_USER": "t15-verify-user",
            "ND_SMTP_PASS": "t15-verify-placeholder",
            "ND_TO_ADDR": "t15-verify-to@t15-verify.invalid",
            "ND_FROM_ADDR": "t15-verify-from@t15-verify.invalid",
        }.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------------------------------------------------------------- 工具
    def _item(self, item_id, title, published_at):
        return envelope_of(
            {"id": item_id, "title": title, "published_at": published_at}
        )

    def _seed(self, items):
        store = Store(self.db)
        try:
            store.init_schema()
            added, _skipped = store.upsert_items(items)
        finally:
            store.close()
        return added

    def _send(self):
        """跑一次非 dry-run 的 cmd_send，返回 (exit_code, summary, stdout, stderr)。"""
        import types

        args = types.SimpleNamespace(db=str(self.db), top=None, to=None, dry_run=False)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.cmd_send(self.cfg, args)
        text = out.getvalue().strip().splitlines()
        summary = json.loads(text[-1]) if text else {}
        return code, summary, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def _smtp_spy(self):
        """在 mailer 的 SMTP 边界打桩，只记调用次数与真正投递出去的报文。"""
        from unittest import mock

        sent: list = []

        class _SpySMTP:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self, *args, **kwargs):
                return (250, b"ok")

            def starttls(self, *args, **kwargs):
                return (220, b"ok")

            def login(self, *args, **kwargs):
                return (235, b"ok")

            def send_message(self, message):
                sent.append(message)
                return {}

            def quit(self):
                return (221, b"bye")

        with mock.patch.object(mailer_mod.smtplib, "SMTP_SSL", _SpySMTP), mock.patch.object(
            mailer_mod.smtplib, "SMTP", _SpySMTP
        ):
            yield sent

    @staticmethod
    def _html_of(message):
        parts = []
        for part in message.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload is None:
                    parts.append(str(part.get_payload()))
                else:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts) if parts else str(message.get_payload())

    def _row(self, item_id):
        """直接读 items 表的原始列。

        成品映射层 `Store.all_items()` 只暴露展示字段，`published_ts` /
        `first_seen_at` 不在其中，所以这里绕过映射层读原始行。
        """
        conn = sqlite3.connect(str(self.db))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _receipt_count(self):
        """回执（ledger 目录）里的回执文件数 —— mailer 同日幂等闸门的落盘依据。"""
        if not self.receipts.exists():
            return 0
        return len(list(self.receipts.glob("*.json")))

    # ------------------------------------------------- 11a：指纹 = 内容的纯函数
    def test_11a_fingerprint_is_pure_function_of_content(self):
        """F1 单元级：页脚渲染时刻必须被归一化掉，正文时间差必须留在指纹里。"""
        subj = "清华通知日报 10-08｜新增 1 条"
        base = _t15_mini_html("2026-10-08 09:15")
        later = _t15_mini_html("2026-10-08 09:16")

        # 前提：两封 HTML 确实不同（只有页脚渲染时刻不同），否则本用例是空转
        self.assertNotEqual(base, later, "[T15-F1] 构造前提：两封 HTML 必须真的不同（只差页脚）")
        self.assertEqual(
            mailer_mod.send_fingerprint(subj, 1, base),
            mailer_mod.send_fingerprint(subj, 1, later),
            "[T15-F1] 页脚渲染时刻不得进入指纹：同内容跨分钟必须得同一指纹",
        )
        self.assertRegex(
            mailer_mod.send_fingerprint(subj, 1, base),
            r"^[0-9a-f]{32}$",
            "[T15-F1] 指纹形态必须仍是 32 位十六进制",
        )

        # 反向：正文里的活动时间属于内容，差一分钟必须换指纹（否则新内容会被旧指纹吞掉）
        self.assertNotEqual(
            mailer_mod.send_fingerprint(subj, 1, _t15_mini_html("2026-10-08 09:15", "2026-10-10 14:00")),
            mailer_mod.send_fingerprint(subj, 1, _t15_mini_html("2026-10-08 09:15", "2026-10-10 15:00")),
            "[T15-F1] 正文时间文本差异必须改变指纹（归一化不得过宽）",
        )
        # 反向：条数与主题仍必须进指纹
        self.assertNotEqual(
            mailer_mod.send_fingerprint(subj, 1, base),
            mailer_mod.send_fingerprint(subj, 2, base),
            "[T15-F1] 条数必须进指纹",
        )
        self.assertNotEqual(
            mailer_mod.send_fingerprint(subj, 1, base),
            mailer_mod.send_fingerprint(subj + "（续）", 1, base),
            "[T15-F1] 主题必须进指纹",
        )

    # ------------------------------------------------- 11b：跨分钟重跑不得再发信
    def test_11b_cross_minute_rerun_of_same_content_must_not_resend(self):
        """F1 端到端：同一份内容 +61s 重跑，必须被幂等闸门拦下（t13 实测的真漏口）。"""
        from unittest import mock

        real_now = store_mod.now_shanghai()
        if real_now.hour == 23 and real_now.minute >= 55:
            self.skipTest("临近午夜：跨分钟用例会跨天（day/回执日期都变），跳过以免假红")

        # 前提证明：同一份评分结果，渲染时刻差 61s ⇒ 原始 HTML 必然不同（%H:%M 至少进一位）
        scored, parsed_map, cfg_fixture, _now = render_mod.load_fixture()
        _s_a, html_a, _ics_a = render_mod.render_email(scored, parsed_map, cfg_fixture, real_now)
        _s_b, html_b, _ics_b = render_mod.render_email(
            scored, parsed_map, cfg_fixture, real_now + timedelta(seconds=61)
        )
        self.assertNotEqual(
            html_a, html_b, "[T15-F1] 前提：渲染时刻差 61s 时原始 HTML 必须不同（否则用例空转）"
        )

        # 条目发布时间放在未来 ⇒ 两轮都落在窗口内，第二轮才是真的"同一份内容重跑"
        fresh = (real_now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        self.assertEqual(
            self._seed([self._item("t15:f1:1", "T15 跨分钟条目", fresh)]),
            1,
            "[T15-F1] 前置：条目应落库",
        )

        clock = [real_now]

        def _fake_now():
            return clock[0]

        # 只替换时钟（被验证逻辑本身不动）：模拟"同一份内容的两次运行之间过了 61 秒"
        with mock.patch.object(cli_mod, "now_shanghai", _fake_now), self._smtp_spy() as sent:
            code1, s1, _o1, e1 = self._send()
            n1 = len(sent)
            body1 = self._html_of(sent[0]) if sent else None

            clock[0] = real_now + timedelta(seconds=61)
            code2, s2, _o2, e2 = self._send()
            n2 = len(sent)
            gate_lines = [ln for ln in e2.splitlines() if "幂等闸门" in ln]

        self.assertEqual(code1, 0, f"[T15-F1] 首投退出码应 0：{e1}")
        self.assertEqual(n1, 1, f"[T15-F1] 首投应实调 SMTP 1 次：{s1}")
        self.assertTrue(s1.get("sent"), f"[T15-F1] 首投 sent 应为 True：{s1}")
        self.assertIn("T15 跨分钟条目", str(body1), "[T15-F1] 首封信体应含该条目")

        self.assertEqual(code2, 0, f"[T15-F1] 跨分钟重跑仍应退出码 0：{e2}")
        self.assertEqual(
            n2, 1, f"[T15-F1] 同一份内容跨分钟重跑不得再调 SMTP（t13 漏口：原为 2）：{s2}"
        )
        self.assertFalse(s2.get("sent"), f"[T15-F1] 第二轮 sent 必须为 False：{s2}")
        self.assertTrue(s2.get("gated"), f"[T15-F1] 第二轮必须标记 gated：{s2}")
        self.assertEqual(
            s2.get("fingerprint"),
            s1.get("fingerprint"),
            "[T15-F1] 同内容跨分钟必须得到同一指纹（改动点就在此处）",
        )
        self.assertTrue(gate_lines, f"[T15-F1] 必须有闸门专用日志：{e2}")
        self.assertNotIn("[mailer] 已投递", e2, "[T15-F1] 闸门命中不得出现投递成功日志")
        self.assertEqual(self._receipt_count(), 1, "[T15-F1] 只应留一张回执")

    # ------------------------------------------------- 11c：NULL 时间行的窗口语义
    def test_11c_null_row_same_day_rerun_is_gated_and_drops_out_across_days(self):
        """F2：NULL 行同日第 2/3 轮不得再发信；跨投递日不再进窗口。

        本实现对 NULL 行用 ``first_seen_at`` 单调游标，语义两条并列（见 store.items_published_after）：
        ① 同日重入 ⇒ 正文与上一轮相同 ⇒ 由 F1 稳定指纹在闸门处拦下（disp=0，不重复发信）；
        ② 跨投递日 ⇒ first_seen_at 早于 last_sent_at ⇒ 不再进窗口（关掉 t13-F2 的无上界重投）。
        """
        self.assertEqual(
            self._seed([self._item("t15:f2:null", "T15 无时间条目", None)]),
            1,
            "[T15-F2] 前置：无时间条目应落库",
        )
        row0 = self._row("t15:f2:null")
        self.assertIsNotNone(row0, "[T15-F2] 条目必须真的落库")
        self.assertIsNone(row0["published_ts"], "[T15-F2] 前置：published_ts 必须是 NULL")
        first_seen_before = row0["first_seen_at"]
        self.assertTrue(first_seen_before, "[T15-F2] 前置：first_seen_at 必须有值（窗口游标）")

        with self._smtp_spy() as sent:
            code1, s1, _o1, e1 = self._send()
            n1 = len(sent)
            code2, s2, _o2, e2 = self._send()
            n2 = len(sent)
            code3, s3, _o3, e3 = self._send()
            n3 = len(sent)

        self.assertEqual(code1, 0, f"[T15-F2] 首投退出码应 0：{e1}")
        self.assertEqual(n1, 1, f"[T15-F2] 首投应发 1 封：{s1}")
        self.assertEqual(s1.get("n_items"), 1, f"[T15-F2] 首投窗口内 1 条：{s1}")
        self.assertTrue(s1.get("sent"), f"[T15-F2] 首投 sent 应为 True：{s1}")

        # 同日第 2、3 轮：SMTP 调用数不得增加（t13 漏口：NULL 行原为每轮都再发一封）
        self.assertEqual(n2, 1, f"[T15-F2] 同日第 2 轮不得再调 SMTP：{s2}")
        self.assertEqual(code2, 0, f"[T15-F2] 第 2 轮应退出码 0：{e2}")
        self.assertFalse(s2.get("sent"), f"[T15-F2] 第 2 轮 sent 必须 False：{s2}")
        self.assertTrue(s2.get("gated"), f"[T15-F2] 第 2 轮必须标记 gated：{s2}")
        self.assertEqual(
            s2.get("fingerprint"),
            s1.get("fingerprint"),
            "[T15-F2] 同日同内容两轮必须同指纹（拦下靠的是纯内容函数）",
        )
        self.assertIn("幂等闸门命中", e2, f"[T15-F2] 第 2 轮必须留下闸门日志：{e2}")
        self.assertNotIn("[mailer] 已投递", e2, f"[T15-F2] 第 2 轮不得出现投递成功日志：{e2}")

        self.assertEqual(n3, 1, f"[T15-F2] 同日第 3 轮同样不得再调 SMTP：{s3}")
        self.assertEqual(code3, 0, f"[T15-F2] 第 3 轮应退出码 0：{e3}")
        self.assertFalse(s3.get("sent"), f"[T15-F2] 第 3 轮 sent 必须 False：{s3}")
        self.assertIn("幂等闸门命中", e3, f"[T15-F2] 第 3 轮必须留下闸门日志：{e3}")
        self.assertEqual(self._receipt_count(), 1, "[T15-F2] 全程只应留一张回执")

        # 不得为了让 NULL 行收口而把它丢掉（静默漏投是更严重的错）
        self.assertIsNotNone(self._row("t15:f2:null"), "[T15-F2] 条目不得被静默丢弃")
        self.assertIsNone(
            self._row("t15:f2:null")["published_ts"], "[T15-F2] published_ts 仍应为 NULL"
        )

        # 窗口语义本身：游标把 NULL 行挡在"次日"之外（跨投递日不再进窗口）
        store = Store(self.db)
        try:
            store.init_schema()
            since = store.last_sent_at()
            self.assertIsNotNone(since, "[T15-F2] 首投成功后必须有 last_sent_at")
            ids = [it["id"] for it in store.items_published_after(since)]
            self.assertIn(
                "t15:f2:null",
                ids,
                "[T15-F2] 同一投递日内 NULL 行仍应入窗（既有冻结行为：靠闸门拦重复发信）",
            )
            # 把首次见到的时间改到昨天：跨投递日必须立刻掉出窗口
            yesterday = (cli_mod.now_shanghai() - timedelta(days=1)).isoformat()
            conn = sqlite3.connect(self.db)
            try:
                conn.execute(
                    "UPDATE items SET first_seen_at=? WHERE id=?",
                    (yesterday, "t15:f2:null"),
                )
                conn.commit()
            finally:
                conn.close()
            ids2 = [it["id"] for it in store.items_published_after(since)]
            self.assertNotIn(
                "t15:f2:null",
                ids2,
                "[T15-F2] 跨投递日后 NULL 行不得再进窗口（关掉无上界重投；改动点就在这里）",
            )
            # 反向：从未成功投递过时窗口不设上界，避免首投失败后静默丢条目
            self.assertIn(
                "t15:f2:null",
                [it["id"] for it in store.items_published_after(None)],
                "[T15-F2] 从未成功投递时不得把 NULL 行丢掉",
            )
        finally:
            store.close()

        # 该收口依赖 first_seen_at 单调：重复见到同一条目不得改写它
        self.assertEqual(
            self._seed([self._item("t15:f2:null", "T15 无时间条目（重复见到）", None)]),
            0,
            "[T15-F2] 重复见到不应新增条目",
        )
        self.assertEqual(
            self._row("t15:f2:null")["first_seen_at"],
            yesterday,
            "[T15-F2] 重复 upsert 不得改写 first_seen_at（单调游标的前提）",
        )

    # ------------------------------------------------- 11d：新条目仍必须发得出去
    def test_11d_new_item_still_changes_fingerprint_and_ships(self):
        """不变式：新增条目必须改变指纹并真的投递出去（不得被自己的闸门吞掉）。"""
        real_now = store_mod.now_shanghai()
        fresh = (real_now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S+08:00")

        self._seed([self._item("t15:inv:1", "T15 不变式条目一", fresh)])
        with self._smtp_spy() as sent:
            code1, s1, _o1, e1 = self._send()
            n1 = len(sent)
            body1 = self._html_of(sent[0]) if sent else None

            self._seed([self._item("t15:inv:2", "T15 不变式条目二", fresh)])
            code2, s2, _o2, e2 = self._send()
            n2 = len(sent)
            body2 = self._html_of(sent[-1]) if len(sent) > n1 else None

        self.assertEqual(code1, 0, f"[T15-INV] 首投退出码应 0：{e1}")
        self.assertEqual(n1, 1, f"[T15-INV] 首投应发 1 封：{s1}")
        self.assertEqual(s1.get("n_items"), 1, f"[T15-INV] 首投 1 条：{s1}")

        self.assertEqual(code2, 0, f"[T15-INV] 第 2 轮退出码应 0：{e2}")
        self.assertEqual(n2, 2, f"[T15-INV] 新增条目必须真的再发 1 封：{s2}")
        self.assertTrue(s2.get("sent"), f"[T15-INV] 第 2 轮 sent 应为 True：{s2}")
        self.assertNotEqual(
            s2.get("fingerprint"),
            s1.get("fingerprint"),
            "[T15-INV] 新增条目必须改变指纹（否则新内容被闸门吞掉）",
        )
        self.assertEqual(s2.get("n_items"), 2, f"[T15-INV] 第 2 轮应算 2 条：{s2}")
        for payload in (body1, body2):
            self.assertIsNotNone(payload, "[T15-INV] 抓不到已投递报文")
        self.assertIn("T15 不变式条目二", str(body2), "[T15-INV] 新条目必须出现在信体")
