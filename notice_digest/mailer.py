"""投递层：SMTP 发送（SSL / STARTTLS）+ ICS 日历附件 + dry-run 落盘 + 投递台账。

对外契约（captain 冻结）：::

    send(subject, html, ics_text, cfg, to_addr=None, dry_run=False) -> bool

* 空主题（当天没有新条目）→ 短路返回 True：不发信、不写台账、不落盘。
* ``dry_run=True`` → 只把邮件落盘到 ``data/outbox/``（可用 ``NOTICE_DIGEST_OUTBOX`` 覆盖），不连 SMTP。
* 任何失败（缺凭据 / SMTP 异常）→ 返回 False 并把可诊断信息打到 stderr，**绝不静默成功**。
* 凭据先从环境变量读（``ND_`` 前缀优先，兼容旧名）：``ND_SMTP_HOST`` / ``ND_SMTP_PORT`` /
  ``ND_SMTP_USER`` / ``ND_SMTP_PASS`` / ``ND_FROM_ADDR`` / ``ND_TO_ADDR``
  （旧名 ``SMTP_HOST`` / ``SMTP_PORT`` / ``SMTP_USER`` / ``SMTP_PASS`` / ``MAIL_FROM`` / ``MAIL_TO`` 仍可用）；
  环境变量为空时回落到 ``Config`` 字段（由 ``config.py`` 从 ``.env`` 装载）；
  代码、模板、日志里都不出现明文口令。
* 邮件是 multipart/alternative（text/plain + text/html）+ ``text/calendar`` 附件。
  因契约签名只传 html，text/plain 备选由 ``render.html_to_text`` 从同一份 HTML 转换得到。
* 真实发送成功后才写投递台账（``store.Store.record_send``）；台账写失败不影响"邮件已发出"的事实，
  只在 stderr 告警。
* **同日幂等闸门**：同一日期 + 同一指纹（主题/条数/HTML 的 sha256）只投递一次；
  第二次运行命中闸门时打印可区分的日志并返回 True（不连 SMTP、不重复记台账）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from .config import Config
from .render import html_to_text, load_fixture, render_email

__all__ = ["send", "build_message", "main"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTBOX = PROJECT_ROOT / "data" / "outbox"
DEFAULT_FROM_NAME = "\u6e05\u534e\u901a\u77e5\u65e5\u62a5"  # 清华通知日报
ICS_FILENAME = "notice-digest.ics"
SMTP_TIMEOUT = 30

# 幂等闸门回执目录（阶段 A 的过渡实现：不依赖 store.py 的新接口）。
# 默认 = 投递台账所在目录（cfg.db_path 的父目录）；
# 阶段 B 由 store.py 的投递尝试行接管后，这里退化为本地文件回执的双保险。
DEFAULT_LEDGER_DIR = PROJECT_ROOT / "data"

# cte_type="7bit"：正文用 quoted-printable、附件用 base64，保证 7bit 干净、任何 SMTP 都能收
_MAIL_POLICY = policy.SMTP.clone(cte_type="7bit")


def _env(name: str):
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _dry_run_dir() -> Path:
    override = _env("NOTICE_DIGEST_OUTBOX")
    return Path(override).expanduser() if override else DEFAULT_OUTBOX


def _env_first(*names):
    """按顺序取第一个非空环境变量（用于兼容 ND_* 与旧的无前缀命名）。"""
    for name in names:
        value = _env(name)
        if value:
            return value
    return None


def resolve_targets(cfg: Config, to_addr=None) -> dict:
    """环境变量优先、cfg 兜底（两者都来自 .env / profile，口令绝不出现在代码里）。

    命名以项目约定 ``ND_*`` 为准（config.load_config 读的就是这套），同时兼容旧的无前缀
    名字；cfg 上的 ``smtp_*`` 字段由 load_config 从 ``.env`` 填入，因此直接跑 Python 模块
    时也能带上凭据。缺失的字段为 None → send() 明确失败，不会静默成功。
    """
    return {
        "host": _env_first("ND_SMTP_HOST", "SMTP_HOST") or (getattr(cfg, "smtp_host", "") or None),
        "port": int(_env_first("ND_SMTP_PORT", "SMTP_PORT") or getattr(cfg, "smtp_port", 0) or 465),
        "user": _env_first("ND_SMTP_USER", "SMTP_USER") or (getattr(cfg, "smtp_user", "") or None),
        "password": _env_first("ND_SMTP_PASS", "SMTP_PASS") or (getattr(cfg, "smtp_pass", "") or None),
        "from_addr": _env_first("ND_FROM_ADDR", "MAIL_FROM") or (getattr(cfg, "from_addr", "") or None),
        "to_addr": to_addr or _env_first("ND_TO_ADDR", "MAIL_TO") or (getattr(cfg, "to_addr", "") or None),
        "from_name": _env_first("MAIL_FROM_NAME", "ND_FROM_NAME") or DEFAULT_FROM_NAME,
        "ssl": bool(getattr(cfg, "smtp_ssl", True)),
        "timeout": int(getattr(cfg, "timeout", 0) or SMTP_TIMEOUT),
    }


def build_message(subject: str, html: str, ics_text, cfg: Config, to_addr=None) -> tuple:
    """构造 RFC822 邮件（未发送）。返回 (message, targets)。"""
    targets = resolve_targets(cfg, to_addr)
    msg = EmailMessage(policy=_MAIL_POLICY)
    msg["Subject"] = subject
    msg["From"] = formataddr((targets["from_name"], targets["from_addr"] or "notice-digest@localhost"))
    if targets["to_addr"]:
        msg["To"] = targets["to_addr"]
    msg["Date"] = formatdate(localtime=True)
    msg["X-Mailer"] = "notice-digest"
    msg.set_content(html_to_text(html), subtype="plain", charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    if ics_text:
        msg.add_attachment(
            ics_text.encode("utf-8"),
            maintype="text",
            subtype="calendar",
            filename=ICS_FILENAME,
            params={"method": "PUBLISH"},
        )
    return msg, targets


def _write_dry_run(out_dir: Path, subject: str, msg, html: str, ics_text) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written = []
    files = {
        f"{stamp}-subject.txt": subject + "\n",
        f"{stamp}-email.html": html,
        f"{stamp}-email_plain.txt": html_to_text(html),
    }
    for name, text in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    eml = out_dir / f"{stamp}-email.eml"
    eml.write_bytes(msg.as_bytes())
    written.append(eml)
    if ics_text:
        ics = out_dir / f"{stamp}-events.ics"
        ics.write_bytes(ics_text.encode("utf-8"))
        written.append(ics)
    meta = out_dir / f"{stamp}-meta.json"
    meta.write_text(
        json.dumps(
            {
                "subject": subject,
                "to": msg.get("To"),
                "from": msg.get("From"),
                "has_ics": bool(ics_text),
                # 注：data-nd-item="1" 是渲染层给每个条目写死的**计数标记**（render 里取值恒为 "1"），
                # 只用于数条目个数。它**不是条目身份标识**（全篇取值相同，区分不了任何两个条目），
                # 因此**禁止**把它当作条目 id / 去重键 / 幂等键的输入。
                "n_items": html.count("data-nd-item="),
                "written_at": stamp,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(meta)
    return written


def _ledger_dir(cfg: Config) -> Path:
    """幂等回执目录：与投递台账同目录（``cfg.db_path`` 的父目录）下的 ``send-receipts/``。

    不放在仓库固定路径，是为了让测试用临时数据库时回执天然隔离 —— 否则「今天已经发过」
    的状态会泄漏给下一次测试或下一轮生产运行。可用 ``NOTICE_DIGEST_LEDGER`` 覆盖。
    """
    override = os.environ.get("NOTICE_DIGEST_LEDGER")
    if override:
        return Path(override).expanduser()
    db_path = getattr(cfg, "db_path", None)
    base = Path(db_path).expanduser().parent if db_path else DEFAULT_LEDGER_DIR
    return base / "send-receipts"


# 页脚里的"生成时间"是**渲染时刻**（render.build_footer_line 的 %H:%M），每次运行都会变。
# 指纹必须是**内容的纯函数**：把这一处渲染时刻归一化掉，否则同一天隔一分钟重跑就会算出
# 不同指纹，两道幂等闸门（store.begin_send 的 attempt 表 / mailer 的同日回执）同时失配，
# 同一份内容会被重复投递（t13 实测的 F1 漏口：+61s 重跑真的发出第二封）。
#
# 注意只归一化**页脚那一处**：正文里的活动/截止时间属于内容，必须留在指纹里，
# 否则"新增一条只在时间上不同的条目"可能不改变指纹，新内容会被自己的闸门吞掉。
_FOOTER_STAMP_RE = re.compile(
    r"(由 notice-digest 生成于 )\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
)


def fingerprint_html(html: str) -> str:
    """返回参与指纹计算的 HTML：只把页脚的渲染时刻替换成固定占位符。"""
    return _FOOTER_STAMP_RE.sub(r"\1<rendered-at>", str(html))


def send_fingerprint(subject: str, n_items: int, html: str) -> str:
    """投递指纹：同一天同一份内容 ⇒ 同一指纹（同日重复发送闸门的键）。

    "同一份内容"不含页脚的渲染时刻（见 ``fingerprint_html``）。
    """
    digest = hashlib.sha256()
    digest.update(str(subject).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(int(n_items)).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(fingerprint_html(html).encode("utf-8"))
    return digest.hexdigest()[:32]


def _receipt_path(cfg: Config, fingerprint: str) -> Path:
    return _ledger_dir(cfg) / f"{datetime.now().strftime('%Y-%m-%d')}-{fingerprint}.json"


def find_receipt(cfg: Config, fingerprint: str):
    """当天是否已有同指纹的投递回执；返回回执 dict，没有或读不出则返回 None。"""
    path = _receipt_path(cfg, fingerprint)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_receipt(cfg: Config, fingerprint: str, subject: str, n_items: int, to_addr) -> None:
    """发送成功后写回执；写失败只告警（"邮件已发出"的事实不受影响）。"""
    path = _receipt_path(cfg, fingerprint)
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "fingerprint": fingerprint,
        "subject": subject,
        "n_items": int(n_items),
        "to_addr": to_addr or "",
        "sent_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(
            f"[mailer] 警告：邮件已发出，但幂等回执写入失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _record_ledger(cfg: Config, subject: str, n_items: int) -> None:
    """投递台账：邮件已经发出才调用；写失败只告警，不改判定。"""
    try:
        from .store import Store

        store = Store(Path(cfg.db_path))
        init = getattr(store, "init_schema", None)
        if callable(init):
            init()
        store.record_send(datetime.now().strftime("%Y-%m-%d"), subject, int(n_items))
    except Exception as exc:  # pragma: no cover - 依赖核心层 store.py
        print(
            f"[mailer] 警告：邮件已发出，但投递台账写入失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def send(
    subject: str,
    html: str,
    ics_text,
    cfg: Config,
    to_addr=None,
    dry_run: bool = False,
) -> bool:
    """发送日报（或 dry-run 落盘）。返回 True=已投递/已落盘，False=失败（原因在 stderr）。"""
    if not subject or not html:
        print("[mailer] 当天没有新条目（空主题哨兵），跳过发送；不发信、不记台账。", file=sys.stderr)
        return True

    # 注：data-nd-item="1" 是渲染层给每个条目写死的**计数标记**（render 里取值恒为 "1"），
    # 只用于数条目个数。它**不是条目身份标识**（全篇取值相同，区分不了任何两个条目），
    # 因此**禁止**把它当作条目 id / 去重键 / 幂等键的输入。
    n_items = html.count("data-nd-item=")
    fingerprint = send_fingerprint(subject, n_items, html)
    try:
        msg, targets = build_message(subject, html, ics_text, cfg, to_addr)
    except Exception as exc:
        print(f"[mailer] 构造邮件失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    if dry_run:
        try:
            written = _write_dry_run(_dry_run_dir(), subject, msg, html, ics_text)
        except OSError as exc:
            print(f"[mailer] dry-run 落盘失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return False
        print(
            f"[mailer] dry-run：未投递，已落盘 {len(written)} 个文件到 {_dry_run_dir()}（{n_items} 条）",
            file=sys.stderr,
        )
        return True

    if not targets["host"] or not targets["to_addr"]:
        print(
            f"[mailer] 缺少投递目标：host={targets['host']!r} to={targets['to_addr']!r}；"
            "请设置 ND_SMTP_HOST / ND_TO_ADDR（.env 或环境变量）。",
            file=sys.stderr,
        )
        return False
    if not targets["user"] or not targets["password"]:
        print(
            "[mailer] 缺少 SMTP 凭据：请设置 ND_SMTP_USER / ND_SMTP_PASS"
            "（.env 或环境变量；口令绝不写进代码）。",
            file=sys.stderr,
        )
        return False

    # 幂等闸门（阶段 A）：同一天同一份内容只投递一次。
    # 阶段 B 交给 store.py 的投递尝试行（写 attempt 行 → 成功改 sent），
    # 这里先用本地回执文件实现，保证「同一天重复运行不重复发信」今天就可验证。
    receipt = find_receipt(cfg, fingerprint)
    if receipt is not None:
        print(
            f"[mailer] 同日同内容已投递过（幂等闸门命中）：fingerprint={fingerprint} "
            f"首次投递={receipt.get('sent_at')} → {receipt.get('to_addr')}；"
            "本次不发信、不重复记台账。",
            file=sys.stderr,
        )
        return True

    try:
        if targets["ssl"]:
            with smtplib.SMTP_SSL(
                targets["host"], targets["port"], timeout=targets["timeout"]
            ) as smtp:
                smtp.login(targets["user"], targets["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(
                targets["host"], targets["port"], timeout=targets["timeout"]
            ) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(targets["user"], targets["password"])
                smtp.send_message(msg)
    except Exception as exc:
        print(
            f"[mailer] SMTP 发送失败：host={targets['host']} port={targets['port']} "
            f"user={targets['user']} to={targets['to_addr']} "
            f"error={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    _write_receipt(cfg, fingerprint, subject, n_items, targets["to_addr"])
    _record_ledger(cfg, subject, n_items)
    print(
        f"[mailer] 已投递：{subject}（{n_items} 条）→ {targets['to_addr']}"
        f"（指纹 {fingerprint}）",
        file=sys.stderr,
    )
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m notice_digest.mailer",
        description="投递日报邮件；--selftest 用样例渲染后走 dry-run 落盘（绝不真发）。",
    )
    parser.add_argument("--selftest", action="store_true", help="用 tests/fixtures/sample_scored.json 渲染")
    parser.add_argument("--dry-run", action="store_true", help="只落盘 data/outbox，不连接 SMTP")
    parser.add_argument("--out", default=None, help="dry-run 落盘目录（默认 data/outbox）")
    parser.add_argument("--fixture", default=None, help="样例文件路径（默认 tests/fixtures/sample_scored.json）")
    args = parser.parse_args(argv)

    if args.out:
        os.environ["NOTICE_DIGEST_OUTBOX"] = str(Path(args.out).expanduser())
    if not args.selftest:
        parser.error("当前只支持 --selftest（生产投递由 cli.py 调用 send()）")
    if not args.dry_run:
        print("[mailer] --selftest 强制 dry-run：不会投递任何邮件。", file=sys.stderr)

    scored, parsed, cfg, now = load_fixture(args.fixture)
    subject, html, ics_text = render_email(scored, parsed, cfg, now)
    ok = send(subject, html, ics_text, cfg, dry_run=True)
    print(f"[mailer] dry-run 自检 {'完成' if ok else '失败'}；主题：{subject}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
