"""渲染层：HTML / 纯文本日报 + ICS 日历附件（只用标准库，服务器零额外依赖）。

对外契约（captain 冻结，cli.py 只做惰性转发）：

    render_email(scored, parsed, cfg, now) -> (subject, html, ics_text)

约定：
* ``("", "", "")`` 是"当天没有新条目"的空主题哨兵，调用方（mailer）必须短路，不发信、不记台账。
* ``Scored`` / ``ParsedTime`` / ``Config`` 一律从核心引擎模块 import，本模块不重复定义。
* 模板用 ``string.Template``（stdlib）渲染，文件名沿用 ``templates/*.j2``，但**不使用 jinja2**。
* 反馈链接与 ``feedback.make_token`` 对齐：``/nd/f?i=..&k=up|down&t=..``、点击跳转 ``/nd/c?i=..&t=..``。
* ``cfg.feedback_base`` 为空（本地开发）时退化为直链真实 url、不渲染反馈按钮，并在邮件顶部明确提示。
"""

from __future__ import annotations

import argparse
import hashlib
import html as _html
import html.parser as _htmlparser
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from urllib.parse import urlencode

from .config import Config
from .feedback import make_token
from .score import Scored
from .timeparse import ParsedTime

__all__ = [
    "render_email",
    "render_ics",
    "render_plain_text",
    "html_to_text",
    "load_fixture",
    "main",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "sample_scored.json"

SUBJECT_PREFIX = "清华通知日报"
SEP = "\uff5c"  # ｜ 全角竖线：主题分隔符，照 captain 给定样例
DOT = "\uff0c"  # ，条目之间
CIRCLED = ("\u2460", "\u2461", "\u2462", "\u2463", "\u2464")  # ①..⑤

# 顶级分节 = pkuknow 网站原生分类（items.category）。13 类按站点内体量从大到小固定排序；
# 不再自创「今天/明天能去 / 截止提醒 / 本周讲座与学术 / 实习就业 / 其他新通知」五桶。
# 紧迫度（今天 / 明天 / 截止）降级为条目标签，见 _urgency_tags。
CATEGORY_ORDER = (
    "校园动态",
    "实习就业",
    "学术科研",
    "社团公益",
    "讲座活动",
    "文体活动",
    "学习成长",
    "生活资讯",
    "院系资讯",
    "交流访学",
    "校园服务",
    "学业教务",
    "奖助评优",
)
# 取值不在 CATEGORY_ORDER 内（含空值 / 纯空白 / 历史旧取值）的条目统一落这一节，永远排在最后。
# 于是「顶级分节标题集合」恒为 CATEGORY_ORDER ∪ {UNCATEGORIZED}：站点将来新增第 14 类时，
# 它先落「未分类」，而不是凭空长出一个没人认识的分节；要正式启用，把它加进 CATEGORY_ORDER 即可。
UNCATEGORIZED = "未分类"
# 单组最多展示条数；超出部分本轮不在正文展开，只在组尾提示条数（不再是全局折叠桶）。
GROUP_LIMIT = 20

SUMMARY_LIMIT = 120
# 组内超限提示（写在组尾）：超出的条目本轮不在正文展开；条目本身仍在库里、下一轮照常参与排序。
GROUP_OVERFLOW_TMPL = "本组还有 {n} 条未列出"
DEV_MODE_NOTE = (
    "本地开发模式：未配置 feedback_base，标题直链原始通知、未渲染反馈按钮，"
    "本条邮件的点击与反馈不会被记录。"
)
DEV_MODE_NO_SECRET_NOTE = (
    "本地开发模式：feedback_base 已配置但缺少 hmac_secret，无法签名反馈链接，"
    "已退化为标题直链、不渲染反馈按钮。"
)

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

try:  # pragma: no cover - 取决于宿主是否带 tzdata
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    # 中国自 1991 年起无夏令时，固定 +08:00 与 Asia/Shanghai 等价
    SHANGHAI = timezone(timedelta(hours=8), "CST")

TZID = "Asia/Shanghai"

# UID 命名空间：只参与 UID 派生，永不掺时间（否则每次重解析都会造出「新日程」）。
UID_NAMESPACE = "thu-lawyer/notice-digest"

_DEFAULT_CONFIG = {
    "campus": "thu",
    "db_path": Path("data/notice-digest.db"),
    "top_n": 30,
    "send_at": "07:30",
    "to_addr": "",
    "from_addr": "",
    "smtp_host": "",
    "smtp_port": 465,
    "feedback_base": "",
    "hmac_secret": "",
    "weights_prior": {},
    "keywords_boost": {},
    "keywords_mute": [],
    "source_boost": {},
    # 兼容 config.py 的 Config.sections 字段（tests/test_score.py 断言它非空）；
    # 渲染层已不再用它决定分节，分节顺序由 CATEGORY_ORDER 决定。
    "sections": list(CATEGORY_ORDER),
}


# --------------------------------------------------------------------------- 小工具


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _squeeze(value) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _truncate(value, limit: int = SUMMARY_LIMIT) -> str:
    text = _squeeze(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _as_shanghai(dt):
    """统一按 Asia/Shanghai 解释；naive 时间视为本地时间。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI)


def _as_datetime(value, default=None):
    if value is None:
        return default
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return default
    return datetime.fromisoformat(text)


def _parsed_of(parsed, scored):
    item = getattr(scored, "item", None) or {}
    key = str(item.get("id") or "")
    return (parsed or {}).get(key)


def _is_undated(p) -> bool:
    return (
        _as_shanghai(getattr(p, "start", None)) is None
        and _as_shanghai(getattr(p, "deadline", None)) is None
    )


def _day_label(day, today) -> str:
    delta = (day - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return f"{day.month}月{day.day}日（{_WEEKDAY_CN[day.weekday()]}）"


def _fmt_clock(dt) -> str:
    return dt.strftime("%H:%M")


def _time_text(p, now) -> str:
    """条目时间文本。没有任何可解析时间时返回空串（由调用方补溯源原文）。"""
    start = _as_shanghai(getattr(p, "start", None))
    end = _as_shanghai(getattr(p, "end", None))
    deadline = _as_shanghai(getattr(p, "deadline", None))
    today = now.date()
    if start is not None:
        text = f"{_day_label(start.date(), today)} {_fmt_clock(start)}"
        if end is not None and end > start:
            if end.date() == start.date():
                text += f"\u2013{_fmt_clock(end)}"
            else:
                text += f"\u2013{_day_label(end.date(), today)} {_fmt_clock(end)}"
        return text
    if deadline is not None:
        return f"截止 {_day_label(deadline.date(), today)} {_fmt_clock(deadline)}"
    return ""


def _evidence_text(p, item) -> str:
    """时间待定时的溯源原文，绝不编造时间。"""
    for candidate in (getattr(p, "evidence", None), item.get("ai_time_evidence"), item.get("ai_event_time")):
        text = _squeeze(candidate)
        if text:
            return text
    return "通知未给出时间"


def _feedback_links(item_id: str, cfg: Config):
    """按 feedback.make_token 的格式签名；cfg.feedback_base 为空时返回 None（开发模式）。

    查询串同时带 ``i=`` 与 ``id=``：契约文本写的是 ``i=``，而服务端 feedback.py 读的是
    ``query.get("id")``（没有 ``i`` 的兼容分支）——两个都带，链接在真实服务上可用，同时
    保留契约参数名。token 用 ``make_token(item_id, kind, cfg)`` 生成（签名口径由 core 决定）。
    """
    base = _squeeze(getattr(cfg, "feedback_base", "")).rstrip("/")
    if not base or not item_id:
        return None
    if not _squeeze(getattr(cfg, "hmac_secret", "")):
        return None
    try:
        tokens = {kind: make_token(item_id, kind, cfg) for kind in ("click", "up", "down")}
    except ValueError:
        # make_token 在缺 ND_HMAC_SECRET 时抛 ValueError → 降级为无反馈按钮（开发模式）
        return None
    query = urlencode({"i": item_id, "id": item_id})
    return {
        "click": f"{base}/nd/c?{query}&t={tokens['click']}",
        "up": f"{base}/nd/f?{query}&k=up&t={tokens['up']}",
        "down": f"{base}/nd/f?{query}&k=down&t={tokens['down']}",
    }


def _dev_note(cfg: Config) -> str:
    base = _squeeze(getattr(cfg, "feedback_base", ""))
    if base:
        return "" if _squeeze(getattr(cfg, "hmac_secret", "")) else DEV_MODE_NO_SECRET_NOTE
    return DEV_MODE_NOTE


def category_of(item: dict) -> str:
    """条目的顶级分节标题 = pkuknow 原生分类；取值不在 13 类内（含空值）一律「未分类」。"""
    name = _squeeze((item or {}).get("category"))
    return name if name in CATEGORY_ORDER else UNCATEGORIZED


def group_by_category(ordered, parsed):
    """按原生分类分组，返回 [(标题, [(scored, parsed), ...]), ...]。

    - 顺序：CATEGORY_ORDER 的顺序，其后是「未分类」；无内容的分类不返回（不渲染空节）。
    - 组内：按个性化分 scored.score 降序，同一天同一分类里越合口味的越靠前。
    """
    buckets: dict = {}
    for scored in ordered:
        item = getattr(scored, "item", None) or {}
        buckets.setdefault(category_of(item), []).append((scored, _parsed_of(parsed, scored)))
    titles = [name for name in CATEGORY_ORDER if name in buckets]
    if UNCATEGORIZED in buckets:
        titles.append(UNCATEGORIZED)
    return [
        (
            name,
            sorted(
                buckets[name],
                key=lambda pair: float(getattr(pair[0], "score", 0.0) or 0.0),
                reverse=True,
            ),
        )
        for name in titles
    ]


def _ordinal(index: int) -> str:
    """①..⑳；超出 20（现实最多 14 个分类）退化为「21.」形式，保证序号唯一可读。"""
    if 1 <= index <= len(CIRCLED):
        return CIRCLED[index - 1]
    if 1 <= index <= 20:
        return chr(0x2460 + index - 1)
    return f"{index}."


def _group_note(hidden: int) -> str:
    """组尾提示：本组还有多少条未列出（0 条返回空串）。"""
    return GROUP_OVERFLOW_TMPL.format(n=hidden) if hidden > 0 else ""


def build_subject(ordered, parsed, now: datetime) -> str:
    """清华通知日报 10-09｜明日 3 场活动，1 项报名今日截止（无活动无截止时退化为「新增 N 条」）。"""
    tomorrow = now.date() + timedelta(days=1)
    n_tomorrow = 0
    n_deadline_today = 0
    for scored in ordered:
        p = _parsed_of(parsed, scored)
        start = _as_shanghai(getattr(p, "start", None))
        bucket = (_squeeze(getattr(p, "bucket", "")) or "").lower()
        if start is not None and start.date() == tomorrow:
            n_tomorrow += 1
        elif start is None and bucket == "tomorrow":
            n_tomorrow += 1
        deadline = _as_shanghai(getattr(p, "deadline", None))
        if deadline is not None and deadline.date() == now.date():
            n_deadline_today += 1
    head = f"{SUBJECT_PREFIX} {now.strftime('%m-%d')}"
    parts = []
    if n_tomorrow:
        parts.append(f"明日 {n_tomorrow} 场活动")
    if n_deadline_today:
        parts.append(f"{n_deadline_today} 项报名今日截止")
    if parts:
        return head + SEP + DOT.join(parts)
    return f"{head}{SEP}新增 {len(ordered)} 条"


# --------------------------------------------------------------------------- 条目


def _urgency_tags(p, now: datetime) -> str:
    """紧迫度标签（条目内标签，不再是顶级分节）：【今天】【明天】【截止 MM-DD】。

    原文没给出任何可解析时间时不编造标签（那种条目的时间文本是「时间待定（原文：…）」）。
    """
    today = now.date()
    tags = []
    start = _as_shanghai(getattr(p, "start", None))
    if start is not None:
        delta = (start.date() - today).days
        if delta == 0:
            tags.append("【今天】")
        elif delta == 1:
            tags.append("【明天】")
    elif (_squeeze(getattr(p, "bucket", "")) or "").lower() == "today":
        # 只有相对文本（「今天」）能解析出 bucket 时，start 为空但语义确定是今天
        tags.append("【今天】")
    deadline = _as_shanghai(getattr(p, "deadline", None))
    if deadline is not None:
        tags.append(f"【截止 {deadline.month:02d}-{deadline.day:02d}】")
    return "".join(tags)


def _item_parts(scored, p, cfg: Config, now: datetime) -> dict:
    item = getattr(scored, "item", None) or {}
    item_id = _squeeze(item.get("id"))
    links = _feedback_links(item_id, cfg)
    real_url = _squeeze(item.get("url"))
    title = _squeeze(item.get("title")) or "(无标题)"
    source = _squeeze(item.get("source_name"))
    place = _squeeze(location_for_item(item))
    time_text = _time_text(p, now)
    evidence = ""
    if not time_text:
        evidence = _evidence_text(p, item)
        time_text = f"时间待定（原文：{evidence}）"
    elif not place and _as_shanghai(getattr(p, "start", None)) is not None:
        place = "地点未注明"
    meta = " · ".join(part for part in (source, time_text, place) if part)
    tags = _urgency_tags(p, now)
    if tags:  # 紧迫度作为条目标签前置，HTML 与纯文本共用同一串元信息
        meta = f"{tags} {meta}"
    return {
        "item_id": item_id,
        "title": title,
        "meta": meta,
        "tags": tags,
        "summary": _truncate(item.get("ai_summary")),
        "evidence": evidence,
        "place": place,
        "links": links,
        "href": links["click"] if links else real_url,
        "real_url": real_url,
    }


def _item_html(scored, p, cfg: Config, now: datetime) -> str:
    parts = _item_parts(scored, p, cfg, now)
    if parts["href"]:
        title_html = (
            f'<a class="nd-link" href="{_esc(parts["href"])}" '
            f'style="color:#1c28f0;text-decoration:none;font-weight:600;">{_esc(parts["title"])}</a>'
        )
    else:
        title_html = f'<span style="font-weight:600;">{_esc(parts["title"])}</span>'
    rows = [title_html]
    if parts["meta"]:
        rows.append(f'<div class="nd-meta" style="margin-top:2px;color:#6b6f6a;font-size:13px;">{_esc(parts["meta"])}</div>')
    if parts["summary"]:
        rows.append(f'<div class="nd-sum" style="margin-top:4px;">{_esc(parts["summary"])}</div>')
    if parts["evidence"]:
        rows.append(
            f'<div class="nd-ev" style="margin-top:4px;color:#8a5a3b;font-size:13px;">'
            f'时间待定，原文溯源：{_esc(parts["evidence"])}</div>'
        )
    if parts["links"]:
        rows.append(
            f'<div class="nd-fb" style="margin-top:6px;color:#6b6f6a;font-size:13px;">'
            f'\U0001f44d <a href="{_esc(parts["links"]["up"])}" style="color:#2f6f4f;text-decoration:none;">有用</a>'
            f' \u2571 '
            f'\U0001f44e <a href="{_esc(parts["links"]["down"])}" style="color:#8a5a3b;text-decoration:none;">少推</a>'
            f"</div>"
        )
    # data-nd-item="1" 是**渲染层写死的计数标记**：每个条目都固定写 "1"，只供下游用
    # html.count("data-nd-item=") 数条目个数。它**不是条目身份标识**（全篇取值相同，
    # 区分不了任何两个条目），因此**禁止**把它当作条目 id / 去重键 / 幂等键的输入。
    return (
        '<li class="nd-item" data-nd-item="1" '
        'style="margin:0 0 14px;padding:0 0 12px;border-bottom:1px solid #eceeeb;list-style:none;">'
        + "".join(rows)
        + "</li>"
    )


def _section_html(index: int, title: str, entries, cfg: Config, now: datetime, note: str = "") -> str:
    items = "".join(_item_html(scored, p, cfg, now) for scored, p in entries)
    note_html = (
        f'<p style="margin:8px 0 0;color:#8a8f89;font-size:13px;">{_esc(note)}</p>' if note else ""
    )
    return (
        '<section class="nd-section" style="margin:0 0 18px;">'
        '<h2 style="font-size:16px;margin:0 0 10px;padding-left:8px;border-left:3px solid #5e7868;">'
        f"{_esc(_ordinal(index))} {_esc(title)}"
        f'<span style="color:#8a8f89;font-size:13px;font-weight:400;">（{len(entries)} 条）</span>'
        "</h2>"
        f'<ul style="margin:0;padding:0;">{items}</ul>'
        f"{note_html}"
        "</section>"
    )


def _item_text(scored, p, cfg: Config, now: datetime, seq: int) -> str:
    parts = _item_parts(scored, p, cfg, now)
    lines = [f"{seq}. {parts['title']}"]
    if parts["meta"]:
        lines.append(f"   {parts['meta']}")
    if parts["summary"]:
        lines.append(f"   {parts['summary']}")
    if parts["evidence"]:
        lines.append(f"   时间待定，原文溯源：{parts['evidence']}")
    if parts["href"]:
        lines.append(f"   链接：{parts['href']}")
    if parts["links"]:
        lines.append(f"   反馈：\U0001f44d {parts['links']['up']}")
        lines.append(f"         \U0001f44e {parts['links']['down']}")
    return "\n".join(lines)


def _section_text(index: int, title: str, entries, cfg: Config, now: datetime, note: str = "") -> str:
    body = []
    for seq, (scored, p) in enumerate(entries, start=1):
        body.append(_item_text(scored, p, cfg, now, seq))
    head = f"{_ordinal(index)} {title}（{len(entries)} 条）"
    chunks = [head, "\n".join(body)]
    if note:  # 组尾提示：同 HTML 版，写在正文之后
        chunks.append(note)
    return "\n".join(chunks)


# --------------------------------------------------------------------------- 模板


def _load_template(name: str) -> Template:
    path = TEMPLATE_DIR / name
    return Template(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 分节渲染


def _render_sections(ordered, parsed, cfg: Config, now: datetime, renderer) -> list:
    """按原生分类逐节渲染，返回已渲染的字符串列表。

    - 每个当日有内容的分类保底展示（空分类不产生分节）；
    - 组内已按个性化分降序（见 group_by_category）；
    - 单组最多 GROUP_LIMIT 条，超出部分只在组尾提示条数。
    """
    chunks = []
    for index, (title, entries) in enumerate(group_by_category(ordered, parsed), start=1):
        shown = entries[:GROUP_LIMIT] if GROUP_LIMIT > 0 else entries
        note = _group_note(len(entries) - len(shown))
        chunks.append(renderer(index, title, shown, cfg, now, note))
    return chunks


def render_email(scored, parsed, cfg: Config, now: datetime) -> tuple[str, str, str]:
    """渲染日报，返回 (subject, html, ics_text)。

    当天没有新条目时返回 ``("", "", "")``：调用方必须短路（不发信、台账不记）。
    """
    now = _as_shanghai(_as_datetime(now, datetime.now(SHANGHAI)))
    ordered = sorted(
        list(scored or []),
        key=lambda s: float(getattr(s, "score", 0.0) or 0.0),
        reverse=True,
    )
    if not ordered:
        return ("", "", "")

    parsed = parsed or {}
    subject = build_subject(ordered, parsed, now)
    html_sections = _render_sections(ordered, parsed, cfg, now, _section_html)
    text_sections = _render_sections(ordered, parsed, cfg, now, _section_text)

    dev_note = _dev_note(cfg)
    meta_line = build_meta_line(ordered, parsed, now)
    footer = build_footer_line(ordered, now)
    banner_html = (
        f'<p class="nd-banner" style="margin:0 0 14px;padding:8px 10px;background:#fdf6e3;'
        f'border-left:3px solid #d9a441;color:#7a5a17;font-size:13px;">{_esc(dev_note)}</p>'
        if dev_note
        else ""
    )

    html = _load_template("email.html.j2").substitute(
        subject=_esc(subject),
        meta_line=_esc(meta_line),
        banner=banner_html,
        sections="\n".join(html_sections),
        footer=_esc(footer),
    )
    ics_text = render_ics(ordered, parsed, cfg, now)
    return subject, html, ics_text


def render_plain_text(scored, parsed, cfg: Config, now: datetime) -> str:
    """纯文本版（给不支持 HTML 的客户端；也供 --selftest 落盘 *_plain.txt）。"""
    now = _as_shanghai(_as_datetime(now, datetime.now(SHANGHAI)))
    ordered = sorted(
        list(scored or []),
        key=lambda s: float(getattr(s, "score", 0.0) or 0.0),
        reverse=True,
    )
    if not ordered:
        return ""
    parsed = parsed or {}
    subject = build_subject(ordered, parsed, now)
    sections = _render_sections(ordered, parsed, cfg, now, _section_text)
    return _load_template("email.txt.j2").substitute(
        subject=subject,
        meta_line=build_meta_line(ordered, parsed, now),
        banner=_dev_note(cfg),
        sections="\n\n".join(sections),
        footer=build_footer_line(ordered, now),
    )


# --------------------------------------------------------------------------- ICS


def _ics_esc(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> list:
    """按 75 字节折行；续行以空格开头（空格计入 75 字节）。只在字符边界折。"""
    chunks, current, used, limit = [], "", 0, 75
    for char in line:
        size = len(char.encode("utf-8"))
        if current and used + size > limit:
            chunks.append(current)
            current, used, limit = char, size, 74
        else:
            current += char
            used += size
    chunks.append(current)
    if len(chunks) == 1:
        return chunks
    return [chunks[0]] + [" " + chunk for chunk in chunks[1:]]


def _uid(item_id: str, start: datetime | None = None) -> str:
    """UID 只由稳定标识派生（固定命名空间 + item_id），**不掺时间**。

    ``start`` 参数只为兼容旧调用签名保留、不参与派生：同一条通知无论时间字段
    如何重解析，UID 恒定 —— 日历客户端才会按 SEQUENCE / LAST-MODIFIED 更新既有
    条目，而不是把同一条通知插成两个日程。
    """
    digest = hashlib.sha1(f"{UID_NAMESPACE}|{item_id}".encode("utf-8")).hexdigest()[:16]
    return f"{digest}@notice-digest"


def _sequence_of(item) -> int:
    """修订号：条目自带 sequence / seq / revision 时用之，否则 0（首次发布）。"""
    if not isinstance(item, dict):
        return 0
    for key in ("sequence", "seq", "revision"):
        raw = item.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


def _last_modified(item, fallback: str) -> str:
    """LAST-MODIFIED（UTC、Z 结尾）：取条目自身时间戳，取不到退回 DTSTAMP。"""
    if isinstance(item, dict):
        for key in ("updated_at", "last_seen_at", "detail_fetched_at", "first_seen_at"):
            stamp = _as_datetime(item.get(key))
            if stamp is not None:
                return _as_shanghai(stamp).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return fallback


# 日期粒度判定：解析器只给当地 00:00、且原文没有任何钟点线索 ⇒ 全天事件。
# 把「10 月 10 日」当 10 月 10 日 00:00 的定时事件，会让 -PT30M 提醒落到前一天 23:30。
_CLOCK_HINT = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*[点时]")


def _time_evidence_text(item, p) -> str:
    """条目上所有可能带钟点的原文片段（只用于判定日期粒度）。"""
    item = item if isinstance(item, dict) else {}
    parts = []
    for candidate in (
        getattr(p, "evidence", None),
        item.get("ai_event_time"),
        item.get("ai_time_text"),
        item.get("ai_time_evidence"),
        item.get("event_time_text"),
        item.get("time_text"),
    ):
        text = _squeeze(candidate)
        if text:
            parts.append(text)
    return " ".join(parts)


def is_all_day(p, item=None) -> bool:
    """日期粒度判定（ParsedTime 没有粒度标记，只能看时间与原文线索）。"""
    start = _as_shanghai(getattr(p, "start", None))
    if start is None:
        return False
    if (start.hour, start.minute, start.second) != (0, 0, 0):
        return False
    end = _as_shanghai(getattr(p, "end", None))
    if end is not None:
        if (end.hour, end.minute, end.second) != (0, 0, 0):
            return False  # 结束时间带钟点 ⇒ 定时事件
        if end < start:
            return False
    return not _CLOCK_HINT.search(_time_evidence_text(item, p))


def _all_day_end(start: datetime, end: datetime | None) -> datetime:
    """全天事件的 DTEND（RFC 5545 半开区间）：单日取次日，区间取解析出的结束日。"""
    if end is not None and end > start:
        return end
    return start + timedelta(days=1)


# 中国无夏令时：恒 +0800 的 STANDARD 块即满足 RFC 5545 §3.6.5 对 TZID 引用的要求。
# 生效起点取「运行年份的上一年 1 月 1 日」而不是 Unix 纪元起点：§3.6.5 规定 STANDARD/DAYLIGHT
# 的 DTSTART 是**本地时间**（不得带 TZID），任何本地起点都合规；Asia/Shanghai 无夏令时、
# TZOFFSETFROM/TO 恒为 +0800，起点位置不影响任何换算结果。
def vtimezone_block(now: datetime | None = None) -> tuple[str, ...]:
    if isinstance(now, datetime):
        year = _as_shanghai(now).year
    else:
        year = datetime.now(SHANGHAI).year
    return (
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        "BEGIN:STANDARD",
        f"DTSTART:{year - 1:04d}0101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "RRULE:FREQ=YEARLY;BYMONTH=1;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    )


# 兼容旧调用点（不带 now 时按当前年份生成）。
VTIMEZONE_BLOCK = vtimezone_block()


# 地点口径（与 enrich.location_for_item 同序）：结构化字段优先，再退回本地规则。
_LOCATION_KEYS = ("event_location", "location", "ai_event_location")
_UNSET = object()
_extract_location = _UNSET


def _local_extract_location(text: str):
    """本地规则：优先复用 enrich.extract_location（只读导入），缺失时不做本地抽取。"""
    global _extract_location
    if _extract_location is _UNSET:
        try:
            from .enrich import extract_location as _fn
        except Exception:  # pragma: no cover - enrich 不可用时降级
            _fn = None
        _extract_location = _fn
    if _extract_location is None or not text:
        return None
    try:
        return _extract_location(text)
    except Exception:  # pragma: no cover - 抽取失败不影响投递
        return None


def location_for_item(item) -> str | None:
    """地点取值：顶层与 detail 里依次看 event_location / location / ai_event_location。

    与 enrich 读同一串字段，再退回同一个本地抽取规则，避免两处口径漂移。
    """
    item = item if isinstance(item, dict) else {}
    detail = item.get("detail")
    sources = [item]
    if isinstance(detail, dict):
        sources.append(detail)
    for source in sources:
        for key in _LOCATION_KEYS:
            value = _squeeze(source.get(key))
            if value:
                return value
    detail = detail if isinstance(detail, dict) else {}
    text = "\n".join(
        part
        for part in (
            _squeeze(item.get("title")),
            _squeeze(item.get("summary")),
            _squeeze(item.get("ai_summary")),
            _squeeze(detail.get("content") or detail.get("content_markdown")),
        )
        if part
    )
    return _local_extract_location(text)


def build_meta_line(ordered, parsed, now) -> str:
    """HTML 与纯文本共用的 meta 行（两版必须逐字一致）。"""
    n_events = sum(
        1
        for scored_item in ordered
        if _as_shanghai(getattr(_parsed_of(parsed, scored_item), "start", None))
    )
    return (
        f"{now.strftime('%Y-%m-%d')}（{_WEEKDAY_CN[now.weekday()]}） · "
        f"共 {len(ordered)} 条新通知 · 日历附件 {n_events} 项"
    )


def build_footer_line(ordered, now) -> str:
    """HTML 与纯文本共用的落款（两版必须逐字一致）。"""
    return (
        f"由 notice-digest 生成于 {now.strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai），"
        f"共 {len(ordered)} 条；点击与反馈用于个性化排序。"
    )


def render_ics(scored, parsed, cfg: Config, now: datetime) -> str:
    """手写 ICS：CRLF、75 字节折行、VALARM 提前 30 分钟；无明确起止时间的条目一律不写入。"""
    now = _as_shanghai(_as_datetime(now, datetime.now(SHANGHAI)))
    parsed = parsed or {}
    dtstamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//thu-lawyer//notice-digest//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_esc(SUBJECT_PREFIX)}",
        f"X-WR-TIMEZONE:{TZID}",
    ]
    header_len = len(lines)  # VTIMEZONE 插入位：只有事件真的引用 TZID 时才写该块
    needs_tz = False
    for scored_item in sorted(
        list(scored or []),
        key=lambda s: float(getattr(s, "score", 0.0) or 0.0),
        reverse=True,
    ):
        p = _parsed_of(parsed, scored_item)
        start = _as_shanghai(getattr(p, "start", None))
        if start is None:
            continue  # 没有明确起始时间：不造时间、不写入日历
        end = _as_shanghai(getattr(p, "end", None))
        item = getattr(scored_item, "item", None) or {}
        item_id = _squeeze(item.get("id"))
        title = _squeeze(item.get("title")) or "(无标题)"
        place = _squeeze(location_for_item(item))
        summary = _truncate(item.get("ai_summary"))
        source = _squeeze(item.get("source_name"))
        real_url = _squeeze(item.get("url"))
        links = _feedback_links(item_id, cfg)
        description = " ".join(
            part
            for part in (
                summary,
                source,
                f"原文：{real_url}" if real_url else "",
                f"点击记录：{links['click']}" if links else "",
            )
            if part
        )
        all_day = is_all_day(p, item)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_uid(item_id or title)}")
        lines.append(f"DTSTAMP:{dtstamp}")
        lines.append(f"LAST-MODIFIED:{_last_modified(item, dtstamp)}")
        lines.append(f"SEQUENCE:{_sequence_of(item)}")
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{_all_day_end(start, end).strftime('%Y%m%d')}")
        else:
            needs_tz = True  # 定时事件引用了 TZID=Asia/Shanghai ⇒ 必须给出同名 VTIMEZONE
            lines.append(f"DTSTART;TZID={TZID}:{start.strftime('%Y%m%dT%H%M%S')}")
            if end is not None and end > start:
                lines.append(f"DTEND;TZID={TZID}:{end.strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"SUMMARY:{_ics_esc(title)}")
        if place:
            lines.append(f"LOCATION:{_ics_esc(place)}")
        if description:
            lines.append(f"DESCRIPTION:{_ics_esc(description)}")
        if real_url.startswith(("http://", "https://")):
            lines.append(f"URL:{real_url}")
        lines.append("STATUS:CONFIRMED")
        lines.append("BEGIN:VALARM")
        if all_day:
            # 全天事件不能用相对触发器（相对 00:00 会把提醒甩到前一天 23:30）：
            # 改用绝对时间 = 当天 07:30（Asia/Shanghai）。
            fire = (start + timedelta(hours=7, minutes=30)).astimezone(timezone.utc)
            lines.append(f"TRIGGER;VALUE=DATE-TIME:{fire.strftime('%Y%m%dT%H%M%SZ')}")
        else:
            lines.append("TRIGGER:-PT30M")
        lines.append("ACTION:DISPLAY")
        lines.append(f"DESCRIPTION:{_ics_esc('提醒：' + title)}")
        lines.append("END:VALARM")
        lines.append("END:VEVENT")
    if needs_tz:
        # 只有真的引用了 TZID 才声明 VTIMEZONE：整份日历全是全天事件时（VALUE=DATE，按
        # RFC 5545 §3.3.5 不带 TZID）不写该块，免得留下一条任何事件都不引用的裸本地
        # DTSTART 行（合规但无意义，且会被「所有 DTSTART 必须带 TZID」式断言误判）。
        lines[header_len:header_len] = list(vtimezone_block(now))
    lines.append("END:VCALENDAR")

    folded = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"


# --------------------------------------------------------------------------- HTML → 纯文本


_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "li", "ul", "ol", "section", "tr", "table", "blockquote"}


class _TextExtractor(_htmlparser.HTMLParser):
    """把渲染出的 HTML 转为纯文本（stdlib，无第三方依赖）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []
        self._href = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "br":
            self._out.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
        elif tag in ("h1", "h2", "h3"):
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            self._out.append(f" <{self._href}>")
            self._href = None
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """HTML → 纯文本。契约签名只传 html，投递层的 text/plain 备选由此生成。"""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.text()


# --------------------------------------------------------------------------- 样例


def _config_from_dict(overrides: dict) -> Config:
    payload = dict(_DEFAULT_CONFIG)
    payload.update(overrides or {})
    if payload.get("db_path") is not None:
        payload["db_path"] = Path(payload["db_path"])
    return Config(**payload)


def _parsed_from_dict(raw: dict) -> ParsedTime:
    return ParsedTime(
        start=_as_datetime(raw.get("start")),
        end=_as_datetime(raw.get("end")),
        deadline=_as_datetime(raw.get("deadline")),
        bucket=_squeeze(raw.get("bucket")) or "undated",
        evidence="" if raw.get("evidence") is None else str(raw.get("evidence")),
    )


def load_fixture(path=None):
    """读取 tests/fixtures/sample_scored.json，构造 (scored, parsed, cfg, now)。"""
    fixture_path = Path(path) if path else FIXTURE_PATH
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    now = _as_shanghai(_as_datetime(payload.get("now"), datetime.now(SHANGHAI)))
    cfg = _config_from_dict(payload.get("config") or {})
    scored, parsed = [], {}
    for entry in payload.get("items") or []:
        item = dict(entry.get("item") or {})
        item.setdefault("title", "")
        parsed_time = _parsed_from_dict(entry.get("parsed") or {})
        parsed[str(item.get("id") or "")] = parsed_time
        scored.append(
            Scored(
                item=item,
                score=float(entry.get("score") or 0.0),
                reasons=[tuple(reason) for reason in (entry.get("reasons") or [])],
            )
        )
    return scored, parsed, cfg, now


# --------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m notice_digest.render",
        description="渲染日报邮件（HTML + 纯文本）与 ICS 日历附件；--selftest 用样例落盘验证。",
    )
    parser.add_argument("--selftest", action="store_true", help="用 tests/fixtures/sample_scored.json 渲染样例")
    parser.add_argument("--out", default="data/selftest", help="自检产物目录（默认 data/selftest）")
    parser.add_argument("--fixture", default=None, help="样例文件路径（默认 tests/fixtures/sample_scored.json）")
    args = parser.parse_args(argv)
    if not args.selftest:
        parser.error("当前只支持 --selftest")

    scored, parsed, cfg, now = load_fixture(args.fixture)
    subject, html, ics_text = render_email(scored, parsed, cfg, now)
    plain = render_plain_text(scored, parsed, cfg, now)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "subject.txt").write_text(subject + "\n", encoding="utf-8")
    (out_dir / "email.html").write_text(html, encoding="utf-8")
    (out_dir / "email_plain.txt").write_text(plain, encoding="utf-8")
    (out_dir / "events.ics").write_bytes(ics_text.encode("utf-8"))
    print(f"[render] 自检产物：{out_dir}（subject.txt / email.html / email_plain.txt / events.ics）")
    print(f"[render] 主题：{subject}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
