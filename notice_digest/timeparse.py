"""中文自由文本时间解析（纯标准库）。

这是本工具的核心价值所在：站点详情里 ``event_start`` / ``event_end`` /
``time_text`` / ``deadline`` 全为 null，只给自由文本 ``ai_event_time``
（如「2026年10月10日（周六）」）。本地把自由文本规范化为结构化时间。

覆盖（至少）：
    今天/今晚/明天/后天、本周X/下周X/周X、X月X日（周X）、
    即日起至X月X日、报名截止X月X日、X日X时、X:X 起

输出 ``ParsedTime(start, end, deadline, bucket, evidence)``；
``bucket`` ∈ today/tomorrow/this_week/next_week/undated/past。

关于 bucket 的语义：它是**紧迫度窗口**而非精确归属 —— 超出「下周」的未来日期
（保留 start 供渲染层显示）与完全无时间的条目统一归 ``undated``。
解析不出时绝不丢弃条目：``evidence`` 保留 ``ai_time_evidence`` 原文以溯源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

TZ_SHANGHAI = timezone(timedelta(hours=8))

BUCKETS = ("today", "tomorrow", "this_week", "next_week", "undated", "past")

#: 正文里搜索时间的最大字符数（正文可能极长）
BODY_SCAN_LIMIT = 20000

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９：／－～　",
    "0123456789:/-~ ",
)

_WEEKDAY_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

#: 相对日偏移
_RELATIVE = {
    "今天": 0, "今日": 0, "今晚": 0, "今早": 0, "今晨": 0, "今夜": 0,
    "明天": 1, "明日": 1, "明晚": 1, "明早": 1, "明晨": 1, "明夜": 1,
    "后天": 2, "后日": 2, "後天": 2, "后晚": 2,
}
_RELATIVE_EVENING = {"今晚", "今夜", "明晚", "明夜", "后晚"}

#: 截止类关键词（用于抽取报名/投稿截止时间）
_DEADLINE_KEYWORDS = (
    "截止", "截止日期", "截止时间", "报名截止", "申请截止", "投稿截止",
    "征稿截止", "提交截止", "投递截止", "注册截止", "缴费截止", "报名时间",
    "报名日期", "前提交", "前完成", "前报名", "结束时间", "截至",
)

_RE_RELATIVE = re.compile("|".join(sorted(map(re.escape, _RELATIVE), key=len, reverse=True)))
_RE_WEEKDAY = re.compile(
    r"(本周|这周|这个周|本星期|下周|下个?周|下星期|周|週|星期|礼拜)\s*([一二三四五六日天])"
)
_RE_DATE = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
    r"(?:\s*[（(]?\s*(?:周|週|星期|礼拜)\s*([一二三四五六日天])\s*[)）]?)?"
)
_RE_DAY_ONLY = re.compile(r"(?:^|[^\d])(\d{1,2})\s*[日号]")
_RE_TIME = re.compile(
    r"(?:(\d{1,2})\s*[:：]\s*(\d{1,2})"
    r"|(\d{1,2})\s*[点时]\s*(半)"
    r"|(\d{1,2})\s*[点时]\s*(?:(\d{1,2})\s*分?)?)"
)
_RE_PERIOD = re.compile(r"(上午|早上|早晨|清晨|中午|下午|傍晚|晚上|晚间|晚|夜|上午|凌晨)")
_RE_RANGE_SEP = re.compile(r"\s*(?:至|到|—|–|~|～|－|-)\s*")
_RE_START_NOW = re.compile(r"即日起|自即日|从即日|今日起|从现在起")


@dataclass
class ParsedTime:
    start: datetime | None = None
    end: datetime | None = None
    deadline: datetime | None = None
    bucket: str = "undated"
    evidence: str = ""

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "bucket": self.bucket,
            "evidence": self.evidence,
        }


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return str(text).translate(_FULLWIDTH)


def _ensure_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_SHANGHAI)
    return now


def _period_hour(period: str | None) -> int | None:
    if not period:
        return None
    if period in ("上午", "早上", "早晨", "清晨"):
        return 9
    if period == "中午":
        return 12
    if period in ("下午",):
        return 14
    if period in ("傍晚", "晚上", "晚间", "晚", "夜", "凌晨"):
        return 19 if period != "凌晨" else 6
    return None


def _apply_period(hour: int, period: str | None) -> int:
    """把 12 小时制的 3 点结合「下午/晚上」修正为 15/... 点。"""
    if period in ("下午", "傍晚", "晚上", "晚间", "晚", "夜") and 1 <= hour <= 11:
        return hour + 12
    return hour


def _extract_time(text: str) -> tuple[int, int] | None:
    """在片段里找第一个时刻，返回 (hour, minute)。"""
    m = _RE_TIME.search(text)
    if not m:
        return None
    if m.group(1) is not None:
        hour, minute = int(m.group(1)), int(m.group(2))
    elif m.group(4):  # X点半 / X时半
        hour, minute = int(m.group(3)), 30
    else:
        hour = int(m.group(5))
        minute = int(m.group(6)) if m.group(6) else 0
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return hour, minute


def _period_before(text: str, pos: int) -> str | None:
    window = text[max(0, pos - 6) : pos]
    matches = list(_RE_PERIOD.finditer(window))
    return matches[-1].group(1) if matches else None


def _make_dt(day: date, hour: int, minute: int, now: datetime) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo or TZ_SHANGHAI)


def _infer_year(month: int, day: int, now: datetime) -> int:
    """补全年份：明显已过去很久的月日视为下一年（跨年通知）。"""
    year = now.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return year
    if candidate < now.date() - timedelta(days=180):
        return year + 1
    return year


def _weekday_of(day: date) -> int:
    return day.weekday()


def _resolve_weekday(target: int, now: datetime, scope: str | None) -> date:
    """把「周X / 本周X / 下周X」解析成具体日期。"""
    today = now.date()
    this_monday = today - timedelta(days=_weekday_of(today))
    if scope in ("下周", "下个周", "下星期"):
        base = this_monday + timedelta(days=7)
    elif scope in ("本周", "这周", "这个周", "本星期"):
        base = this_monday
    else:
        # 无范围词：取「今天起到下一个该星期几」，跨周则顺延到下周
        delta = (target - _weekday_of(today)) % 7
        base = today
        return base + timedelta(days=delta)
    return base + timedelta(days=target)


def _match_start_now(text: str, now: datetime) -> tuple[datetime, str] | None:
    m = _RE_START_NOW.search(text)
    if not m:
        return None
    return _make_dt(now.date(), 0, 0, now), m.group(0)


def _parse_absolute(text: str, now: datetime) -> tuple[datetime, str] | None:
    """解析 X月X日（周X）/ 带年份的完整日期。"""
    m = _RE_DATE.search(text)
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else _infer_year(int(m.group(2)), int(m.group(3)), now)
    month, day = int(m.group(2)), int(m.group(3))
    try:
        base = date(year, month, day)
    except ValueError:
        return None
    hour, minute = 0, 0
    tm = _extract_time(text[m.end() : m.end() + 24])
    if tm:
        hour, minute = tm
        period = _period_before(text, m.end() + _RE_TIME.search(text[m.end() : m.end() + 24]).start())
        hour = _apply_period(hour, period)
    return _make_dt(base, hour, minute, now), m.group(0)


def _parse_day_only(text: str, now: datetime) -> tuple[datetime, str] | None:
    """解析「X日X时」（无月份），用于「截止5日17时」。"""
    m = _RE_DAY_ONLY.search(text)
    if not m:
        return None
    day = int(m.group(1))
    if not (1 <= day <= 31):
        return None
    year, month = now.year, now.month
    try:
        base = date(year, month, day)
    except ValueError:
        return None
    if base < now.date() - timedelta(days=20):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        try:
            base = date(year, month, day)
        except ValueError:
            return None
    hour, minute = 0, 0
    tm = _extract_time(text[m.end() : m.end() + 16])
    if tm:
        hour, minute = tm
    return _make_dt(base, hour, minute, now), m.group(0)


def _parse_weekday(text: str, now: datetime) -> tuple[datetime, str] | None:
    m = _RE_WEEKDAY.search(text)
    if not m:
        return None
    scope = m.group(1)
    target = _WEEKDAY_CHARS.get(m.group(2), 0)
    day = _resolve_weekday(target, now, scope if scope not in ("周", "週", "星期", "礼拜") else None)
    hour, minute = 0, 0
    tm = _extract_time(text[m.end() : m.end() + 24])
    if tm:
        hour, minute = tm
        tm_match = _RE_TIME.search(text[m.end() : m.end() + 24])
        period = _period_before(text, m.end() + tm_match.start()) if tm_match else None
        hour = _apply_period(hour, period)
    return _make_dt(day, hour, minute, now), m.group(0)


def _parse_relative(text: str, now: datetime) -> tuple[datetime, str] | None:
    m = _RE_RELATIVE.search(text)
    if not m:
        return None
    token = m.group(0)
    day = now.date() + timedelta(days=_RELATIVE[token])
    hour, minute = 0, 0
    tm = _extract_time(text[m.end() : m.end() + 20])
    if tm:
        hour, minute = tm
        tm_match = _RE_TIME.search(text[m.end() : m.end() + 20])
        period = _period_before(text, m.end() + tm_match.start()) if tm_match else token
        hour = _apply_period(hour, period)
    elif token in _RELATIVE_EVENING:
        hour = 19
    return _make_dt(day, hour, minute, now), token


#: 解析器优先级（先精确后模糊）
_PARSERS = (_parse_relative, _parse_absolute, _parse_weekday, _parse_day_only)


def _first_datetime(text: str, now: datetime) -> tuple[datetime, str] | None:
    """按位置取最靠前的一个时间表达（各解析器各取首个匹配，再比位置）。"""
    text = _norm(text)
    best: tuple[int, datetime, str] | None = None
    for parser in _PARSERS:
        found = parser(text, now)
        if not found:
            continue
        dt, snippet = found
        pos = text.find(snippet)
        if pos < 0:
            pos = 10**9
        if best is None or pos < best[0]:
            best = (pos, dt, snippet)
    if best is None:
        return None
    return best[1], best[2]


def _deadline_window(text: str, now: datetime) -> tuple[datetime, str] | None:
    """在「截止」类关键词附近找日期。"""
    text = _norm(text)
    for kw in _DEADLINE_KEYWORDS:
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx < 0:
                break
            window = text[idx : idx + 40]
            found = _first_datetime(window, now)
            if found:
                return found
            # 关键词前也可能写日期：「10月8日截止」
            back = text[max(0, idx - 24) : idx + len(kw)]
            found = _first_datetime(back, now)
            if found:
                return found
            start = idx + len(kw)
    return None


def _range_in(text: str, now: datetime) -> tuple[datetime | None, datetime | None, str] | None:
    """识别「即日起至X月X日」「X月X日至X月X日」区间。"""
    text = _norm(text)
    m = _RE_RANGE_SEP.search(text)
    if not m:
        return None
    head, tail = text[: m.start()], text[m.end() :]
    left = _first_datetime(head[-60:], now) if head.strip() else None
    right = _first_datetime(tail[:60], now) if tail.strip() else None
    if left is None and right is None:
        return None
    if left is None:
        start_now = _match_start_now(text, now)
        if start_now:
            left = start_now
    if left is None or right is None:
        return None
    snippet = f"{left[1]} 至 {right[1]}"
    # 方向修正：结束早于开始时按跨年处理
    if right[0] < left[0]:
        right = (right[0].replace(year=right[0].year + 1), right[1])
    return left[0], right[0], snippet


def _far_bucket(dt: datetime, now: datetime) -> str:
    today = now.date()
    delta = (dt.date() - today).days
    if delta < 0:
        return "past"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    this_monday = today - timedelta(days=today.weekday())
    week_start = this_monday
    week_end = this_monday + timedelta(days=6)
    if week_start <= dt.date() <= week_end:
        return "this_week"
    next_start = week_end + timedelta(days=1)
    next_end = next_start + timedelta(days=6)
    if next_start <= dt.date() <= next_end:
        return "next_week"
    return "undated"


def _bucket_for(
    start: datetime | None,
    deadline: datetime | None,
    now: datetime,
    end: datetime | None = None,
) -> str:
    """判定时间桶。t7(F3)：区间一律以「区间末日」为准，覆盖今天即不再判 past。"""
    if end is not None:
        rng = _range_bucket(start, end, now)
        if rng != "undated":
            return rng
    if start is not None:
        b = _far_bucket(start, now)
        if b != "undated":
            return b
        if deadline is not None:
            return _far_bucket(deadline, now)
        return "undated"
    if deadline is not None:
        return _far_bucket(deadline, now)
    return "undated"


def _range_bucket(start: datetime | None, end: datetime, now: datetime) -> str:
    today = now.date()
    if end.date() < today:
        return "past"
    b = _far_bucket(end, now)
    if b in ("today", "tomorrow", "this_week", "next_week"):
        return b
    if start is not None and start.date() <= today <= end.date():
        return "this_week"
    return b


def bucket_for(start=None, end=None, deadline=None, now=None) -> str:
    """公开时间桶入口（enrich 用结构化字段时保持同一口径）。"""
    if now is None:
        now = datetime.now(TZ_SHANGHAI)
    now = _ensure_aware(now)
    b = _bucket_for(start, deadline, now, end=end)
    return b if b in BUCKETS else "undated"


STATION_DEADLINE_MAX_DAYS = 180


def is_placeholder_time(parsed) -> bool:
    """占位 00:00（只有日期没给时刻）不算确定时刻。"""
    st = getattr(parsed, "start", None)
    return bool(st is not None and st.hour == 0 and st.minute == 0)


def time_status(parsed) -> str:
    """ok | unconfirmed | none —— 邮件的「时间未确认」标注口径。

    只有「单个起止点且时刻是 00:00 占位」才算未确认；
    有 end/deadline 的区间（如「9月22日-10月9日」）日期本身是明确的，判 ok。
    """
    if getattr(parsed, "start", None) is None:
        return "none"
    if getattr(parsed, "end", None) is not None or getattr(parsed, "deadline", None) is not None:
        return "ok"
    return "unconfirmed" if is_placeholder_time(parsed) else "ok"


def time_label(parsed) -> str:
    st = time_status(parsed)
    return {"none": "时间待定", "unconfirmed": "时间未确认"}.get(st, "")


def plausible_station_deadline(start, deadline, now) -> bool:
    """站点 deadline 实测不可信（唯一非空那条是招聘年龄截止 2027-06-30）：
    只在「有 start 且 deadline 在其后 180 天内」当真截止，否则只当弱信号。"""
    if deadline is None:
        return False
    if start is not None and deadline < start:
        return False
    base = start or now
    return (deadline - base).days <= STATION_DEADLINE_MAX_DAYS



def parse_item_time(
    title: str,
    ai_event_time: str | None,
    ai_time_evidence: str | None,
    body: str | None,
    now: datetime,
) -> ParsedTime:
    """解析条目时间。永远返回 ParsedTime，绝不抛异常、绝不丢弃条目。"""
    try:
        now = _ensure_aware(now)
        title = title or ""
        ai_event_time = _norm(ai_event_time)
        ai_time_evidence = _norm(ai_time_evidence)
        body = _norm(body)[:BODY_SCAN_LIMIT]

        # 文本来源优先级：结构化程度从高到低
        sources: list[tuple[str, str]] = []
        for label, text in (
            ("ai_event_time", ai_event_time),
            ("ai_time_evidence", ai_time_evidence),
            ("title", title),
            ("body", body),
        ):
            if text and text.strip():
                sources.append((label, text.strip()))

        # ai_time_evidence 原文永远保留用于溯源
        raw_evidence = (ai_time_evidence or ai_event_time or "").strip()

        start: datetime | None = None
        end: datetime | None = None
        deadline: datetime | None = None
        evidence = ""

        # 1) 截止时间：优先在事件时间文本里找，再退化到正文
        for label, text in sources:
            found = _deadline_window(text, now)
            if found:
                deadline = found[0]
                if not evidence:
                    evidence = found[1]
                break

        # 2) 区间
        for label, text in sources:
            rng = _range_in(text, now)
            if rng:
                start, end, snippet = rng
                evidence = snippet if not evidence else evidence
                break

        # 3) 单点时间（若还没有 start）
        if start is None:
            for label, text in sources:
                found = _first_datetime(text, now)
                if found:
                    start = found[0]
                    evidence = found[1] if not evidence else evidence
                    break

        # 4) 只有 deadline 时，把 deadline 作为 start 的兜底参考
        if start is None and deadline is not None:
            start = deadline

        if end is not None and (deadline is None or deadline < end):
            deadline = end
        bucket = _bucket_for(start, deadline, now, end=end)
        if bucket not in BUCKETS:
            bucket = "undated"

        return ParsedTime(
            start=start,
            end=end,
            deadline=deadline,
            bucket=bucket,
            evidence=evidence or raw_evidence,
        )
    except Exception:  # noqa: BLE001 —— 解析失败绝不丢弃条目
        fallback = (ai_time_evidence or ai_event_time or "").strip()
        return ParsedTime(start=None, end=None, deadline=None, bucket="undated", evidence=fallback)


def bucket_label(bucket: str) -> str:
    return {
        "today": "今天",
        "tomorrow": "明天",
        "this_week": "本周",
        "next_week": "下周",
        "undated": "待定",
        "past": "已过期",
    }.get(bucket, "待定")
