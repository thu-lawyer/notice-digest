"""详情补全：给需要的条目抓详情并落地本地时间解析结果。

只抓「值得抓」的条目（intent_group=activity 或关键分类），
``detail_status == "complete"`` 的不重复请求；
请求之间强制间隔 ≥ MIN_INTERVAL（默认 1 秒）并带重试，避免给源站压力。
"""

from __future__ import annotations

import re

import time
from datetime import datetime

from . import fetch
from .config import Config
from .store import Store, needs_enrich
from .timeparse import (
    TZ_SHANGHAI,
    ParsedTime,
    bucket_for,
    is_placeholder_time,
    parse_item_time,
    plausible_station_deadline,
    time_status,
)

#: 请求最小间隔（秒）——对源站的礼貌限速，不得下调
MIN_INTERVAL = 1.0

#: 单次 enrich 最多抓多少条（防止一次跑太久）
DEFAULT_BATCH = 60


def detail_time_text(detail: dict) -> str:
    """站点 detail 里可用的时间自由文本（按可用性排序）。"""
    if not isinstance(detail, dict):
        return ""
    for key in ("ai_event_time", "time_text", "event_time_text"):
        value = detail.get(key)
        if value:
            return str(value)
    return ""


def _is_not_found(exc: BaseException) -> bool:
    """FetchError 是否由 404 引起。

    列表 API 会返回详情已 404 的条目（实测 weixinzs_467874276:12240933），
    这属于上游已下架的正常噪音，必须单独计数，不能计入「结构性失败」。
    """
    text = str(exc)
    return "404" in text or "not found" in text.lower()


def _is_rate_limited(exc: BaseException) -> bool:
    """异常是否由 429 限流引起（t27：与结构性失败分开计数）。"""
    cls = getattr(fetch, "RateLimited", None)
    return bool(cls is not None and isinstance(exc, cls))


def _rate_limit_cooldown(seconds: float) -> float:
    """429 的 ``Retry-After`` 有界退避（单次 ≤ ``fetch.RATE_LIMIT_BACKOFF_CAP``）。

    实测 pkuknow 的 429 响应**不带** ``Retry-After``（见 docs/RUNBOOK.md §17），
    因此生产路径不退避、立即收批；一旦站点补上该头，这里最多等 30 秒（有界），
    批处理总时长仍然有界。
    """
    cap = float(getattr(fetch, "RATE_LIMIT_BACKOFF_CAP", 30.0))
    delay = max(0.0, min(float(seconds or 0.0), cap))
    if delay > 0:
        time.sleep(delay)
    return delay


def enrich_one(
    store: Store,
    cfg: Config,
    item: dict,
    now: datetime,
    *,
    min_interval: float = MIN_INTERVAL,
    timeout: int = 20,
    retries: int = 3,
    error_sink: dict | None = None,
) -> dict | None:
    """抓单条详情、写库，返回详情 dict；失败返回 None。

    单条失败只记录、不抛出（t7-R5）：一条 404 不得中断整批。
    ``error_sink`` 由调用方传入，用于把失败分成 rate_limited / not_found / 其它失败，
    并写入 ``error_samples``（条目 id + 异常类型 + 异常消息，最多 5 条）——
    429 限流的现场证据就在那里（docs/RUNBOOK.md §17）。
    """
    item_id = str(item.get("id") or "")
    if not item_id:
        return None
    try:
        detail = fetch.fetch_detail(cfg.campus, item_id, timeout=timeout, retries=retries)
    except fetch.FetchError as exc:
        store.touch_enrich_attempt(item_id)
        if error_sink is not None:
            error_sink["failed"] = int(error_sink.get("failed") or 0) + 1
            if _is_rate_limited(exc):
                error_sink["rate_limited"] = int(error_sink.get("rate_limited") or 0) + 1
                error_sink["retry_after"] = float(getattr(exc, "retry_after", 0.0) or 0.0)
            elif _is_not_found(exc):
                error_sink["not_found"] = int(error_sink.get("not_found") or 0) + 1
            # 失败样本（t27-F1）：每一条 FetchError 都要留下可被调用方看见的证据
            # （条目 id + 异常类型 + 异常消息）。旧实现只在这里加计数、不写样本，
            # 于是 60 条详情全灭时对外仍是 {"failed": 60, "errors": 0,
            # "error_samples": []} —— 现场根本看不到 429。
            samples = error_sink.setdefault("error_samples", [])
            if len(samples) < 5:
                samples.append(f"{item_id}｜{type(exc).__name__}: {exc}")
        return None

    store.update_detail(item_id, detail)

    # 顺手把可解析的时间落一份（store 只存原始 detail，解析留给上层复用）
    merged = dict(item)
    merged["detail"] = detail
    return merged


def enrich_pending(
    store: Store,
    cfg: Config,
    now: datetime,
    limit: int = DEFAULT_BATCH,
    *,
    min_interval: float = MIN_INTERVAL,
) -> dict:
    """按批次补详情。返回统计（含时间结构化命中率，供 cli 判断异常）。

    命名红线（t7-R1）：本函数内**不得**用 ``structured_time`` / ``text_only``
    当局部变量名 —— 它们与同名模块函数冲突，会让 ``structured_time(detail, now)``
    在**第一轮**就变成 ``TypeError: 'int' object is not callable``，
    所有真实 enrich 全崩、日报根本产不出来。计数一律用 *_cnt 后缀。
    """
    pending = store.pending_enrich(limit)
    fetched = failed = skipped = 0
    not_found = errors = 0
    rate_limited = 0
    structured_cnt = text_cnt = none_cnt = 0
    error_samples: list[str] = []
    last_request_at = 0.0
    item_started = 0.0
    attempted = 0
    stopped_reason = "done"
    stopped_at: str | None = None
    retry_after = 0.0
    cooldown_seconds = 0.0
    per_item_seconds: list[float] = []
    batch_started = time.monotonic()

    for item in pending:
        if not needs_enrich(item):
            skipped += 1
            continue
        elapsed = time.monotonic() - last_request_at
        if last_request_at and elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        last_request_at = time.monotonic()
        item_started = time.monotonic()
        attempted += 1

        per = {"failed": 0, "not_found": 0, "rate_limited": 0, "retry_after": 0.0,
               "error_samples": []}
        try:
            result = enrich_one(
                store,
                cfg,
                item,
                now,
                min_interval=min_interval,
                timeout=cfg.timeout,
                retries=cfg.retries,
                error_sink=per,
            )
        except Exception as exc:  # noqa: BLE001 —— 单条异常不得中断整批（t7-R5）
            per["failed"] = 1
            errors += 1
            if len(error_samples) < 5:
                error_samples.append(f"{item.get('id') or '?'}｜{type(exc).__name__}: {exc}")
            result = None

        failed += int(per.get("failed") or 0)
        not_found += int(per.get("not_found") or 0)
        rate_limited += int(per.get("rate_limited") or 0)
        per_item_seconds.append(round(time.monotonic() - item_started, 3))
        for sample in per.get("error_samples") or []:
            if len(error_samples) < 5:
                error_samples.append(sample)
        if per.get("rate_limited"):
            # —— 优雅降级（t27-F4）——
            # 首次 429 立即收批：不再为剩余条目各烧一次约 4.8 秒的退避（旧行为下
            # 60 条 = 60 × 4.81 s ≈ 289 s，日报直接被拖出投递窗口）。已补全的条目
            # 照常入库；未补全的**原样留在 pending**（只 +1 enrich_attempts，
            # 不写失败态、不删行），下一轮自然重试。
            stopped_reason = "rate-limited"
            stopped_at = str(item.get("id") or "") or None
            retry_after = float(per.get("retry_after") or 0.0)
            cooldown_seconds = _rate_limit_cooldown(retry_after)
            break
        if result is None:
            continue
        fetched += 1
        detail = result.get("detail") or {}
        st = structured_time(detail, now)
        if st is not None and st.get("start") is not None:
            structured_cnt += 1
        else:
            parsed = parsed_for_item(result, now)
            if getattr(parsed, "start", None) is None:
                none_cnt += 1
                result["time_status"] = "none"
            else:
                text_cnt += 1
                result["time_status"] = time_status(parsed)

    return {
        "candidates": len(pending),
        "fetched": fetched,
        "failed": failed,
        "skipped": skipped,
        "not_found": not_found,
        "rate_limited": rate_limited,
        "errors": errors,
        "error_samples": error_samples,
        "structured_time": structured_cnt,
        "text_only": text_cnt,
        "no_time": none_cnt,
        # —— 收批与耗时台账（t27-F2/F4）——
        "stopped_reason": stopped_reason,
        "stopped_at": stopped_at,
        "retry_after": retry_after,
        "cooldown_seconds": round(cooldown_seconds, 3),
        "attempted": attempted,
        "remaining_pending": max(0, len(pending) - attempted),
        "elapsed_seconds": round(time.monotonic() - batch_started, 3),
        "per_item_seconds": per_item_seconds,
    }



def parsed_for_item(item: dict, now: datetime):
    """详情里的结构化时间优先，自由文本解析只作兜底（t7-F6①）。

    实测（14 条 activity 详情）：event_start 10/14、event_end 2/14、
    ai_event_time 12/14，且存在 ai_event_time 为空但 event_start 有值的情形
    —— 站点已给的 ISO 带时区，比从自由文本猜更准，必须先采用。
    站点 deadline 不可信：只在 start 之后 180 天内才当真截止，否则仅当弱信号。
    """
    detail = item.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    structured = structured_time(detail, now)
    if structured is not None and structured.get("start") is not None:
        return _parsed_from_structured(structured, detail, now)
    body = detail.get("content") or detail.get("content_markdown") or ""
    parsed = parse_item_time(
        title=str(item.get("title") or ""),
        ai_event_time=detail.get("ai_event_time"),
        ai_time_evidence=detail.get("ai_time_evidence"),
        body=body,
        now=now,
    )
    weak = structured_time(detail, now, start_only_deadline=True)
    if weak and weak.get("deadline") is not None:
        item["station_deadline_weak"] = True
    return parsed


def structured_time(detail: dict, now: datetime, start_only_deadline: bool = False) -> dict | None:
    """读取站点结构化字段（ISO/时间戳皆可）。无任何字段时返回 None。"""
    if not isinstance(detail, dict):
        return None
    start = _coerce_dt(detail.get("event_start") or detail.get("start_time"), now)
    end = _coerce_dt(detail.get("event_end") or detail.get("end_time"), now)
    deadline = _coerce_dt(detail.get("deadline") or detail.get("signup_deadline"), now)
    if start is None and end is None and deadline is None:
        return None
    out = {"start": start, "end": end, "deadline": None, "weak_deadline": deadline}
    if deadline is not None and not start_only_deadline:
        if plausible_station_deadline(start, deadline, now):
            out["deadline"] = deadline
        elif start is None and deadline is not None and plausible_station_deadline(now, deadline, now):
            out["deadline"] = deadline
    return out


def _coerce_dt(value, now: datetime):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ_SHANGHAI)
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ_SHANGHAI)
    except ValueError:
        pass
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        try:
            return datetime(y, mo, d, hh, mi, tzinfo=TZ_SHANGHAI)
        except ValueError:
            return None
    if re.match(r"^\d{10}$", text) or re.match(r"^\d{13}$", text):
        ts = int(text)
        if ts > 10**12:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=TZ_SHANGHAI)
    return None


def _parsed_from_structured(st: dict, detail: dict, now: datetime):
    start = st.get("start")
    end = st.get("end")
    deadline = st.get("deadline")
    if end is not None and (deadline is None or deadline < end):
        deadline = end
    bucket = bucket_for(start=start, end=end, deadline=deadline, now=now)
    evidence = "结构化字段：detail.event_start=" + str(detail.get("event_start"))
    if end is not None:
        evidence += " detail.event_end=" + str(detail.get("event_end"))
    return ParsedTime(
        start=start,
        end=end,
        deadline=deadline,
        bucket=bucket,
        evidence=evidence,
    )


def time_status_for_item(item: dict, now: datetime, parsed=None) -> str:
    """ok | unconfirmed | none（供邮件把「时间未确认」「无时间」单列）。"""
    parsed = parsed if parsed is not None else parsed_for_item(item, now)
    return time_status(parsed)



def location_for_item(item: dict) -> str | None:
    """地点抽取：结构化字段优先，其次本地规则（站点 event_location 实测 0/14）。"""
    detail = item.get("detail") or {}
    if isinstance(detail, dict):
        for key in ("event_location", "location", "ai_event_location"):
            value = detail.get(key)
            if value:
                return str(value)
    else:
        detail = {}
    body = detail.get("content") or detail.get("content_markdown") or ""
    text = "\n".join(
        str(x) for x in (item.get("title") or "", item.get("summary") or "", body) if x
    )
    return extract_location(text)


LOCATION_LABELS = ("地点", "位置", "举办地点", "活动地点", "会议地点", "地点为", "地点：")
BUILDING_RE = re.compile(
    r"([一二三四五六七八九十\d]{1,2}教[ ]?[A-Za-z0-9\-]{0,8}|[A-Za-z]座[0-9A-Za-z\-]{0,8}"
    r"|[\u4e00-\u9fa5]{2,8}(?:楼|馆|厅|堂|报告厅|会议室|教室)[0-9A-Za-z\-]{0,8})"
)


def extract_location(text: str) -> str | None:
    """从正文/标题抽地点。命中「地点：X」优先，其次楼宇+房间号样式。"""
    if not text:
        return None
    flat = re.sub(r"\s+", " ", str(text))
    for label in LOCATION_LABELS:
        idx = flat.find(label)
        while idx >= 0:
            seg = flat[idx + len(label): idx + len(label) + 48].lstrip("：: 　")
            seg = re.split(r"[，。；;、\n|｜]", seg)[0].strip()
            if 2 <= len(seg) <= 40:
                return seg
            idx = flat.find(label, idx + len(label))
    m = BUILDING_RE.search(flat)
    if m:
        seg = m.group(0).strip()
        if 2 <= len(seg) <= 40:
            return seg
    return None



def time_evidence_for_item(item: dict) -> str:
    from .store import Store as _Store  # noqa: F401  仅保持导入关系清晰

    detail = item.get("detail") or {}
    if not isinstance(detail, dict):
        return ""
    return str(detail.get("ai_time_evidence") or "")
