"""命令行入口。

子命令：fetch / enrich / report / render / send / learn-selftest / stats / explain

``render`` 与 ``send`` 只做**惰性转发**到渲染层（deliver-dev 提供），
本模块不实现它们 —— 缺失时给出清晰报错而不是伪装成功。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from . import fetch as fetch_mod
from . import score as score_mod
from .config import Config, load_config
from .enrich import enrich_pending, parsed_for_item
from . import feedback as feedback_mod
from .store import Store, now_shanghai
from .timeparse import bucket_label

#: 默认扫描页数（观测：每日新增 150–180 条，站点窗口约 7 天）
DEFAULT_PAGES = 30

#: 早停阈值：某页**全部**条目都已入库即停
EARLY_STOP_KNOWN_RATIO = 1.0


def _open(cfg: Config, db: str | None) -> Store:
    store = Store(Path(db) if db else cfg.db_path)
    store.init_schema()
    return store


def _parse_times(items: list[dict], now: datetime) -> dict:
    return {str(it.get("id")): parsed_for_item(it, now) for it in items}


# --------------------------------------------------------------------- fetch
def cmd_fetch(cfg: Config, args) -> int:
    """抓取列表页（分页 + 按 id 去重增量）。

    t7 三条停止分支（缺一不可）：
      ① items 为 null / 空          → stop_reason = page-N-empty（且第 1 页为空按异常处理）
      ② 整页都已入库（零新增）      → stop_reason = page-N-all-known
      ③ 达到 max_pages（默认 30，刻意放大）→ stop_reason = reached-max-pages
    另有第 ④ 条兜底：站点对越界页会「钳制并重复返回同一页」，靠页签名去重识别，
    stop_reason = page-N-repeated，避免在越界页上空转到 max_pages。
    深度 = 配置的 max_pages + 零新增停止，不写死任何「每日增量」数字。
    """
    store = _open(cfg, args.db)
    campus = args.campus or cfg.campus
    pages = int(args.pages)
    now = now_shanghai()
    inserted = known = 0
    skipped_total = 0
    scanned_pages = 0
    stop_reason = "reached-max-pages"
    per_page = []
    seen_signatures = set()
    prev_signature = None
    repeat = False
    anomalies = []
    try:
        for page in range(1, pages + 1):
            data = fetch_mod.fetch_list(campus, page, timeout=cfg.timeout, retries=cfg.retries)
            scanned_pages = page
            if not isinstance(data, dict):
                anomalies.append({
                    "kind": "structure-drift",
                    "severity": "error",
                    "detail": f"page {page} 返回 {type(data).__name__}，期望 dict",
                })
                stop_reason = f"page-{page}-bad-payload"
                break
            raw_items = data.get("items")
            if raw_items is None or raw_items == []:
                stop_reason = f"page-{page}-empty"
                if page == 1:
                    anomalies.append({
                        "kind": "page-1-empty",
                        "severity": "error",
                        "detail": "第 1 页 items 为空 —— 上游可能改结构或不可用，不能当成静默成功",
                    })
                break
            if not isinstance(raw_items, list) or not all(isinstance(x, dict) for x in raw_items):
                anomalies.append({
                    "kind": "structure-drift",
                    "severity": "error",
                    "detail": f"page {page} items 类型异常：{type(raw_items).__name__}",
                })
                stop_reason = f"page-{page}-bad-payload"
                break
            signature = page_signature(raw_items)
            is_repeat = bool(
                (prev_signature is not None and signature == prev_signature)
                or signature in seen_signatures
            )
            seen_signatures.add(signature)
            prev_signature = signature
            # D-3：缺 ``id`` 的列表项过去被静默丢弃 —— 上游列表结构漂移会表现为
            # 「exit 0 + 零新增 + 空日报」，与「今天没有新通知」外部不可区分。
            # 现在逐页计数跳过项，并把「原始 items 非空却无一项可用」判为致命结构漂移。
            skip_sink = {"not_dict": 0, "missing_id": 0}
            new_count, known_count = store.upsert_items(raw_items, skip_sink=skip_sink)
            inserted += new_count
            known += known_count
            page_skipped = skip_sink["not_dict"] + skip_sink["missing_id"]
            if page_skipped:
                skipped_total += page_skipped
                detail = (
                    f"page {page} 有 {page_skipped}/{len(raw_items)} 条列表项缺必需键 id"
                    f"（not_dict={skip_sink['not_dict']}, missing_id={skip_sink['missing_id']}），已跳过未入库"
                )
                if len(raw_items) - page_skipped == 0:
                    anomalies.append({
                        "kind": "schema-drift",
                        "severity": "error",
                        "detail": detail + "；该页原始 items 非空却无一项可用，判为上游结构漂移",
                    })
                    per_page.append({
                        "page": page, "items": len(raw_items), "new": new_count,
                        "known": known_count, "skipped": page_skipped,
                    })
                    stop_reason = f"page-{page}-schema-drift"
                    break
                anomalies.append({
                    "kind": "item-missing-id",
                    "severity": "warn",
                    "detail": detail,
                })
            per_page.append({
                "page": page, "items": len(raw_items), "new": new_count,
                "known": known_count, "skipped": page_skipped,
            })
            # 停止分支 ②：整页全已知（零新增）—— 幂等重跑的**正常**收敛点。
            # 必须排在「重复页」判定之前：越界钳制页的 id 必然已全部入库，若先判重复，
            # 收敛原因会被误报成 page-N-repeated，调用方就无法据 stop_reason 判断是「跑完了」。
            if known_count >= len(raw_items) * EARLY_STOP_KNOWN_RATIO:
                if is_repeat:
                    anomalies.append({
                        "kind": "page-repeat",
                        "severity": "warn",
                        "detail": f"page {page} 与已扫描页返回同一批 id（越界钳制），且整页已全知，按 all-known 收敛",
                    })
                stop_reason = f"page-{page}-all-known"
                break
            # 停止分支 ③：id 集合重复但仍有未知项 —— 上游把越界页夹回，继续翻页只会空转。
            # 这是站点的**正常**行为（实测 page 44-47 反复返回同一批 9 条），故记 warn：
            # 记 error 会让每次翻到窗口底部的正常运行都发一封失败邮件（误报）。
            if is_repeat:
                repeat = True
                stop_reason = f"page-{page}-repeated"
                anomalies.append({
                    "kind": "page-repeat",
                    "severity": "warn",
                    "detail": f"page {page} 的 id 集合此前出现过（越界钳制），已停止翻页",
                })
                break
    except fetch_mod.FetchError as exc:
        # 退出码分级（t7 裁决二）：网络不可达 / 5xx 重试耗尽 / 响应非 JSON
        # 属致命故障 → exit 3 + 失败邮件，systemd 才会记失败并告警。
        # 「可疑但已处理」（零新增、整页全已知、末页重复、单条详情 404、
        # 个别 enrich 失败）一律 exit 0 只记 warn —— 把「今天没新通知」
        # 判成失败，等于每周造几次假警报，真故障会被淹掉。
        report_failure(cfg, "fetch", f"{type(exc).__name__}: {exc}", store=store)
        print(f"[fetch] 失败（致命，exit 3）：{exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 —— 无人值守任务不允许静默死亡
        report_failure(cfg, "fetch", f"{type(exc).__name__}: {exc}", store=store)
        print(f"[fetch] 未预期异常（致命，exit 3）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    if inserted == 0 and scanned_pages > 0:
        anomalies.append({
            "kind": "zero-new",
            "severity": "warn",
            "detail": f"扫描 {scanned_pages} 页，零新增（stop_reason={stop_reason}）",
        })
    if stop_reason == "reached-max-pages":
        anomalies.append({
            "kind": "max-pages-hit",
            "severity": "warn",
            "detail": f"翻到 max_pages={pages} 仍未出现空页/全已读页；站点窗口可能比预期更长",
        })

    result = {
        "campus": campus,
        "pages_scanned": scanned_pages,
        "new": inserted,
        "known": known,
        "skipped_items": skipped_total,
        "stop_reason": stop_reason,
        "repeat_detected": repeat,
        "per_page": per_page,
        "anomalies": anomalies,
        "db": str(store.db_path),
        "scanned_at": now.isoformat(),
    }
    errors = [a for a in anomalies if a["severity"] == "error"]
    warnings = [a for a in anomalies if a["severity"] == "warn"]
    if warnings:
        for a in warnings:
            print(f"[fetch][warn] {a['kind']}: {a['detail']}", file=sys.stderr)
    if errors:
        result["failure_mail"] = report_failure(
            cfg, "fetch", "; ".join(f"{a['kind']}: {a['detail']}" for a in errors), store=store
        )
    log_event(store, "fetch", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    store.close()
    return 3 if errors else 0


# -------------------------------------------------------------------- enrich
def cmd_enrich(cfg: Config, args) -> int:
    """详情补全；同样把「静默失败」变成显式失败（t7-F2）。"""
    store = _open(cfg, args.db)
    now = now_shanghai()
    try:
        stats = enrich_pending(store, cfg, now, limit=int(args.limit))
    except Exception as exc:  # noqa: BLE001
        report_failure(cfg, "enrich", f"{type(exc).__name__}: {exc}", store=store)
        print(f"[enrich] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        store.close()
        return 3
    stats = dict(stats)
    anomalies = []
    fetched = int(stats.get("fetched") or 0)
    failed = int(stats.get("failed") or 0)
    parsed_ok = int(stats.get("structured_time") or 0) + int(stats.get("text_only") or 0)
    not_found = int(stats.get("not_found") or 0)
    item_errors = int(stats.get("errors") or 0)
    rate_limited = int(stats.get("rate_limited") or 0)
    # 单条详情 404 属上游正常噪音（列表里仍挂着已下架的条目），只记 warn、
    # 不触发失败邮件；该条已 touch_enrich_attempt，下一轮会自然重试（t7-R5 / 裁决二）。
    if not_found:
        anomalies.append({
            "kind": "detail-404",
            "severity": "warn",
            "detail": f"{not_found} 条详情 404（列表挂着但详情已下架），已计数并留待重试，不影响其它条目",
        })
    if item_errors:
        anomalies.append({
            "kind": "enrich-item-errors",
            "severity": "warn",
            "detail": f"{item_errors} 条补全时抛非 FetchError 异常（未中断整批）；样例：{stats.get('error_samples')}",
        })
    if fetched > 0 and parsed_ok == 0 and not rate_limited:
        # rate_limited 时不判「解析 0 命中」：被配额截断的批次里，解析命中率是
        # **有偏样本**（可能只补全到 1 条且恰好没有时间文本），拿它当结构性信号
        # 会误报致命（t27-F3）。真正的解析失效仍会在不掺限流的批次里被抓到。
        anomalies.append({
            "kind": "parse-failure-spike",
            "severity": "error",
            "detail": f"补全 {fetched} 条，但时间解析 0 命中 —— 详情结构或解析规则可能已失效",
        })
    if rate_limited:
        # 429 是站点配额，不是结构性缺陷：单列一级计数 + warn，绝不发失败邮件（t27-F3）。
        anomalies.append({
            "kind": "detail-rate-limited",
            "severity": "warn",
            "detail": (
                f"{rate_limited} 条详情被站点限流（HTTP 429）；本批首次命中即收批"
                f"（stopped_at={stats.get('stopped_at')}，remaining="
                f"{stats.get('remaining_pending')}），已补全的照常入库，"
                f"未补全的原样留在 pending 留待下一轮；"
                f"Retry-After={stats.get('retry_after')}"
            ),
        })
    # 判据收窄（t27-F3）：只有「一条都没补全成功」且失败**不能被限流/404 解释**时，
    # 才算结构性致命失败。原判据 `fetched > 0 and failed >= fetched` 会在
    # 「先成功 60 条、随后被限流」时误报致命（60 >= 60），把一份有效日报打成失败邮件。
    structural_failed = failed - rate_limited - not_found
    if fetched == 0 and structural_failed > 0:
        anomalies.append({
            "kind": "detail-fetch-all-failed",
            "severity": "error",
            "detail": (
                f"详情请求 {fetched} 条全部失败"
                f"（非限流、非 404 的结构性失败 {structural_failed} 条）"
            ),
        })
    if fetched == 0 and int(stats.get("candidates") or 0) == 0 and int(stats.get("skipped") or 0) == 0:
        anomalies.append({"kind": "nothing-to-enrich", "severity": "warn", "detail": "没有待补全条目"})
    stats["anomalies"] = anomalies
    errors = [a for a in anomalies if a["severity"] == "error"]
    for a in anomalies:
        print(f"[enrich][{a['severity']}] {a['kind']}: {a['detail']}", file=sys.stderr)
    if errors:
        stats["failure_mail"] = report_failure(
            cfg, "enrich", "; ".join(f"{a['kind']}: {a['detail']}" for a in errors), store=store
        )
    log_event(store, "enrich", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    store.close()
    return 3 if errors else 0


# -------------------------------------------------------------------- report
def _build_report(cfg: Config, store: Store, now: datetime, top_n: int) -> tuple[list, dict]:
    items = store.all_items(limit=int(args_limit_default()))
    parsed_map = _parse_times(items, now)
    scored = score_mod.score_items(items, store.get_weights(), cfg, now, parsed_map)
    return scored[:top_n], parsed_map


def args_limit_default() -> int:
    return 2000


def cmd_report(cfg: Config, args) -> int:
    store = _open(cfg, args.db)
    now = now_shanghai()
    top_n = cfg.clamp_top_n(args.top)
    scored, parsed_map = _build_report(cfg, store, now, top_n)
    buckets: dict[str, int] = {}
    for s in scored:
        b = s.parsed.bucket if s.parsed else "undated"
        buckets[b] = buckets.get(b, 0) + 1
    print(f"# notice-digest 日报（{now.date().isoformat()}）候选 {len(scored)} 条\n")
    for idx, s in enumerate(scored, 1):
        parsed = parsed_map.get(s.item_id)
        when = parsed.start.strftime("%m-%d %H:%M") if parsed and parsed.start else "时间待定"
        print(f"{idx:2d}. [{s.score:+.2f}] {s.title}")
        print(f"    分类={s.item['category']}  来源={s.item['source_name']}  时间={when}")
        print(f"    时间证据：{(parsed.evidence if parsed else '') or '（无）'}")
        print(f"    打分理由：{'; '.join(s.reason_strings) or '（无命中特征）'}")
    print("\n分桶统计：" + ", ".join(f"{bucket_label(k)}={v}" for k, v in sorted(buckets.items())))
    store.close()
    return 0


# --------------------------------------------------------------------- stats
def cmd_stats(cfg: Config, args) -> int:
    store = _open(cfg, args.db)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    store.close()
    return 0


# ------------------------------------------------------------------- explain
def cmd_explain(cfg: Config, args) -> int:
    store = _open(cfg, args.db)
    now = now_shanghai()
    if args.id:
        item = store.get_item(args.id)
        if item is None:
            print(f"未找到条目 {args.id}", file=sys.stderr)
            store.close()
            return 1
        items = [item]
    else:
        items = store.all_items(limit=args_limit_default())
    parsed_map = _parse_times(items, now)
    scored = score_mod.score_items(items, store.get_weights(), cfg, now, parsed_map)
    for s in scored[: int(args.top)]:
        print(score_mod.explain(s))
        print()
    store.close()
    return 0


# ------------------------------------------------------- render / send 转发
def cmd_render(cfg: Config, args) -> int:
    """惰性转发到渲染层 notice_digest.render（由 deliver-dev 实现）。"""
    try:
        from . import render as render_mod
    except ImportError as exc:
        print(
            "[render] 渲染层未就绪：缺少 notice_digest/render.py（由渲染层负责实现）。\n"
            f"        底层错误：{exc}",
            file=sys.stderr,
        )
        return 3

    store = _open(cfg, args.db)
    now = now_shanghai()
    capped = cfg.clamp_top_n(args.top)
    items = store.all_items(limit=args_limit_default())
    parsed_map = _parse_times(items, now)
    scored = score_mod.score_items(items, store.get_weights(), cfg, now, parsed_map)[:capped]
    subject, html, ics_text = render_mod.render_email(scored, parsed_map, cfg, now)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        if args.ics_out:
            Path(args.ics_out).write_text(ics_text, encoding="utf-8")
        print(json.dumps({"subject": subject, "html_path": str(out)}, ensure_ascii=False))
    else:
        print(subject)
        print(html)
    store.close()
    return 0


def cmd_send(cfg: Config, args) -> int:
    """惰性转发到投递层 notice_digest.mailer（由 deliver-dev 实现）。

    投递窗口（Phase B）：只发「上次成功投递之后」新出现的条目 —— 没有成功记录时视为首次
    投递、窗口不限。「新增 N 条」始终是**本窗口**的条数，历史条目不会被重复计数。

    幂等闸门在发信**之前**：``store.begin_send()`` 先把 attempt 行落盘、抢占本轮发送权，
    发信成功后 ``store.mark_sent()`` 升级为 sent。同一天同内容再跑一次会命中闸门、跳过
    SMTP，且返回 0（不是失败：systemd timer 重试/手动补跑都应当无害）。
    """
    try:
        from . import mailer as mailer_mod
    except ImportError as exc:
        print(
            "[send] 投递层未就绪：缺少 notice_digest/mailer.py（由渲染层负责实现）。\n"
            f"       底层错误：{exc}",
            file=sys.stderr,
        )
        return 3

    try:
        from . import render as render_mod
    except ImportError as exc:
        print(f"[send] 渲染层未就绪：{exc}", file=sys.stderr)
        return 3

    store = _open(cfg, args.db)
    now = now_shanghai()
    capped = cfg.clamp_top_n(args.top)
    window_since = store.last_sent_at()
    items = store.items_published_after(window_since, limit=args_limit_default())
    parsed_map = _parse_times(items, now)
    scored = score_mod.score_items(items, store.get_weights(), cfg, now, parsed_map)[:capped]
    subject, html, ics_text = render_mod.render_email(scored, parsed_map, cfg, now)

    summary = {
        "sent": False,
        "subject": subject,
        "n_items": len(scored),
        "window_since": window_since,
        "gated": False,
    }

    if args.dry_run:
        # 干跑只写文件、不落台账也不闸门（否则一次预演就会把当天真的投递拦掉）
        ok = mailer_mod.send(subject, html, ics_text, cfg, to_addr=args.to or None, dry_run=True)
        summary["sent"] = bool(ok)
        print(json.dumps(summary, ensure_ascii=False))
        store.close()
        return 0 if ok else 4

    if not subject.strip():
        # 窗口里没有新条目：交给 mailer 的空主题哨兵（不发信、不记台账、不算失败）
        mailer_mod.send(subject, html, ics_text, cfg, to_addr=args.to or None)
        print(json.dumps(summary, ensure_ascii=False))
        store.close()
        return 0

    day = now.date().isoformat()
    fingerprint = mailer_mod.send_fingerprint(subject, len(scored), html)
    summary["fingerprint"] = fingerprint
    if not store.begin_send(day, subject, fingerprint):
        print(
            f"[send] 幂等闸门命中：{day} 已投递过同一份内容（指纹 {fingerprint}），"
            "本次不发信、不重复记台账。",
            file=sys.stderr,
        )
        summary["gated"] = True
        print(json.dumps(summary, ensure_ascii=False))
        store.close()
        return 0

    ok = mailer_mod.send(subject, html, ics_text, cfg, to_addr=args.to or None)
    summary["sent"] = bool(ok)
    if ok:
        store.mark_sent(day, len(scored), fingerprint)
    print(json.dumps(summary, ensure_ascii=False))
    store.close()
    return 0 if ok else 4


# ------------------------------------------------------------ learn-selftest
def cmd_learn_selftest(cfg: Config, args) -> int:
    """在线学习自检：幂等性、上下界、衰减、学习方向。全过返回 0。"""
    store = _open(cfg, args.db)
    now = now_shanghai()
    failures: list[str] = []
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    items = store.all_items(limit=50)
    if not items:
        data = fetch_mod.fetch_list(cfg.campus, 1, timeout=cfg.timeout, retries=cfg.retries)
        raw = data.get("items") or []
        store.upsert_items(raw)
        items = store.all_items(limit=50)
    check("样本可用", bool(items), f"{len(items)} 条")

    sample = items[0] if items else None
    if sample is None:
        print(json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False, indent=2))
        store.close()
        return 1

    sid = str(sample["id"])
    # 每次运行用唯一 kind 后缀：selftest 必须**可重复运行** —— 若沿用固定 kind，
    # 上一轮写入的反馈行会让本轮的「幂等首记 / 反馈→学习」断言失败（冷却期拒绝重复），
    # 那是重复运行的自我干扰，不是引擎缺陷。
    run_tag = f"{int(now.timestamp())}-{uuid.uuid4().hex[:6]}"
    up_kind = f"selftest_up:{run_tag}"
    click_kind = f"selftest_click:{run_tag}"
    # 1) 幂等：同一 (item_id, kind) 重复反馈只计一次
    first = store.record_feedback(sid, up_kind, 1.0, now.isoformat())
    second = store.record_feedback(sid, up_kind, 1.0, now.isoformat())
    check("反馈幂等（首次记为 True）", first is True)
    check("反馈幂等（重复记为 False）", second is False)
    check(
        "反馈表无重复行",
        len([f for f in store.recent_feedback(500) if f["item_id"] == sid and f["kind"] == up_kind]) == 1,
    )

    # 2) 在线学习方向：正反馈应抬高命中特征权重
    feats = score_mod.features_of(sample, cfg, now, parsed_for_item(sample, now))
    base_w = dict(score_mod.merged_weights(cfg, {}))
    up_w = score_mod.learn(base_w, feats, 1.0)
    down_w = score_mod.learn(base_w, feats, 0.0)
    hit = [k for k in feats if k in base_w]
    if hit:
        key = hit[0]
        check("正反馈抬高权重", up_w.get(key, 0) > base_w.get(key, 0), f"{key}: {base_w.get(key):.4f} -> {up_w.get(key):.4f}")
        check("负反馈压低权重", down_w.get(key, 0) < base_w.get(key, 0), f"{key}: {base_w.get(key):.4f} -> {down_w.get(key):.4f}")
    else:
        check("正反馈抬高权重", True, "该样本无先验命中特征，跳过方向断言")
        check("负反馈压低权重", True, "跳过")

    # 3) 硬上下界：极端奖励反复冲击后仍在界内
    blown = dict(base_w)
    for _ in range(500):
        blown = score_mod.learn(blown, feats, 1.0, lr=1.0)
    for _ in range(500):
        blown = score_mod.learn(blown, feats, 0.0, lr=1.0)
    check(
        "权重不越界",
        all(score_mod.W_MIN <= v <= score_mod.W_MAX for v in blown.values()),
        f"min={min(blown.values()):.3f} max={max(blown.values()):.3f}",
    )

    # 4) 衰减：乘因子且仍在界内
    decayed = score_mod.decay(base_w, 0.995)
    check(
        "衰减按因子缩放",
        all(
            abs(decayed[k] - score_mod.clip_weight(base_w[k] * 0.995)) < 1e-9
            for k in base_w
        ),
    )
    check("衰减后仍在界内", all(score_mod.W_MIN <= v <= score_mod.W_MAX for v in decayed.values()))

    # 5) 可解释：理由格式固定为「特征名, 贡献分」
    #    断言必须在整批样本上做：冷启动（权重表为空）时，单条若既不属于
    #    先验分类、也没命中先验关键词，则贡献分全为 0 —— reasons 为空是**正确**行为
    #    （explain 渲染为「（无命中特征）」），不是可解释性缺陷。
    parsed_all = {str(i.get("id")): parsed_for_item(i, now) for i in items}
    scored = score_mod.score_items(items, store.get_weights(), cfg, now, parsed_all)
    reason_strings = [r for sc in scored for r in sc.reason_strings]
    check(
        "打分可解释（特征名, 贡献分）",
        bool(reason_strings) and all(", " in r for r in reason_strings),
        f"{len(scored)} 条打分 / {len(reason_strings)} 条理由；样例：{reason_strings[:2]}",
    )

    # 6) 权重落库往返
    store.put_weights({"selftest:feature": 0.5})
    check("权重落库往返", abs(store.get_weights().get("selftest:feature", 0.0) - 0.5) < 1e-9)

    # 7) 真实反馈写库 + 学习
    res = None
    try:
        from .feedback import record_and_learn

        res = record_and_learn(store, cfg, sid, click_kind, now)
    except Exception as exc:  # noqa: BLE001
        check("反馈→学习链路", False, str(exc))
    else:
        check("反馈→学习链路", res["recorded"] is True, json.dumps(res, ensure_ascii=False))

    print(json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False, indent=2))
    store.close()
    return 1 if failures else 0


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notice_digest.cli", description="notice-digest 引擎 CLI")
    parser.add_argument("--db", default=None, help="SQLite 路径（默认取配置）")
    parser.add_argument("--campus", default=None, help="站点前缀：thu / ruc")
    parser.add_argument("--profile", default=None, help="profile.yaml 路径")
    parser.add_argument("--env-file", default=None, help=".env 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    # 全局开关同时挂在子命令上：`fetch --campus thu --db x.db` 与
    # `--campus thu fetch --db x.db` 都要能用。default=SUPPRESS 保证子解析器
    # 在参数缺省时不覆盖顶层解析器已解析出的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 路径（默认取配置）")
    common.add_argument("--campus", default=argparse.SUPPRESS, help="站点前缀：thu / ruc")
    common.add_argument("--profile", default=argparse.SUPPRESS, help="profile.yaml 路径")
    common.add_argument("--env-file", default=argparse.SUPPRESS, help=".env 路径")

    p = sub.add_parser("fetch", parents=[common], help="抓取列表页（分页 + 按 id 去重增量）")
    p.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("enrich", parents=[common], help="详情补全（限速 ≥1s/请求）")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("report", parents=[common], help="输出文本版日报（不依赖渲染层）")
    p.add_argument("--top", type=int, default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("render", parents=[common], help="转发到渲染层生成 HTML/ICS")
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--out", default=None, help="HTML 输出路径")
    p.add_argument("--ics-out", default=None, help="ICS 输出路径")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("send", parents=[common], help="转发到投递层发信")
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--to", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("learn-selftest", parents=[common], help="在线学习自检")
    p.set_defaults(func=cmd_learn_selftest)

    p = sub.add_parser("stats", parents=[common], help="库统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("explain", parents=[common], help="解释某条（或前 N 条）打分理由")
    p.add_argument("--id", default=None)
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("feedback-serve", parents=[common], help="启动反馈接收服务（HMAC 签名，监听回环 + nginx 反代）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_feedback_serve)
    p = sub.add_parser("units", parents=[common], help="生成 systemd unit/timer 文本（含 OnFailure=）")
    p.add_argument("--workdir", default="/opt/notice-digest")
    p.add_argument("--user", default="notice-digest")
    p.add_argument("--hour", default="07:30")
    p.set_defaults(func=cmd_units)
    p = sub.add_parser("logs", parents=[common], help="查询最近运行事件（含异常分支）")
    p.add_argument("--tail", type=int, default=20)
    p.set_defaults(func=cmd_logs)
    p = sub.add_parser("notify-failure", parents=[common], help="systemd OnFailure= 落点")
    p.add_argument("--unit", default=None)
    p.add_argument("--detail", default=None)
    p.set_defaults(func=cmd_notify_failure)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(
        profile_path=Path(args.profile) if args.profile else None,
        env_path=Path(args.env_file) if args.env_file else None,
    )
    if hasattr(args, "func") and args.func is cmd_feedback_serve:
        return int(args.func(cfg, args))
    resolved = feedback_mod.resolve_public_base(cfg)
    configured = (getattr(cfg, "feedback_base", "") or "").strip()
    if resolved:
        cfg.feedback_base = resolved
    elif configured:
        print(
            "[feedback] 警告：feedback_base 指向本机回环地址，用户点邮件里的链接到不了它 —— "
            "已降级为不生成反馈按钮。请设 ND_FEEDBACK_BASE 为公网地址，"
            "或按 docs/RUNBOOK.md 用 nginx 反向代理到 127.0.0.1:8791。",
            file=sys.stderr,
        )
        cfg.feedback_base = ""
    return int(args.func(cfg, args))


# ------------------------------------------------------------------ t7-F2 可观测性
EVENTS_FILE_SUFFIX = ".events.jsonl"


def page_signature(raw_items) -> tuple:
    """页签名 = 该页 id 序列（用于识别站点对越界页的钳制重复）。"""
    return tuple(str(it.get("id")) for it in raw_items if isinstance(it, dict))


def page_new_ids(raw_items, known_ids) -> list:
    """纯函数：这一页里哪些 id 是新的（便于无网络单测分页逻辑）。"""
    known = set(known_ids or ())
    return [str(it.get("id")) for it in raw_items if isinstance(it, dict) and str(it.get("id")) not in known]


def detect_page_repeat(signatures, signature) -> bool:
    """该页签名是否与上一页相同，或此前已出现过。"""
    sigs = list(signatures or ())
    if sigs and sigs[-1] == signature:
        return True
    return signature in set(sigs)


def events_path(store) -> Path:
    return Path(str(store.db_path) + EVENTS_FILE_SUFFIX)


def log_event(store, kind: str, payload: dict) -> None:
    """把每次运行的判定写进 <db>.events.jsonl，供 `logs` 子命令查询。"""
    try:
        line = json.dumps({"kind": kind, "ts": now_shanghai().isoformat(), "payload": payload}, ensure_ascii=False)
        with open(events_path(store), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[log] 事件写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)


def report_failure(cfg, stage: str, detail: str, store=None) -> str:
    """失败通知：显式 stderr + 事件日志 + 一封失败邮件（t7-F2）。

    邮件失败绝不允许掩盖原始故障，因此这里吞掉自身异常并返回状态字符串。
    未配置 SMTP 时返回 "skipped(no-smtp)"；设 ND_FAILURE_MAIL=0 可整体关闭。
    """
    text = f"[{stage}] {detail}"
    print(f"FAILURE {text}", file=sys.stderr)
    if store is not None:
        log_event(store, "failure", {"stage": stage, "detail": detail})
    if os.environ.get("ND_FAILURE_MAIL", "1") == "0":
        return "disabled"
    if not getattr(cfg, "smtp_host", ""):
        return "skipped(no-smtp)"
    try:
        from . import mailer as mailer_mod
        now = now_shanghai()
        html = (
            "<h2>notice-digest 运行失败</h2>"
            f"<p><b>阶段</b>：{stage}</p>"
            f"<p><b>时间</b>：{now.isoformat()}</p>"
            f"<p><b>错误</b>：{detail}</p>"
            "<p>退出码非 0，systemd 的 OnFailure= 也会触发；请查看 "
            "<code>&lt;db&gt;.events.jsonl</code> 与 journalctl。</p>"
        )
        ok = mailer_mod.send(f"[notice-digest] 运行失败：{stage}", html, "", cfg)
        return "sent" if ok else "send-failed"
    except Exception as exc:  # noqa: BLE001
        print(f"[mail] 失败通知发送失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return f"error:{type(exc).__name__}"


def cmd_feedback_serve(cfg: Config, args) -> int:
    """启动反馈接收服务（t7-F1：此前没有任何可启动入口）。"""
    store = _open(cfg, args.db)
    base = feedback_mod.resolve_public_base(cfg)
    probe = None
    if base:
        ok, probe = feedback_mod.probe_public_base(base)
        if not ok:
            print(f"[feedback] 警告：配置的公网反馈地址不可达：{probe}", file=sys.stderr)
    else:
        print(
            "[feedback] 警告：未配置公网反馈地址（ND_FEEDBACK_BASE）。服务只监听 127.0.0.1，"
            "需要在同一台机器上用 nginx 反代（见 docs/RUNBOOK.md）才能让邮件里的链接生效；"
            "在此之前渲染层不会生成反馈按钮。",
            file=sys.stderr,
        )
    ready = {}
    try:
        srv = feedback_mod.serve(
            cfg, store, host=args.host, port=args.port, ready_callback=lambda h: ready.setdefault("addr", h.server_address)
        )
    except Exception as exc:  # noqa: BLE001
        report_failure(cfg, "feedback-serve", f"{type(exc).__name__}: {exc}", store=store)
        store.close()
        return 3
    info = {
        "listening": f"{args.host}:{ready.get('addr', ('', args.port or ''))[1] if args.port is None else args.port}",
        "public_base": base or "",
        "reachable": probe,
        "health": (base or "http://127.0.0.1") + "/nd/health",
    }
    log_event(store, "feedback-serve", info)
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        store.close()
    return 0


def systemd_units(workdir: str = "/opt/notice-digest", user: str = "notice-digest", hour: str = "07:30") -> str:
    """生成 systemd unit 文本（t7-F2：OnFailure= 让非零退出显式可见）。"""
    return f"""# {workdir}/deploy/notice-digest.service
[Unit]
Description=notice-digest daily digest
After=network-online.target
Wants=network-online.target
OnFailure=notice-digest-failure@%n.service

[Service]
Type=oneshot
User={user}
WorkingDirectory={workdir}
EnvironmentFile={workdir}/.env
ExecStart=/usr/bin/env python3 -m notice_digest.cli fetch --pages 30
ExecStart=/usr/bin/env python3 -m notice_digest.cli enrich --limit 120
ExecStart=/usr/bin/env python3 -m notice_digest.cli send
Restart=no

# --- notice-digest-failure@.service（失败即发信，见 README/docs） ---
# [Unit]
# Description=notify failure of %i
# [Service]
# Type=oneshot
# EnvironmentFile={workdir}/.env
# ExecStart=/usr/bin/env python3 -m notice_digest.cli notify-failure --unit %i

# --- notice-digest-feedback.service（反馈接收服务，常驻） ---
[Unit]
Description=notice-digest feedback server (loopback, nginx reverse-proxied)

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
EnvironmentFile={workdir}/.env
ExecStart=/usr/bin/env python3 -m notice_digest.cli feedback-serve --host 127.0.0.1 --port 8791
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target

# --- notice-digest.timer ---
[Unit]
Description=run notice-digest daily at {hour} (avoid /opt/aidigest 08:00)

[Timer]
OnCalendar=*-*-* {hour}:00
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
"""


def cmd_units(cfg: Config, args) -> int:
    print(systemd_units(workdir=args.workdir, user=args.user, hour=args.hour))
    return 0


def cmd_logs(cfg: Config, args) -> int:
    """查询最近 N 条运行事件（t7-F2：失败必须可查）。"""
    path = Path(args.db or getattr(cfg, "db_path", "data/notice_digest.db"))
    path = Path(str(path) + EVENTS_FILE_SUFFIX)
    if not path.exists():
        print(json.dumps({"events": [], "path": str(path), "note": "尚无事件日志"}, ensure_ascii=False, indent=2))
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-int(args.tail):]
    out = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line})
    errors = [e for e in out if (e.get("payload", {}).get("anomalies") and any(
        a.get("severity") == "error" for a in e["payload"]["anomalies"])) or e.get("kind") == "failure"]
    print(json.dumps({"path": str(path), "tail": len(out), "error_events": len(errors), "events": out},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_notify_failure(cfg: Config, args) -> int:
    """systemd OnFailure= 的落点：把失败写进日志并发一封失败邮件。"""
    detail = args.detail or "systemd 报告单元失败"
    state = report_failure(cfg, args.unit or "systemd", detail)
    print(json.dumps({"unit": args.unit, "failure_mail": state}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


