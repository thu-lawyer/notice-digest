"""t2 投递链验收测试：HTML/纯文本渲染、ICS 日历、SMTP 投递与 dry-run 落盘。

运行：``/opt/anaconda3/bin/python3 -m unittest tests.test_render -v``（项目根目录下）
"""

import contextlib
import dataclasses
import io
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from notice_digest import mailer
from notice_digest.config import Config
from notice_digest.feedback import make_token
from notice_digest.render import (
    PROJECT_ROOT,
    SEP,
    SUMMARY_LIMIT,
    html_to_text,
    load_fixture,
    render_email,
    render_ics,
    render_plain_text,
)
from notice_digest.score import Scored
from notice_digest.timeparse import ParsedTime

EXPECTED_SUBJECT = "清华通知日报 10-09\uff5c明日 3 场活动\uff0c1 项报名今日截止"
UNDATED = ParsedTime(start=None, end=None, deadline=None, bucket="undated", evidence="未给出时间")


def _items(count, **extra):
    scored, parsed = [], {}
    for index in range(count):
        item_id = f"x:{index}"
        item = {
            "id": item_id,
            "title": f"测试通知 {index}",
            "url": f"https://mp.weixin.qq.com/s/X{index}",
            "source_name": "测试来源",
            "category": "校园动态",
            "intent_group": "notice",
            "ai_summary": "测试摘要",
        }
        item.update(extra)
        scored.append(Scored(item=item, score=1.0 - index * 0.01, reasons=[("test", 1.0)]))
        parsed[item_id] = UNDATED
    return scored, parsed


class RenderFixtureTest(unittest.TestCase):
    """用 tests/fixtures/sample_scored.json 做的端到端渲染验收。"""

    @classmethod
    def setUpClass(cls):
        cls.scored, cls.parsed, cls.cfg, cls.now = load_fixture()
        cls.subject, cls.html, cls.ics = render_email(cls.scored, cls.parsed, cls.cfg, cls.now)
        cls.plain = render_plain_text(cls.scored, cls.parsed, cls.cfg, cls.now)
        cls.secret = cls.cfg.hmac_secret
        cls.base = cls.cfg.feedback_base.rstrip("/")

    # ① 主题格式

    def test_subject_matches_contract_example(self):
        self.assertEqual(self.subject, EXPECTED_SUBJECT)
        self.assertIn(SEP, self.subject)
        self.assertNotIn("|", self.subject)

    def test_subject_degenerates_to_new_count(self):
        scored, parsed = _items(12)
        no_events = dataclasses.replace(self.cfg, feedback_base="")
        subject, _, _ = render_email(scored, parsed, no_events, datetime(2026, 10, 9, 7, 30))
        self.assertEqual(subject, "清华通知日报 10-09\uff5c新增 12 条")

    def test_subject_date_is_zero_padded(self):
        scored, parsed = _items(3)
        subject, _, _ = render_email(scored, parsed, self.cfg, datetime(2026, 1, 5, 7, 30))
        self.assertEqual(subject, "清华通知日报 01-05\uff5c新增 3 条")

    # ② 分节顺序 / 不丢条目

    def test_sections_keep_fixed_order(self):
        positions = [self.html.index(mark) for mark in ("\u2460", "\u2461", "\u2462", "\u2463", "\u2464")]
        self.assertEqual(positions, sorted(positions), "五个分节的顺序必须固定为 ①②③④⑤")
        self.assertIn("今天/明天能去", self.html)
        self.assertIn("截止提醒", self.html)
        self.assertIn("本周讲座与学术", self.html)
        self.assertIn("实习就业", self.html)
        self.assertIn("其他新通知", self.html)

    def test_sections_route_items_as_designed(self):
        head = lambda title: self.html.index(title)  # noqa: E731
        self.assertIn("人工智能与法律推理前沿问题学术报告会", self.html[head("今天/明天能去"):head("截止提醒")])
        self.assertIn("国际交流项目报名通知", self.html[head("截止提醒"):head("本周讲座与学术")])
        self.assertIn("数字法治前沿系列讲座", self.html[head("本周讲座与学术"):head("实习就业")])
        self.assertIn("法务与合规实习宣讲会", self.html[head("实习就业"):head("其他新通知")])

    def test_overflow_items_are_folded_not_dropped(self):
        self.assertEqual(self.html.count('data-nd-item="1"'), 12, "12 条新通知必须全部出现在邮件里")
        self.assertIn("超出优先展示条数", self.html)
        for title in ("图书馆新增数据库试用通知", "校园网出口设备维护通知", "食堂菜品更新", "校医院门诊预约方式变更"):
            self.assertIn(title, self.html)

    # ③ 条目字段

    def test_item_links_to_signed_click_redirect(self):
        item_id = "thu:law-ai-forum-20261010"
        token = make_token(item_id, "click", self.cfg)
        # 模板里的 & 会被转义成 &amp;，比较前先还原
        html = self.html.replace("&amp;", "&")
        href = (
            f"{self.base}/nd/c?i=thu%3Alaw-ai-forum-20261010"
            f"&id=thu%3Alaw-ai-forum-20261010&t={token}"
        )
        self.assertIn(f'href="{href}"', html)
        self.assertNotIn('href="https://mp.weixin.qq.com/s/AAA1111"', self.html)

    def test_feedback_buttons_are_hmac_signed(self):
        item_id = "thu:civil-code-lecture-20261010"
        html = self.html.replace("&amp;", "&")
        for kind in ("up", "down"):
            token = make_token(item_id, kind, self.cfg)
            self.assertIn(
                f"{self.base}/nd/f?i=thu%3Acivil-code-lecture-20261010"
                f"&id=thu%3Acivil-code-lecture-20261010&k={kind}&t={token}",
                html,
            )
        self.assertIn("\U0001f44d", self.html)
        self.assertIn("\U0001f44e", self.html)

    def test_source_time_and_location_rendered(self):
        self.assertIn("清华大学法学院", self.html)
        self.assertIn("明天 14:00", self.html)
        self.assertIn("法律图书馆 103", self.html)

    def test_summary_is_truncated_to_one_line(self):
        long_summary = self.scored[0].item["ai_summary"]
        self.assertGreater(len(long_summary), SUMMARY_LIMIT)
        rendered = next(part for part in self.html.split("nd-sum") if "数字法治研究中心主办" in part)
        body = rendered.split(">")[1].split("<")[0]
        self.assertLessEqual(len(body), SUMMARY_LIMIT)
        self.assertTrue(body.endswith("\u2026"))

    def test_undated_item_shows_evidence_verbatim(self):
        self.assertIn("具体开通时间以图书馆后续通知为准", self.html)
        self.assertIn("时间待定", self.html)

    # ④ 开发模式降级

    def test_dev_mode_degrades_to_direct_url_with_warning(self):
        dev_cfg = dataclasses.replace(self.cfg, feedback_base="")
        _, html, _ = render_email(self.scored, self.parsed, dev_cfg, self.now)
        self.assertNotIn("/nd/f?", html)
        self.assertNotIn("/nd/c?", html)
        self.assertIn('href="https://mp.weixin.qq.com/s/AAA1111"', html)
        self.assertIn("未配置 feedback_base", html)

    def test_missing_secret_also_degrades(self):
        dev_cfg = dataclasses.replace(self.cfg, hmac_secret="")
        _, html, _ = render_email(self.scored, self.parsed, dev_cfg, self.now)
        self.assertNotIn("/nd/c?", html)
        self.assertIn("缺少 hmac_secret", html)

    # ⑤ 纯文本版

    def test_plain_text_has_no_tags(self):
        self.assertNotIn("<div", self.plain)
        self.assertNotIn("<a ", self.plain)
        for title in ("人工智能与法律推理前沿问题学术报告会", "食堂菜品更新与营业时间调整"):
            self.assertIn(title, self.plain)
        self.assertIn("\u2460 今天/明天能去", self.plain)

    def test_html_to_text_keeps_all_items_and_links(self):
        text = html_to_text(self.html)
        self.assertIn("人工智能与法律推理前沿问题学术报告会", text)
        self.assertIn("/nd/c?i=", text)

    # ⑥ ICS

    def _ics_bytes(self):
        return self.ics.encode("utf-8")

    def test_ics_is_crlf_and_byte_folded(self):
        raw = self._ics_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "ICS 只允许 CRLF 换行")
        lines = raw.split(b"\r\n")
        self.assertLessEqual(max(len(line) for line in lines), 75, "每行不超过 75 字节")
        self.assertTrue(any(line.startswith(b" ") for line in lines), "长行必须折行且续行以空格开头")

    def test_ics_structure_and_event_fields(self):
        raw = self._ics_bytes()
        self.assertTrue(raw.startswith(b"BEGIN:VCALENDAR\r\n"))
        self.assertTrue(raw.endswith(b"END:VCALENDAR\r\n"))
        self.assertIn(b"VERSION:2.0\r\n", raw)
        self.assertIn(b"PRODID:", raw)
        self.assertEqual(raw.count(b"BEGIN:VEVENT\r\n"), raw.count(b"END:VEVENT\r\n"))
        self.assertEqual(raw.count(b"BEGIN:VEVENT\r\n"), 5, "样例里只有 5 条含明确起始时间的活动")
        for field in (b"UID:", b"DTSTAMP:", b"SUMMARY:", b"LOCATION:", b"BEGIN:VALARM", b"TRIGGER:-PT30M"):
            self.assertIn(field, raw)
        # 只看事件块：VTIMEZONE 自己的 DTSTART 是裸本地时间（无 TZID），属正常
        events_only = b"\r\n".join(raw.split(b"BEGIN:VEVENT")[1:])
        for line in events_only.split(b"\r\n"):
            if not line.startswith(b"DTSTART"):
                continue
            if b"VALUE=DATE:" in line:
                continue  # 日期粒度事件写成全天（F4 修复），不带 TZID
            self.assertIn(b"TZID=Asia/Shanghai", line)
            self.assertFalse(line.endswith(b"Z"), "DTSTART 必须是本地时间，不能加 Z")

    def test_ics_excludes_items_without_explicit_start(self):
        raw = self._ics_bytes()
        self.assertNotIn("国际交流项目报名通知".encode("utf-8"), raw)
        self.assertNotIn("图书馆新增数据库试用通知".encode("utf-8"), raw)
        self.assertNotIn("校医院门诊预约方式变更".encode("utf-8"), raw)
        self.assertIn("人工智能与法律推理前沿问题学术报告会".encode("utf-8"), raw)


class RenderEdgeCaseTest(unittest.TestCase):
    def test_empty_day_returns_sentinel(self):
        _, _, cfg, now = load_fixture()
        self.assertEqual(render_email([], {}, cfg, now), ("", "", ""))
        self.assertEqual(render_plain_text([], {}, cfg, now), "")

    def test_top_n_zero_means_no_folding(self):
        scored, parsed = _items(6)
        _, _, cfg, now = load_fixture()
        cfg = dataclasses.replace(cfg, top_n=0)
        _, html, _ = render_email(scored, parsed, cfg, now)
        self.assertEqual(html.count('data-nd-item="1"'), 6)
        self.assertNotIn("超出优先展示条数", html)


class MailerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scored, cls.parsed, cls.cfg, cls.now = load_fixture()
        cls.subject, cls.html, cls.ics = render_email(cls.scored, cls.parsed, cls.cfg, cls.now)

    def setUp(self):
        # 回执目录必须【逐测试】隔离，两个原因：
        # ① fixture 的 db_path 指向仓库内 data/，不改向就会往真账目录写测试回执；
        # ② 本类多个测试投递的是同一份内容，类级共享目录会让第一个测试的「同日同内容已投递」
        #    回执挡住后面所有测试的 SMTP 调用（闸门本身正确，泄漏的是测试态）。
        receipts = tempfile.TemporaryDirectory()
        self.addCleanup(receipts.cleanup)
        self._receipts = receipts

    def _ledger_env(self):
        return {"NOTICE_DIGEST_LEDGER": self._receipts.name}

    def _env(self):
        return {
            "NOTICE_DIGEST_LEDGER": self._receipts.name,
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASS": "unit-test-not-a-real-credential",
            "MAIL_FROM": "digest@example.com",
            "MAIL_TO": "reader@example.com",
        }

    def test_empty_subject_short_circuits_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NOTICE_DIGEST_OUTBOX": str(Path(tmp) / "outbox")}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                mailer.smtplib, "SMTP_SSL"
            ) as smtp_cls:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertTrue(mailer.send("", "", None, self.cfg))
                    self.assertTrue(mailer.send("", self.html, None, self.cfg))
                smtp_cls.assert_not_called()
            self.assertFalse((Path(tmp) / "outbox").exists(), "空主题不应落盘、不应记台账")

    def test_dry_run_writes_outbox_and_never_connects(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox"
            with mock.patch.dict(os.environ, {"NOTICE_DIGEST_OUTBOX": str(outbox)}, clear=True), mock.patch.object(
                mailer.smtplib, "SMTP_SSL"
            ) as smtp_cls, contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(mailer.send(self.subject, self.html, self.ics, self.cfg, dry_run=True))
                smtp_cls.assert_not_called()
            names = sorted(path.name for path in outbox.iterdir())
            for suffix in ("-subject.txt", "-email.html", "-email_plain.txt", "-email.eml", "-events.ics", "-meta.json"):
                self.assertTrue(any(name.endswith(suffix) for name in names), f"缺少 {suffix}：{names}")
            eml = next(outbox.glob("*-email.eml")).read_bytes()
            self.assertIn(b"text/plain", eml)
            self.assertIn(b"text/html", eml)
            self.assertIn(b"text/calendar", eml)
            self.assertIn(b"notice-digest.ics", eml)
            # 附件是 base64 编码的，必须解码后检查 ICS 内容
            parsed = BytesParser(policy=email_policy.default).parsebytes(eml)
            calendar = next(p for p in parsed.walk() if p.get_content_type() == "text/calendar")
            ics_bytes = calendar.get_payload(decode=True)
            self.assertIn(b"BEGIN:VCALENDAR", ics_bytes)
            self.assertIn(b"BEGIN:VEVENT", ics_bytes)
            self.assertIn(b"VALARM", ics_bytes)
            self.assertEqual(calendar.get_filename(), "notice-digest.ics")

    def test_real_send_uses_smtp_ssl_with_env_credentials(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls, mock.patch.object(mailer, "_record_ledger") as ledger, contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertTrue(mailer.send(self.subject, self.html, self.ics, self.cfg))
        smtp_cls.assert_called_once()
        self.assertEqual(smtp_cls.call_args[0][0], "smtp.example.com")
        self.assertEqual(smtp_cls.call_args[0][1], 465)
        server = smtp_cls.return_value.__enter__.return_value
        server.login.assert_called_once_with("sender@example.com", "unit-test-not-a-real-credential")
        server.send_message.assert_called_once()
        message = server.send_message.call_args[0][0]
        self.assertEqual(message["Subject"], self.subject)
        self.assertIn("reader@example.com", message["To"])
        self.assertNotIn("unit-test-not-a-real-credential", str(message["From"]))
        types = [part.get_content_type() for part in message.walk()]
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)
        self.assertIn("text/calendar", types)
        calendar = [part for part in message.walk() if part.get_content_type() == "text/calendar"][0]
        self.assertEqual(calendar.get_filename(), "notice-digest.ics")
        self.assertIn("method=publish", str(calendar["Content-Type"]).lower().replace('"', ""))
        self.assertIn(b"BEGIN:VEVENT", calendar.get_payload(decode=True))
        plain = next(part for part in message.walk() if part.get_content_type() == "text/plain")
        self.assertIn("人工智能与法律推理前沿问题学术报告会", plain.get_content())
        ledger.assert_called_once()
        self.assertEqual(ledger.call_args[0][2], 12)

    def test_missing_credentials_returns_false_without_connecting(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self._ledger_env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls, contextlib.redirect_stderr(stderr):
            self.assertFalse(mailer.send(self.subject, self.html, self.ics, self.cfg))
        smtp_cls.assert_not_called()
        self.assertIn("SMTP_USER", stderr.getvalue())

    def test_cfg_credentials_used_when_env_is_empty(self):
        """环境变量为空时退回 cfg（load_config 从 .env 读到的 ND_SMTP_*）。"""
        cfg = dataclasses.replace(
            self.cfg, smtp_user="cfg-sender@example.com", smtp_pass="cfg-unit-test-credential"
        )
        with mock.patch.dict(os.environ, self._ledger_env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls, mock.patch.object(mailer, "_record_ledger"), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertTrue(mailer.send(self.subject, self.html, self.ics, cfg))
        server = smtp_cls.return_value.__enter__.return_value
        server.login.assert_called_once_with("cfg-sender@example.com", "cfg-unit-test-credential")

    def test_real_send_accepts_nd_env_names(self):
        """项目实际使用的 ND_* 变量名必须被识别。"""
        env = {
            "NOTICE_DIGEST_LEDGER": self._receipts.name,
            "ND_SMTP_HOST": "smtp.nd.example.com",
            "ND_SMTP_PORT": "465",
            "ND_SMTP_USER": "nd-sender@example.com",
            "ND_SMTP_PASS": "nd-unit-test-credential",
            "ND_FROM_ADDR": "digest@example.com",
            "ND_TO_ADDR": "reader@example.com",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls, mock.patch.object(mailer, "_record_ledger"), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertTrue(mailer.send(self.subject, self.html, self.ics, self.cfg))
        self.assertEqual(smtp_cls.call_args[0][0], "smtp.nd.example.com")
        server = smtp_cls.return_value.__enter__.return_value
        server.login.assert_called_once_with("nd-sender@example.com", "nd-unit-test-credential")
        message = server.send_message.call_args[0][0]
        self.assertIn("reader@example.com", message["To"])

    def test_smtp_error_returns_false_with_diagnosis(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL", side_effect=OSError("connection refused")
        ), contextlib.redirect_stderr(stderr):
            self.assertFalse(mailer.send(self.subject, self.html, self.ics, self.cfg))
        output = stderr.getvalue()
        self.assertIn("connection refused", output)
        self.assertIn("smtp.example.com", output)
        self.assertNotIn("unit-test-not-a-real-credential", output)

    def test_to_addr_override_wins(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls, mock.patch.object(mailer, "_record_ledger"), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(mailer.send(self.subject, self.html, self.ics, self.cfg, to_addr="other@example.com"))
        message = smtp_cls.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertIn("other@example.com", message["To"])


class CliSelftestTest(unittest.TestCase):
    def test_render_selftest_writes_subject_html_plain_ics(self):
        from notice_digest import render

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selftest"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(render.main(["--selftest", "--out", str(out)]), 0)
            self.assertEqual((out / "subject.txt").read_text(encoding="utf-8").strip(), EXPECTED_SUBJECT)
            html = (out / "email.html").read_text(encoding="utf-8")
            plain = (out / "email_plain.txt").read_text(encoding="utf-8")
            ics = (out / "events.ics").read_bytes()
            self.assertIn("<html", html)
            self.assertNotIn("<div", plain)
            self.assertIn(b"BEGIN:VCALENDAR", ics)
            self.assertIn(b"\r\n", ics)
            self.assertLessEqual(max(len(line) for line in ics.split(b"\r\n")), 75)

    def test_mailer_selftest_goes_through_dry_run_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            with mock.patch.dict(
                os.environ, {"NOTICE_DIGEST_OUTBOX": str(out)}, clear=False
            ), mock.patch.object(mailer.smtplib, "SMTP_SSL") as smtp_cls, contextlib.redirect_stderr(
                io.StringIO()
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mailer.main(["--dry-run", "--selftest", "--out", str(out)]), 0)
                smtp_cls.assert_not_called()
            self.assertTrue(any(out.glob("*-email.eml")))

    def test_cli_requires_selftest(self):
        from notice_digest import render

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            render.main([])


class HygieneTest(unittest.TestCase):
    FILES = ("notice_digest/render.py", "notice_digest/mailer.py")

    def _source(self, relative):
        return (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    def test_no_third_party_imports(self):
        pattern = re.compile(
            r"^\s*(?:import|from)\s+(jinja2|icalendar|yaml|requests|numpy|pandas|bs4|lxml|arrow|dateutil)\b",
            re.MULTILINE,
        )
        for relative in self.FILES:
            with self.subTest(file=relative):
                self.assertIsNone(pattern.search(self._source(relative)), f"{relative} 引入了第三方依赖")

    def test_credentials_only_from_environment(self):
        source = self._source("notice_digest/mailer.py")
        # 凭据按项目约定 ND_* 读取（兼容旧无前缀名），都经 _env_first → 环境变量/ .env
        self.assertIn('_env_first("ND_SMTP_PASS", "SMTP_PASS")', source)
        self.assertIn('_env_first("ND_SMTP_USER", "SMTP_USER")', source)
        self.assertNotIn("smtp_pass = ", source)
        literal_secret = re.compile(r"(?i)\b(password|passwd|secret|token)\s*=\s*[\"'][^\"']{3,}")
        self.assertIsNone(literal_secret.search(source), "mailer.py 里出现字面口令赋值")
        for template in ("notice_digest/templates/email.html.j2", "notice_digest/templates/email.txt.j2"):
            text = self._source(template)
            self.assertNotIn("SMTP_PASS", text)
            self.assertNotIn("password", text.lower())

    def test_signatures_match_frozen_contract(self):
        import inspect

        from notice_digest import render

        signature = inspect.signature(render.render_email)
        self.assertEqual(
            list(signature.parameters), ["scored", "parsed", "cfg", "now"], "render_email 签名被契约冻结"
        )
        send_signature = inspect.signature(mailer.send)
        self.assertEqual(
            list(send_signature.parameters),
            ["subject", "html", "ics_text", "cfg", "to_addr", "dry_run"],
            "send 签名被契约冻结",
        )
        self.assertIsInstance(self.cfg_type(), type)
        from notice_digest.score import Scored as _Scored

        self.assertIs(Scored, _Scored)

    def cfg_type(self):
        return Config


# --------------------------------------------------------------------------- ⑦ t12 阶段 A：F1/F3–F7 修复验收


class PhaseARepairTest(unittest.TestCase):
    """F3 UID 稳定 / F4 日期粒度全天 / F5 VTIMEZONE / F6 位置来源一致 / F7 纯文本对齐 / F1 同日闸门。"""

    def setUp(self):
        self.scored, self.parsed, self.cfg, self.now = load_fixture()
        self.subject, self.html, self.ics = render_email(
            self.scored, self.parsed, self.cfg, self.now
        )
        # render_email 的第三项是 ICS 附件本体；纯文本版另有入口（F7 比对必须用它）
        self.plain = render_plain_text(self.scored, self.parsed, self.cfg, self.now)
        self.smtp_env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASS": "unit-test-not-a-real-credential",
            "MAIL_FROM": "digest@example.com",
            "MAIL_TO": "reader@example.com",
        }

    def _render_one(self, item, start, end=None, evidence="10月10日"):
        """单条目渲染，返回 (ics, html)：用来精确控制时间粒度与字段形态。"""
        parsed = {
            item["id"]: ParsedTime(
                start=start, end=end, deadline=None, bucket="next_week", evidence=evidence
            )
        }
        scored = [Scored(item=item, score=1.0, reasons=[])]
        _, html, _ = render_email(scored, parsed, self.cfg, self.now)
        return render_ics(scored, parsed, self.cfg, self.now), html

    @staticmethod
    def _uids(ics_text):
        return [
            line.split(":", 1)[1]
            for line in ics_text.split("\r\n")
            if line.startswith("UID:")
        ]

    # F3：UID 只看条目 id，时间/标题变化都不得让日历里出现「新事件」
    def test_uid_does_not_drift_when_start_time_changes(self):
        before = self._uids(self.ics)
        self.assertEqual(len(before), 5)
        moved = {}
        for key, parsed_time in self.parsed.items():
            if parsed_time.start is not None:
                moved[key] = dataclasses.replace(
                    parsed_time, start=parsed_time.start + timedelta(days=1)
                )
                break
        self.assertTrue(moved, "样例里没有带 start 的条目，无法验证 UID 稳定性")
        after = self._uids(
            render_ics(self.scored, {**self.parsed, **moved}, self.cfg, self.now)
        )
        self.assertEqual(sorted(before), sorted(after), "起始时间变化不得让 UID 漂移（F3）")

    def test_uid_is_derived_from_item_id_not_title(self):
        item = {"id": "thu:uid-probe", "title": "标题会变的通知", "source_name": "测试来源"}
        ics, _ = self._render_one(item, datetime(2026, 10, 10, 14, 0))
        renamed, _ = self._render_one(
            dict(item, title="改过标题的通知"), datetime(2026, 10, 10, 14, 0)
        )
        self.assertEqual(self._uids(ics), self._uids(renamed))
        self.assertEqual(self._uids(ics), self._uids(ics))

    def test_every_event_carries_sequence_and_last_modified(self):
        blocks = self.ics.split("BEGIN:VEVENT")[1:]
        self.assertEqual(len(blocks), 5)
        for block in blocks:
            self.assertIn("SEQUENCE:", block)
            self.assertIn("LAST-MODIFIED:", block)

    # F4：日期粒度必须写成全天，且提醒不得甩到前一天半夜
    def test_date_only_start_becomes_all_day_event(self):
        item = {"id": "thu:all-day-probe", "title": "全天活动探针", "source_name": "测试来源"}
        ics, _ = self._render_one(item, datetime(2026, 10, 10), evidence="10月10日")
        self.assertIn("DTSTART;VALUE=DATE:20261010", ics)
        self.assertIn("DTEND;VALUE=DATE:20261011", ics)
        self.assertNotIn("DTSTART;TZID=", ics)
        trigger = next(line for line in ics.split("\r\n") if line.startswith("TRIGGER"))
        # 绝对触发 = 当天 07:30（Asia/Shanghai）= 前一天 23:30Z；
        # 修复前是 00:00 - PT30M = 前一天 23:30「本地」时间（真·半夜提醒）。
        self.assertEqual(trigger, "TRIGGER;VALUE=DATE-TIME:20261009T233000Z")

    def test_clock_start_stays_timed_event_with_relative_alarm(self):
        item = {
            "id": "thu:timed-probe",
            "title": "带钟点的活动探针",
            "source_name": "测试来源",
            "ai_event_time": "10月10日 14:00",
        }
        ics, _ = self._render_one(
            item, datetime(2026, 10, 10, 14, 0), datetime(2026, 10, 10, 16, 0)
        )
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261010T140000", ics)
        self.assertIn("DTEND;TZID=Asia/Shanghai:20261010T160000", ics)
        self.assertIn("TRIGGER:-PT30M", ics)

    # F5：引用了 TZID 就必须带同名 VTIMEZONE
    def test_calendar_declares_the_referenced_timezone(self):
        self.assertIn("BEGIN:VTIMEZONE", self.ics)
        self.assertIn("END:VTIMEZONE", self.ics)
        block = self.ics.split("BEGIN:VTIMEZONE")[1].split("END:VTIMEZONE")[0]
        self.assertIn("TZID:Asia/Shanghai", block)
        self.assertIn("TZOFFSETTO:+0800", block)
        for line in self.ics.split("\r\n"):
            if "TZID=" in line:
                self.assertIn("TZID=Asia/Shanghai", line)

    # F6：HTML 与 ICS 共用同一个地点来源，且不再只认 ai_event_location
    def test_location_uses_shared_source_for_html_and_ics(self):
        item = {
            "id": "thu:loc-probe",
            "title": "地点探针",
            "source_name": "测试来源",
            "detail": {"event_location": "第六教学楼 6A018"},
            "ai_event_time": "10月10日 14:00",
        }
        ics, html = self._render_one(item, datetime(2026, 10, 10, 14, 0))
        self.assertIn("LOCATION:第六教学楼 6A018", ics)
        self.assertIn("第六教学楼 6A018", html)

    def test_location_accepts_generic_location_key(self):
        item = {
            "id": "thu:loc-probe-2",
            "title": "通用地点字段",
            "source_name": "测试来源",
            "location": "明理楼 321",
            "ai_event_time": "10月10日 14:00",
        }
        ics, html = self._render_one(item, datetime(2026, 10, 10, 14, 0))
        self.assertIn("LOCATION:明理楼 321", ics)
        self.assertIn("明理楼 321", html)

    # F7：纯文本版元信息与 HTML 版逐字一致（含「日历附件 N 项」），模板允许长串折行
    def test_plain_text_meta_line_matches_html_meta_line(self):
        from html import unescape

        match = re.search(r'class="nd-meta-line"[^>]*>(.*?)</p>', self.html, re.S)
        self.assertIsNotNone(match, "HTML 里找不到元信息行")
        html_meta = unescape(match.group(1)).strip()
        self.assertIn("日历附件", html_meta)
        self.assertIn(html_meta, self.plain, "纯文本版元信息必须与 HTML 版一致（F7）")

    def test_html_template_wraps_long_tokens(self):
        template = (
            PROJECT_ROOT / "notice_digest" / "templates" / "email.html.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("word-break", template)
        self.assertIn("overflow-wrap", template)

    # F1（阶段 A 可验证部分）：同一天同一内容只投递一次；内容变了照常投递
    def test_receipt_dir_follows_env_override(self):
        """F1 回执目录必须可由环境变量改向，否则测试态与生产态会互相污染。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(self.smtp_env, NOTICE_DIGEST_LEDGER=tmp)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                mailer, "_record_ledger"
            ), mock.patch.object(mailer.smtplib, "SMTP_SSL"), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(mailer.send(self.subject, self.html, self.ics, self.cfg))
            receipts = sorted(Path(tmp).glob("*.json"))
            self.assertEqual(len(receipts), 1, f"回执应落在被改向的目录，实际：{receipts}")
            self.assertRegex(receipts[0].name, r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{32}\.json$")

    def test_second_real_send_same_day_is_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(self.cfg, db_path=Path(tmp) / "notice.db")
            with mock.patch.dict(os.environ, self.smtp_env, clear=True), mock.patch.object(
                mailer, "_record_ledger"
            ), mock.patch.object(mailer.smtplib, "SMTP_SSL") as smtp_cls, contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertTrue(mailer.send(self.subject, self.html, self.ics, cfg))
                self.assertTrue(mailer.send(self.subject, self.html, self.ics, cfg))
                self.assertEqual(smtp_cls.call_count, 1, "同一天同内容只允许投递一次（F1 闸门）")
                self.assertTrue(
                    mailer.send(self.subject, self.html + "<!--更新-->", self.ics, cfg)
                )
                self.assertEqual(smtp_cls.call_count, 2, "内容变化后必须照常投递")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- ⑧ t12 阶段 B：投递窗口 + 幂等闸门 + ICS 时间不变量


def _pb_item(item_id, published_at=None, **extra):
    """构造一条最小可用条目：只填 upsert 会落库的字段。"""
    item = {
        "id": item_id,
        "title": f"阶段 B 探针通知 {item_id}",
        "source_id": "src-pb",
        "source_name": "阶段 B 测试来源",
        "url": f"https://example.invalid/{item_id}",
        "category": "讲座活动",
        "intent_group": "lecture",
    }
    if published_at is not None:
        item["published_at"] = published_at
    item.update(extra)
    return item


class PhaseBGateTest(unittest.TestCase):
    """阶段 B：闸门在发信之前、窗口只认成功投递、崩溃窗口可观测。

    判据一律用**行为三事实**：SMTP 调用次数 + 闸门专属日志 + 台账/attempt 行状态。
    台账行数不能当证据（``sends.date`` 是主键 + UPSERT，行数永远是 1）。
    """

    def setUp(self):
        from notice_digest.store import Store

        _, _, cfg, _ = load_fixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "notice.db"
        self.outbox = root / "outbox"
        self.ledger = root / "send-receipts"
        self.cfg = dataclasses.replace(cfg, db_path=self.db)
        self.store = Store(self.db)
        self.store.init_schema()
        self.addCleanup(self.store.close)

    # ---------------------------------------------------------------- 工具
    def _env(self):
        return {
            "NOTICE_DIGEST_LEDGER": str(self.ledger),
            "NOTICE_DIGEST_OUTBOX": str(self.outbox),
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASS": "unit-test-not-a-real-credential",
            "MAIL_FROM": "digest@example.com",
            "MAIL_TO": "reader@example.com",
        }

    def _run_send(self, dry_run=False):
        """跑一次 cmd_send，返回 (rc, stdout, stderr)。"""
        import types

        from notice_digest.cli import cmd_send

        args = types.SimpleNamespace(db=str(self.db), top=None, to=None, dry_run=dry_run)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cmd_send(self.cfg, args)
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _summary(stdout):
        import json

        return json.loads(stdout.strip().splitlines()[-1])

    def _receipts(self):
        if not self.ledger.exists():
            return []
        return sorted(path.name for path in self.ledger.iterdir())

    def _today(self):
        from notice_digest.store import now_shanghai

        return now_shanghai().date().isoformat()

    # ---------------------------------------------------------------- 闸门语义
    def test_begin_send_records_attempt_then_sent_and_is_exclusive(self):
        day = "2026-10-08"
        self.assertIsNone(self.store.get_send(day), "没有任何投递记录时应返回 None")

        self.assertTrue(self.store.begin_send(day, "主题", "fp-1"))
        row = self.store.get_send(day)
        self.assertEqual(row["status"], "attempt")
        self.assertEqual(row["fingerprint"], "fp-1")
        self.assertTrue(row["pending_attempt"], "抢权后未收尾的尝试必须可观测（崩溃窗口）")
        self.assertFalse(row["sent"])
        self.assertIsNone(self.store.last_sent_at(), "没发完的尝试不得推进投递窗口")

        self.store.mark_sent(day, 3, "fp-1")
        row = self.store.get_send(day)
        self.assertEqual(row["status"], "sent")
        self.assertTrue(row["sent"])
        self.assertEqual(row["n_items"], 3)
        self.assertIsNotNone(self.store.last_sent_at())

        self.assertFalse(self.store.begin_send(day, "主题", "fp-1"), "同日同指纹必须被闸门挡住")
        self.assertTrue(self.store.begin_send(day, "内容变了", "fp-2"), "内容变了应放行")

    def test_window_keeps_undated_items_and_orders_by_publish_time(self):
        base = datetime.fromisoformat("2026-10-08T12:00:00+08:00")
        self.store.upsert_items(
            [
                _pb_item("pb:old", "2026-10-08T09:00:00+08:00"),
                _pb_item("pb:new", "2026-10-09T09:00:00+08:00"),
                _pb_item("pb:undated", None),
            ]
        )
        got = [item["id"] for item in self.store.items_published_after(base)]
        self.assertEqual(got, ["pb:new", "pb:undated"], "窗口按发布时间倒序，未定日期的条目必须保留")

        self.assertEqual(len(self.store.items_not_yet_sent()), 3, "从未成功投递时窗口不限")
        self.store.mark_sent("2026-10-08", 1, "fp-x", sent_at=base.isoformat())
        self.assertEqual(
            sorted(item["id"] for item in self.store.items_not_yet_sent()),
            ["pb:new", "pb:undated"],
            "成功投递之后的窗口只应包含更晚发布与未定日期的条目",
        )

    # ---------------------------------------------------------------- 整链路
    def test_two_same_day_sends_call_smtp_once_and_leave_a_sent_row(self):
        # 未定发布时间的条目（published_ts 为 NULL）在窗口里永远保留，
        # 因此「同日重跑 → 渲染内容完全相同」正是幂等闸门必须拦下的场景。
        self.store.upsert_items([_pb_item("pb:one")])
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls:
            rc1, out1, _ = self._run_send()
            rc2, out2, err2 = self._run_send()

        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0, "重复投递不是失败：timer 重试与手动补跑都应无害")
        # 事实一：SMTP 只被调用一次
        self.assertEqual(smtp_cls.call_count, 1, "同日同内容第二次必须不发信")
        # 事实二：第二次走的是闸门路径，而不是投递成功路径
        self.assertIn("幂等闸门命中", err2)
        self.assertNotIn("[mailer] 已投递", err2)
        # 事实三：台账/attempt 行状态 + 回执数
        self.assertEqual(self._summary(out1)["n_items"], 1)
        self.assertEqual(self._summary(out2)["n_items"], 1)
        self.assertIsNotNone(
            self.store.last_sent_at(), "首次投递必须推进窗口；第二次靠指纹闸门而非窗口拦截"
        )
        row = self.store.get_send(self._today())
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["n_items"], 1)
        self.assertEqual(len(self._receipts()), 1)

    def test_changed_content_reopens_the_gate_and_window_counts_only_new_items(self):
        self.store.upsert_items([_pb_item("pb:first", "2026-10-08T09:00:00+08:00")])
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls:
            rc1, out1, _ = self._run_send()
            # 投递之后才出现的条目：用「现在 +1 小时」保证落在窗口里、且不受真实时钟漂移影响
            later = datetime.now().astimezone() + timedelta(hours=1)
            self.store.upsert_items([_pb_item("pb:second", later.isoformat())])
            rc2, out2, _ = self._run_send()

        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(self._summary(out1)["n_items"], 1)
        self.assertEqual(smtp_cls.call_count, 2, "内容变了必须重新发信")
        self.assertEqual(
            self._summary(out2)["n_items"], 1, "「新增 N 条」只数本窗口的条目，不重复计历史条目"
        )
        self.assertEqual(len(self._receipts()), 2, "新指纹要有新回执")

    def test_crash_window_keeps_the_items_and_the_next_run_resends(self):
        self.store.upsert_items([_pb_item("pb:crash", "2026-10-08T09:00:00+08:00")])
        day = self._today()
        # 模拟：抢到发送权之后进程死掉，没走到 mark_sent
        self.assertTrue(self.store.begin_send(day, "主题", "fp-crash"))
        self.assertIsNone(self.store.last_sent_at())

        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls:
            rc, out, _ = self._run_send()

        self.assertEqual(rc, 0)
        self.assertEqual(smtp_cls.call_count, 1, "崩溃窗口里的条目下一轮必须照常发出去")
        self.assertEqual(self._summary(out)["n_items"], 1)
        self.assertEqual(self.store.get_send(day)["status"], "sent")

    def test_dry_run_neither_gates_nor_records_a_send(self):
        self.store.upsert_items([_pb_item("pb:dry", "2026-10-08T09:00:00+08:00")])
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls:
            rc_dry, out_dry, _ = self._run_send(dry_run=True)
            self.assertIsNone(self.store.get_send(self._today()), "预演不得占掉当天真正的投递机会")
            rc_real, _, _ = self._run_send()

        self.assertEqual((rc_dry, rc_real), (0, 0))
        self.assertEqual(smtp_cls.call_count, 1, "预演不发信，随后那次真实投递才发信")
        self.assertFalse(self._summary(out_dry)["gated"])
        self.assertTrue(list(self.outbox.iterdir()), "dry-run 必须落盘")
        self.assertEqual(len(self._receipts()), 1)

    def test_empty_window_hits_the_empty_subject_sentinel(self):
        with mock.patch.dict(os.environ, self._env(), clear=True), mock.patch.object(
            mailer.smtplib, "SMTP_SSL"
        ) as smtp_cls:
            rc, out, _ = self._run_send()

        self.assertEqual(rc, 0)
        self.assertEqual(smtp_cls.call_count, 0)
        summary = self._summary(out)
        self.assertEqual(summary["subject"], "", "窗口为空时主题必须退化成空串哨兵")
        self.assertEqual(summary["n_items"], 0)
        self.assertFalse(summary["sent"])
        self.assertEqual(self._receipts(), [])
        self.assertFalse(self.outbox.exists(), "空主题哨兵不落盘、不记台账")


class PhaseBIcsInvariantTest(unittest.TestCase):
    """阶段 B：ICS 时间形态不变量（按行为判定，不 grep 源码字符串）。"""

    def setUp(self):
        self.scored, self.parsed, self.cfg, self.now = load_fixture()
        _, self.html, self.ics = render_email(self.scored, self.parsed, self.cfg, self.now)

    def _event_time_lines(self):
        """只取 VEVENT 区域里的 DTSTART/DTEND。

        VTIMEZONE 子组件里的 STANDARD/DAYLIGHT 起点按 RFC 5545 §3.6.5 必须是**本地时间**
        （不能带 TZID、也不能是 UTC 的 Z 形式），所以它们不属于「事件时间行」。
        """
        picked, in_tz = [], False
        for line in self.ics.split("\r\n"):
            if line == "BEGIN:VTIMEZONE":
                in_tz = True
            elif line == "END:VTIMEZONE":
                in_tz = False
            elif not in_tz and line.startswith(("DTSTART", "DTEND")):
                picked.append(line)
        return picked

    def _probe_ics(self, specs):
        """用最小条目字典渲染探针日历（不走 fixture），逐个事件检查不变量。"""
        scored, parsed = [], {}
        for item, pt in specs:
            scored.append(Scored(item=item, score=1.0, reasons=[]))
            parsed[item["id"]] = pt
        return render_ics(scored, parsed, self.cfg, self.now)

    def test_every_event_time_line_is_tzid_timed_or_date_valued(self):
        lines = self._event_time_lines()
        self.assertTrue(lines, "样例日报至少要有一个事件时间行")
        for line in lines:
            self.assertTrue(
                "TZID=Asia/Shanghai" in line or ";VALUE=DATE:" in line,
                f"定时行必须带 TZID、全天行必须用 DATE 值：{line}",
            )
        self.assertNotIn("19700101", self.ics, "不得出现 Unix 纪元兜底时间")
        for line in self.ics.split("\r\n"):
            if line.startswith("DTSTART") and "TZID=" in line:
                self.assertIn("TZID=Asia/Shanghai", line)

    def test_all_day_events_do_not_inherit_timed_values_or_midnight_alarms(self):
        # fixture 里全是定时事件，全天路径必须用混合探针才走得到（见 _probe_ics）。
        specs = [
            (
                {"id": "pb:probe-allday", "title": "全天探针", "source_name": "阶段 B 测试来源"},
                ParsedTime(
                    start=datetime(2026, 10, 10),
                    end=None,
                    deadline=None,
                    bucket="next_week",
                    evidence="10月10日",
                ),
            ),
            (
                {"id": "pb:probe-timed", "title": "定时探针", "source_name": "阶段 B 测试来源"},
                ParsedTime(
                    start=datetime(2026, 10, 10, 14, 0),
                    end=datetime(2026, 10, 10, 16, 0),
                    deadline=None,
                    bucket="tomorrow",
                    evidence="10月10日 14:00-16:00",
                ),
            ),
        ]
        ics = self._probe_ics(specs)
        all_day, timed = 0, 0
        for block in ics.split("BEGIN:VEVENT")[1:]:
            lines = block.split("\r\n")
            dtstart = next(line for line in lines if line.startswith("DTSTART"))
            dtend = next(line for line in lines if line.startswith("DTEND"))
            trigger = next(line for line in lines if line.startswith("TRIGGER"))
            if ";VALUE=DATE:" in dtstart:
                all_day += 1
                self.assertIn(";VALUE=DATE:", dtend, "全天事件的 DTEND 也必须是日期粒度")
                self.assertNotIn("TZID=", dtstart, "全天 DTSTART 不得带 TZID（RFC 5545 §3.3.5）")
                self.assertNotIn("TZID=", dtend, "全天 DTEND 不得继承上一条定时事件的 TZID")
                self.assertNotEqual(
                    trigger, "TRIGGER:-PT30M", "全天事件不得用相对提醒（会落到前一天本地 23:30）"
                )
                self.assertRegex(
                    trigger,
                    r"^TRIGGER;VALUE=DATE-TIME:\d{8}T\d{6}Z$",
                    "全天提醒必须是绝对 UTC 时刻",
                )
            else:
                timed += 1
                self.assertIn("TZID=Asia/Shanghai", dtstart, "定时 DTSTART 必须带 TZID")
                self.assertIn("TZID=Asia/Shanghai", dtend, "定时 DTEND 必须带 TZID")
                self.assertEqual(trigger, "TRIGGER:-PT30M", "定时事件用相对提醒")
        self.assertEqual((all_day, timed), (1, 1), "混合日历里全天与定时事件各一个")

    def test_all_day_only_calendar_needs_no_timezone_component(self):
        item = {"id": "pb:allday", "title": "全天探针通知", "source_name": "阶段 B 测试来源"}
        parsed = {
            item["id"]: ParsedTime(
                start=datetime(2026, 10, 10),
                end=None,
                deadline=None,
                bucket="next_week",
                evidence="10月10日",
            )
        }
        scored = [Scored(item=item, score=1.0, reasons=[])]
        ics = render_ics(scored, parsed, self.cfg, self.now)
        self.assertIn("DTSTART;VALUE=DATE:20261010", ics)
        self.assertNotIn("TZID=", ics, "没有定时事件时不得引用时区")
        self.assertNotIn("BEGIN:VTIMEZONE", ics, "没有定时事件时不得声明 VTIMEZONE")

    def test_undated_item_yields_no_vevent_but_stays_in_the_body(self):
        item = {"id": "pb:undated", "title": "时间待定探针通知", "source_name": "阶段 B 测试来源"}
        parsed = {item["id"]: UNDATED}
        scored = [Scored(item=item, score=1.0, reasons=[])]
        _, html, ics = render_email(scored, parsed, self.cfg, self.now)
        self.assertNotIn("BEGIN:VEVENT", ics, "没有可用时间的条目不得凭空造一个日历事件")
        self.assertNotIn("时间待定", ics)
        self.assertIn("时间待定", html, "无时间的条目必须仍然出现在正文的「时间待定」里")
        self.assertIn("时间待定探针通知", html)
