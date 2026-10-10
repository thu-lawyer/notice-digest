"""反馈接收服务（本机回环）。

三路信号：
    👍/👎  邮件内 ``/nd/f?id=<item_id>&k=up|down&t=<token>``
    点击   邮件内链接走 ``/nd/c?id=<item_id>&t=<token>``，记录后 302 到真实 url
    沉默   日报里展示但无点击 → 由 ``report`` 侧按弱负样本处理

安全约定（硬约束）：
  * 每个请求都必须带 HMAC 签名，签名不对一律 403；
  * 只监听 127.0.0.1，绝不对外暴露；
  * 密钥只从环境变量 / profile.yaml 读，**不写进代码、不写进日志**；
  * 同一 (item_id, kind) 重复反馈只计一次（幂等，由 Store 保证）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
from datetime import datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import score as score_mod
from .config import Config
from .store import Store, now_shanghai

#: 反馈类型 → 奖励值
REWARD_UP = score_mod.REWARD_UP
REWARD_DOWN = score_mod.REWARD_DOWN
REWARD_CLICK = score_mod.REWARD_CLICK

TOKEN_LEN = 32
DEFAULT_PORT = 8791

#: 每日权重衰减因子
DECAY_FACTOR = 0.995


def _secret_bytes(cfg: Config) -> bytes:
    secret = cfg.hmac_secret or os.environ.get("ND_HMAC_SECRET") or ""
    return secret.encode("utf-8")


def _payload(item_id: str, kind: str, exp: int | None = None) -> str:
    msg = f"{item_id}|{kind}"
    if exp is not None:
        msg += f"|{int(exp)}"
    return msg


def token_exp(cfg, now=None, ttl_days=None) -> int:
    """默认票据有效期（unix 秒）。t7-F1②：渲染层调用时把它作为 exp 传入并写进链接。"""
    now = now or now_shanghai()
    if ttl_days is None:
        ttl_days = TOKEN_TTL_DAYS
        raw = os.environ.get("ND_TOKEN_TTL_DAYS")
        if raw and raw.isdigit():
            ttl_days = int(raw)
    return int(now.timestamp()) + int(ttl_days) * 86400


def _sign(item_id: str, kind: str, exp, cfg) -> str:
    return hmac.new(
        _secret_bytes(cfg), _payload(item_id, kind, exp).encode("utf-8"), hashlib.sha256
    ).hexdigest()[:TOKEN_LEN]


def make_token(item_id: str, kind: str, cfg, exp=None) -> str:
    """32 位 HMAC 票据。exp 参与签名但不出现在票据里（不可自描述，故校验时必须给出同一 exp）。"""
    if not _secret_bytes(cfg):
        raise ValueError("反馈签名密钥未配置（ND_HMAC_SECRET）")
    if exp is not None:
        exp = int(exp)
    return _sign(item_id, kind, exp, cfg)


def parse_token_exp(token, default=None):
    """历史兼容：票据不再自描述 exp，一律返回 default。"""
    return default


def is_expired(exp, now=None) -> bool:
    """exp 为空/0/非法 → 不过期；否则 exp < now 即过期。"""
    if exp is None or exp == "":
        return False
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp <= 0:
        return False
    now = now or now_shanghai()
    return int(now.timestamp()) > exp



def verify_token(token: str, item_id: str, kind: str, cfg, exp=None) -> bool:
    """常数时间比对；票据与 exp 必须同时正确（exp=None 时校验不带 exp 的票据）。"""
    if not token:
        return False
    try:
        expected = make_token(item_id, kind, cfg, exp=exp)
    except ValueError:
        return False
    return hmac.compare_digest(str(token), expected)



def maybe_daily_decay(store: Store, now: datetime | None = None, factor: float = DECAY_FACTOR) -> bool:
    """每天最多执行一次权重衰减，防锁死。返回是否真的衰减了。"""
    now = now or now_shanghai()
    today = now.date().isoformat()
    if store.get_meta("last_decay_date") == today:
        return False
    weights = store.get_weights()
    if weights:
        store.put_weights(score_mod.decay(weights, factor))
    store.set_meta("last_decay_date", today)
    return True


def _reward_for_kind(kind: str) -> float:
    return {"up": REWARD_UP, "down": REWARD_DOWN, "click": REWARD_CLICK}.get(kind, REWARD_CLICK)


def _meta_get(store, key):
    for name in ("get_meta", "meta_get", "get_meta_value"):
        fn = getattr(store, name, None)
        if callable(fn):
            try:
                return fn(key)
            except Exception:
                return None
    return None


def _meta_set(store, key, value):
    for name in ("set_meta", "put_meta", "meta_set"):
        fn = getattr(store, name, None)
        if callable(fn):
            try:
                return fn(key, value)
            except Exception:
                return None
    return None


_WRITE_LOCK = threading.Lock()
"""写串行化（D-2 修复的第二半）。

每个请求线程用**自己懒建**的 Store 连接（见 `_db`），因此天然满足 sqlite3 的
check_same_thread=True —— 根本不存在跨线程复用连接。但同一 .db 文件上并发写仍可能
撞 `database is locked`，所以所有落库写入都在此锁内串行。
"""


def _looks_like_prefetch(headers) -> bool:
    """邮件客户端/安全网关的预取与扫描请求不是真人反馈（t7-F5④ / D-2）。

    显式规则（三条；改名单必须同步改本节）：

    ① 只有**已知的邮件扫描器/代理/预览/爬虫签名**才算预取证据 —— 见
       ``PREFETCH_UA_MARKERS``（GoogleImageProxy、YahooMailProxy、Outlook/Exchange
       图片预览、barracuda、Proofpoint、安全网关、含 bot/preview/spider 的 UA）。
    ② **通用 HTTP 客户端库名一律不算证据**（``python-urllib``、``requests``、
       ``okhttp``、``go-http-client``、curl、wget、测试框架默认 UA）—— 它们正是
       「真人点链接」与「自动化测试」的样子。曾把 ``python-urllib`` 列入名单，
       导致合法反馈被静默吞掉（D-2 的成因）。
    ③ 无 UA 才算预取。

    判定为预取时**必须可观测**，不允许静默丢弃：``/nd/f`` 回 202 +
    ``X-ND-Skipped: prefetch`` + stderr 一行日志。
    """
    ua = ""
    try:
        ua = (headers.get("User-Agent") or "").strip()
    except Exception:
        ua = ""
    if not ua:
        return True
    low = ua.lower()
    for marker in PREFETCH_UA_MARKERS:
        if marker in low:
            return True
    return False


def record_and_learn(store, cfg, item_id: str, kind: str, now=None, *, token=None, learn: bool = True, ua=None) -> dict:
    """记录一次反馈并按需学习（t7-F1②/F5）。

    · kind ∈ {up, down, click}；同一 (item_id, kind) 只计一次分（幂等）；
    · 令牌相同时视为重复提交 → 不重复学习；
    · 令牌不同（更晚一封邮件里的链接）→ 冷却窗内不重复学习，超出冷却窗允许
      一次有界纠偏（避免把「改了主意」和「重复点击」混为一谈）；
    · 预取/无 UA 请求 → 只回执不学习。
    """
    now = now or now_shanghai()
    reward = _reward_for_kind(kind)
    result = {"item_id": item_id, "kind": kind, "recorded": False, "learned": False}
    if not learn:
        result["ignored"] = True
        result["reason"] = "prefetch"
        return result
    if ua is not None and _looks_like_prefetch({"User-Agent": ua}):
        result["ignored"] = True
        result["reason"] = "prefetch"
        return result

    key = f"fb:{item_id}:{kind}"
    prev = _meta_get(store, key) or ""
    prev_token, _, prev_ts_raw = str(prev).partition("|")
    try:
        prev_ts = float(prev_ts_raw)
    except (TypeError, ValueError):
        prev_ts = 0.0
    tok = str(token or "")

    first = store.record_feedback(item_id, kind, reward, now.isoformat())
    result["recorded"] = bool(first)

    if not first and prev_token == tok:
        result["reason"] = "duplicate"
        return result
    if prev and tok and prev_token != tok:
        if prev_ts and (now.timestamp() - prev_ts) < FEEDBACK_COOLDOWN_SECONDS:
            result["reason"] = "cooldown"
            return result
        result["corrected"] = True
    elif not first:
        result["reason"] = "duplicate"
        return result

    item = store.get_item(item_id)
    if item is None:
        result["reason"] = "unknown-item"
        return result

    deltas = store.get_weights()
    merged = score_mod.merged_weights(cfg, deltas)
    parsed = None
    try:
        from .enrich import parsed_for_item
        parsed = parsed_for_item(item, now)
    except Exception:
        parsed = None
    updated = score_mod.apply_feedback_deltas(deltas, merged, item, cfg, now, reward=reward, parsed=parsed)
    store.put_weights(updated)
    _meta_set(store, key, f"{tok}|{int(now.timestamp())}")
    result["learned"] = True
    result["trained_keys"] = len(updated)
    return result



class _Handler(BaseHTTPRequestHandler):
    server_version = "notice-digest/0.1"

    def __init__(self, *args, store: Store, cfg: Config, store_factory=None, **kwargs):
        self.store = store
        self.cfg = cfg
        self.store_factory = store_factory
        super().__init__(*args, **kwargs)

    def _db(self) -> Store:
        """取本线程可用的 Store。

        handler 跑在 ThreadingHTTPServer 的工作线程里，而 Store 的 sqlite 连接是默认
        check_same_thread=True（store.py 未关掉）—— 主线程建的连接在工作线程里一碰就抛
        ProgrammingError，用户点邮件里的 👍/👎 会得到 HTTP 500 且库里 0 行，**唯一的生产
        反馈通道静默失效**。所以每个线程懒开一条自己的连接（按 db_path 复刻 + 建表）。
        """
        if self.store_factory is None:
            return self.store
        try:
            return self.store_factory()
        except Exception:
            return self.store

    # 默认日志会打印请求行（含签名）——覆盖掉，避免签名进日志
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _deny(self, code: int = 403, text: str = "forbidden") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, text: str, status: int = 200, extra_headers: dict | None = None) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        query = parse_qs(parsed_url.query)
        item_id = (query.get("id") or [""])[0]
        token = (query.get("t") or [""])[0]
        exp_raw = (query.get("exp") or [""])[0]
        exp = int(exp_raw) if exp_raw.isdigit() else None
        ua = self.headers.get("User-Agent") or ""
        prefetch = _looks_like_prefetch(self.headers)

        if parsed_url.path == "/nd/f":
            kind = (query.get("k") or [""])[0]
            if kind not in ("up", "down"):
                return self._deny(400, "bad kind")
            if not item_id or not verify_token(token, item_id, kind, self.cfg, exp):
                return self._deny(403, "bad signature")
            if is_expired(exp):
                return self._deny(403, "expired")
            if prefetch:
                # 不静默（t7 裁决 2）：202 + X-ND-Skipped 头 + stderr 一行日志。
                # 上一轮 D-2 之所以难查，就是因为「丢弃」与「成功」都回 200。
                print(
                    f"[feedback] /nd/f 判定为预取，已忽略（未计入偏好）："
                    f"item={item_id} kind={kind} ua={ua!r}",
                    file=sys.stderr,
                )
                return self._ok(
                    "<h2>已忽略（疑似邮件客户端预取）</h2><p>这次点击没有计入偏好</p>",
                    status=202,
                    extra_headers={"X-ND-Skipped": "prefetch"},
                )
            with _WRITE_LOCK:
                res = record_and_learn(self._db(), self.cfg, item_id, kind, token=token, ua=ua)
            mark = "👍 已记下，会多推这类" if kind == "up" else "👎 已记下，会少推这类"
            extra = "" if res["recorded"] else "（此前已记录，未重复计分）"
            return self._ok(f"<h2>{mark}</h2><p>{item_id}{extra}</p>")

        if parsed_url.path == "/nd/c":
            if not verify_token(token, item_id, "click", self.cfg, exp):
                return self._deny(403, "bad signature")
            if is_expired(exp):
                return self._deny(403, "expired")
            store = self._db()
            item = store.get_item(item_id)
            if item is None:
                return self._deny(404, "unknown item")
            if not prefetch:
                with _WRITE_LOCK:
                    record_and_learn(store, self.cfg, item_id, "click", token=token, ua=ua)
            url = item.get("url") or ""
            if not url:
                return self._deny(404, "no url")
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed_url.path == "/nd/ics":
            # 「+ 日历」按钮：确认参加 = 强正向信号。与 /nd/f 同一条安全链：
            # kind="ics" 签名校验（渲染层 make_token 不带 exp ⇒ 这里 exp 同样按 None 验）
            # → 预取过滤 → 幂等记账 → 找条目 → 有明确开始时间才产单事件 .ics。
            if not item_id or not verify_token(token, item_id, "ics", self.cfg, exp):
                return self._deny(403, "bad signature")
            if is_expired(exp):
                return self._deny(403, "expired")
            store = self._db()
            if not prefetch:
                with _WRITE_LOCK:
                    record_and_learn(store, self.cfg, item_id, "up", token=token, ua=ua)
            item = store.get_item(item_id)
            if item is None:
                return self._deny(404, "unknown item")
            # 惰性导入：保持反馈服务冷启动不变，只有真点「+ 日历」才加载解析/渲染层。
            from .enrich import parsed_for_item
            from .render import build_single_event_ics
            from urllib.parse import quote

            parsed = parsed_for_item(item, now_shanghai())
            if getattr(parsed, "start", None) is None:
                return self._deny(404, "no parseable start")
            body = build_single_event_ics(item, parsed, now_shanghai())
            raw_name = (item.get("title") or "event").strip()[:40] or "event"
            safe_name = "".join(
                ch for ch in raw_name if ch not in '\\/:*?"<>|\r\n\t'
            ) or "event"
            file_base = f"{safe_name}.ics"
            try:
                file_base.encode("ascii")
                disposition = f'attachment; filename="{file_base}"'
            except UnicodeEncodeError:
                # http.server 头部按 latin-1 写出：中文文件名必须走 RFC 5987 filename*
                disposition = "attachment; filename*=UTF-8''" + quote(file_base)
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed_url.path in ("/nd/health", "/healthz"):
            return self._ok("ok")

        return self._deny(404, "not found")


def serve(
    cfg: Config,
    store: Store,
    host: str = "127.0.0.1",
    port: int | None = None,
    ready_callback=None,
) -> ThreadingHTTPServer:
    """启动反馈服务。**只绑定 127.0.0.1**，返回 server 对象（调用方负责 serve_forever）。"""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("反馈服务只允许绑定 127.0.0.1，绝不对外暴露")
    if not _secret_bytes(cfg):
        raise ValueError("缺少 ND_HMAC_SECRET，拒绝以无签名保护的方式启动反馈服务")
    if port is None:
        port = DEFAULT_PORT
        base = cfg.feedback_base or ""
        if ":" in base.rsplit("/", 1)[-1]:
            tail = base.rsplit(":", 1)[-1]
            if tail.isdigit():
                port = int(tail)

    # handler 跑在工作线程：按 db_path 给每个线程发一条自己的 sqlite 连接（见 _Handler._db）。
    # 只在磁盘库上分叉（:memory: 分叉会得到另一个空库）。
    local = threading.local()
    store_cls = type(store)
    db_path = getattr(store, "db_path", None)
    can_fork = db_path is not None and str(db_path) != ":memory:"

    def _store_factory():
        st = getattr(local, "store", None)
        if st is None:
            st = store_cls(db_path)
            st.init_schema()
            local.store = st
        return st

    handler = partial(
        _Handler, store=store, cfg=cfg, store_factory=_store_factory if can_fork else None
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
    httpd.daemon_threads = True
    if ready_callback:
        ready_callback(httpd)
    return httpd


# ------------------------------------------------- t7-F1 反馈服务可启动/可达性
ALLOW_LEGACY_TOKENS = False
TOKEN_TTL_DAYS = 30
FEEDBACK_COOLDOWN_SECONDS = 6 * 3600
#: 只登记「邮件安全网关 / 图片代理 / 爬虫」这类**确凿的预取方**的特征串。
#: 刻意**不登记** curl / wget / python-requests / python-urllib / okhttp 这类
#: 通用 HTTP 客户端库名 —— 它们是「谁在发请求」的工具标识，不是「这是预取」的证据，
#: 把它们一并当预取会让任何脚本化/自动化驱动（含验收探针、本机自测）的合法签名反馈被静默丢弃。
PREFETCH_UA_MARKERS = (
    "googleimageproxy", "yahoomailproxy", "ggpht", "proofpoint", "mimecast",
    "barracuda", "googlebot", "bingbot", "bot/", "spider", "preview", "prefetch",
    "headlesschrome", "scanner", "safe-links", "outlook-image-proxy",
)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_loopback_base(base: str) -> bool:
    if not base:
        return True
    host = urlparse(base).hostname or ""
    return host in LOOPBACK_HOSTS


def resolve_public_base(cfg, env=None) -> str:
    """解析邮件里给用户点的公网反馈地址（t7-F1③）。

    顺序：ND_FEEDBACK_PUBLIC_BASE / ND_FEEDBACK_BASE 环境变量 → 配置项。
    刻意不再默认 127.0.0.1：用户点邮件里的链接时，那台机器上并没有这个服务。
    解析不到公网地址时返回空串，渲染层会进入「无反馈按钮」模式（诚实降级）。
    """
    env = env if env is not None else os.environ
    for name in ("ND_FEEDBACK_PUBLIC_BASE", "ND_FEEDBACK_BASE"):
        value = (env.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    base = (getattr(cfg, "feedback_base", "") or "").strip()
    if base and not is_loopback_base(base):
        return base.rstrip("/")
    return ""


def probe_public_base(base: str, timeout: float = 5.0) -> tuple:
    """启动时探测反馈地址是否真的可达（返回 (ok, detail)）。"""
    if not base:
        return False, "未配置公网反馈地址"
    url = base.rstrip("/") + "/nd/health"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "notice-digest/0.1 health"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(64).decode("utf-8", "replace")
            ok = resp.status == 200
            return ok, f"{url} -> {resp.status} {body.strip()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{url} -> {type(exc).__name__}: {exc}"
