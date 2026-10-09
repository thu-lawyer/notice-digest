"""pkuknow.cn 只读 JSON API 客户端。

已实测的硬约束（照做，不要重新试错）：
  * campus 由 URL **路径前缀**选择（/thu/ 清华、/ruc/ 人大）；``?campus=xx`` 无效。
  * 列表 ``GET {base}/{campus}/api/notices?page=N``；page_size 被忽略，恒返 30 条。
  * 按 published_at 倒序；高页码 items 可能为 null → 必须容忍 None 而不是抛 TypeError。
  * 所有日期区间参数（from/to/since/…）全无效 → 增量只能靠分页 + 本地按 id 去重。
  * 详情 ``GET {base}/{campus}/api/notices/<urlencode(id)>``；id 含冒号必须编码。
  * **访客会话闸门**：不带 cookie 的冷请求对列表与详情端点**恒返 403**
    （body ``{"code":"READ_SESSION_REQUIRED","scope":"missing_session"}``），
    响应头里同时 ``Set-Cookie: __Host-pku_read_guest=<uuid>.<ts>.<hmac>;
    Path=/; Max-Age=86400; HttpOnly; SameSite=Lax; Secure`` 并带 ``retry-after: 1``。
    因此必须持 cookie jar 复用站点下发的这个访客 cookie：同一个 URL 冷 403 后
    **带该 cookie 原样重放一次**即可 200。cookie 在同一运行内跨请求复用
    （列表第 1 页拿到的 cookie 供后续列表页与详情端点共用），所以整轮抓取只在
    第一次请求上付一次 403 的代价。
  * **不走系统代理**：只认显式环境变量（http_proxy/https_proxy），忽略操作系统
    级代理设置。macOS 的系统代理经 ``_scproxy`` 被 urllib 默认读走，而站点是
    国内直连可达的；一旦系统代理的 TLS 隧道不通，整轮抓取会死在
    SSL UNEXPECTED_EOF 上（不是 403，也不是超时）。详见 ``_proxy_handler``。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://pkuknow.cn"

#: 路径前缀 → 站点；仅这几个前缀有效
CAMPUS_PREFIXES = {
    "thu": "thu",
    "ruc": "ruc",
    "pku": "api",
}

USER_AGENT = "notice-digest/0.1 (+https://github.com/thu-lawyer/notice-digest)"

#: 站点下发的访客会话 cookie 名。``__Host-`` 前缀要求 Secure + Path=/ + 无 Domain，
#: 这三条由 ``http.cookiejar`` 原生按 RFC 6265 解析与保存，不需要手拼 Cookie 头。
GUEST_COOKIE_NAME = "__Host-pku_read_guest"

#: 单个 URL 上「冷 403 + 带 cookie 重放」这条路最多发出几次请求。
#: 会话重放只允许一次，且不进入常规重试循环 ⇒ 礼貌抓取：不会被放大成 retries×2。
MAX_REQUESTS_PER_URL_SESSION_RETRY = 2

#: 会话重放前最多等待的秒数（站点给 ``retry-after: 1``，照它稍等，但设上界）。
_MAX_RETRY_AFTER = 5.0


class FetchError(Exception):
    """列表 / 详情请求失败或返回结构异常。"""


def _resolve_prefix(campus: str) -> str:
    key = (campus or "thu").strip().lower()
    return CAMPUS_PREFIXES.get(key, key)


#: 最低层传输打桩点：离线测试直接替换 ``urllib.request.urlopen`` 即可拦下真实请求
#: （``tests/test_integration.py`` 的致命退出码分级用例就打在这里，好让重试循环、
#: JSON 解析、异常归类**真实执行**）。``Session.open`` 只在它被替换过时才改走它，
#: 默认仍走本会话 opener —— 也就是 cookie jar + 只认环境变量的代理策略。
_STDLIB_URLOPEN = urllib.request.urlopen


def _proxy_handler() -> urllib.request.ProxyHandler:
    """只认显式环境变量代理，忽略操作系统级代理设置。

    ``build_opener()`` 不带参数时会用 ``urllib.request.getproxies()``，它在
    macOS 上会去读系统网络设置的代理（``_scproxy``）——那是 GUI 层面的开关，
    抓取端既看不见也管不着。pkuknow.cn 是国内站点、直连可达，把抓取链路交给
    系统代理只会引入一个不可控的单点（本轮实测：系统代理开着但隧道重置，
    所有请求死在 ``SSL: UNEXPECTED_EOF_WHILE_READING``）。

    部署机通常是 Linux 服务器（systemd timer），那里既没有系统代理也没有
    环境变量代理，行为与从前完全一致；需要走代理的部署用 ``https_proxy``
    显式指定即可。
    """
    proxies: dict[str, str] = {}
    for scheme in ("http", "https"):
        for name in (f"{scheme}_proxy", f"{scheme.upper()}_PROXY"):
            value = os.environ.get(name)
            if value:
                proxies[scheme] = value
                break
    return urllib.request.ProxyHandler(proxies)


class Session:
    """一次运行内的访客会话：一个 cookie jar + 一个基于它的 opener。

    ``__Host-`` 前缀 cookie 的三条硬要求（Secure + Path=/ + 无 Domain）交给
    ``http.cookiejar`` 按 RFC 6265 原生处理；``policy`` 只是留给离线测试注入的
    缝隙（环回 fixture 没有 TLS，需要放行 Secure cookie 的回送）。
    """

    def __init__(self, policy: http.cookiejar.CookiePolicy | None = None):
        self.jar = http.cookiejar.CookieJar(
            policy if policy is not None else http.cookiejar.DefaultCookiePolicy()
        )
        self.opener = urllib.request.build_opener(
            _proxy_handler(), urllib.request.HTTPCookieProcessor(self.jar)
        )

    @property
    def has_cookies(self) -> bool:
        return any(True for _ in self.jar)

    def store_response_cookies(self, request: urllib.request.Request, response) -> int:
        """从响应（含 ``HTTPError``）里收下 Set-Cookie，返回收下前已有 cookie 数。

        显式调用是刻意的：处理器链里 ``HTTPErrorProcessor`` 可能先于
        ``HTTPCookieProcessor`` 把 4xx/5xx 抛成异常，403 响应里的
        ``Set-Cookie`` 必须自己兜住，否则拿不到会话 cookie，重放照样 403。
        """
        before = len(self.jar)
        try:
            self.jar.extract_cookies(response, request)
        except AttributeError:
            # 离线打桩用的假响应壳没有 ``info()``，无从提取；真实响应（含
            # ``HTTPError``）都实现该协议，实际链路上不会因此静默丢 cookie。
            return before
        return before

    def open(self, request: urllib.request.Request, timeout: int):
        """最低层传输 + cookie 归档，返回响应对象（HTTPError 原样抛出）。

        默认走本会话 opener（``_proxy_handler()`` 的代理策略 + cookie jar）。
        唯一例外是**被替换过的** ``urllib.request.urlopen``：那是有意的离线打桩点，
        尊重它才能让重试循环、JSON 解析、异常归类真实执行，也才让调用方在
        ``urllib.request.urlopen`` 这一层打的桩继续生效。
        """
        transport = urllib.request.urlopen
        try:
            if transport is _STDLIB_URLOPEN:
                response = self.opener.open(request, timeout=timeout)
            else:
                response = transport(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # 处理器链里 HTTPErrorProcessor 与 HTTPCookieProcessor 的先后不是契约，
            # 4xx/5xx 响应里的 Set-Cookie 自己再兜一遍（重复收同一条是幂等的）。
            self.store_response_cookies(request, exc)
            raise
        self.store_response_cookies(request, response)
        return response

    def get(self, url: str, *, timeout: int = 20):
        """按 URL 发一次 GET（会话级入口，带 UA/Accept 头），返回响应对象。"""
        return self.open(_build_request(url), timeout)


_DEFAULT_SESSION = Session()


def default_session() -> Session:
    """进程级默认会话：同一运行内跨请求复用访客 cookie。"""
    return _DEFAULT_SESSION


def make_session(policy: http.cookiejar.CookiePolicy | None = None) -> Session:
    """新建一个独立会话（``policy`` 供离线测试注入）。"""
    return Session(policy)


def _build_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )


def _retry_after_seconds(response) -> float:
    """读响应的 ``Retry-After``（秒）；读不到或不可解析则为 0。"""
    try:
        raw = response.headers.get("Retry-After")
    except Exception:  # pragma: no cover - 头部缺失/对象不支持
        return 0.0
    if not raw:
        return 0.0
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(seconds, _MAX_RETRY_AFTER))


def _session_cookie_delivered(response) -> bool:
    """这次响应是否下发了 ``Set-Cookie``（⇒ 值不值得带 cookie 重放）。

    **不能**用「jar 里多了几条」当判据：``HTTPCookieProcessor`` 通常已先把
    403 响应的 ``Set-Cookie`` 收进 jar，事后再看增量恒为 0。判据必须落在响应头。
    """
    try:
        values = response.headers.get_all("Set-Cookie") or []
    except AttributeError:  # pragma: no cover - 假响应壳没有 headers
        return False
    return bool(values)


def _read_json(session: Session, url: str, timeout: int) -> dict:
    """发一次请求并解析 JSON；HTTPError 原样抛出（cookie 归档在 ``Session.open``）。"""
    with session.get(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise FetchError(f"返回的不是 JSON 对象: {url}")
    return data


def _get_json(url: str, timeout: int, retries: int, session: Session | None = None) -> dict:
    sess = session if session is not None else _DEFAULT_SESSION
    attempts = max(1, retries)
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            return _read_json(sess, url, timeout)
        except urllib.error.HTTPError as exc:
            last_err = FetchError(f"HTTP {exc.code} for {url}")
            if exc.code == 403 and _session_cookie_delivered(exc) and sess.has_cookies:
                # —— 访客会话闸门 ——
                # 冷请求的 403 响应里站点已下发访客会话 cookie（cookiejar 已收下），
                # 带 cookie 原样重放 **一次**（同一运行内），随后无论成败都收手：
                # 重放不走常规重试循环 ⇒ 这个 URL 上最多 2 次请求，绝不成倍放大。
                delay = _retry_after_seconds(exc)
                if delay:
                    time.sleep(delay)
                try:
                    return _read_json(sess, url, timeout)
                except urllib.error.HTTPError as exc2:
                    # 会话「又更新了」：新 cookie 已进 jar，留给后续请求用；
                    # 本 URL 不再打第三次。
                    raise FetchError(f"HTTP {exc2.code} for {url}")
                except (urllib.error.URLError, TimeoutError, OSError) as exc2:
                    raise FetchError(f"网络错误 {exc2} for {url}")
                except (ValueError, FetchError) as exc2:
                    raise exc2 if isinstance(exc2, FetchError) else FetchError(str(exc2))
            if exc.code == 403:
                # 403 但响应里没有会话 cookie → 再原样重发一次是同一个冷请求，
                # 只会白打站点，直接收手。
                break
            if exc.code in (400, 404):
                break  # 请求本身有问题，重试无意义
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = FetchError(f"网络错误 {exc} for {url}")
        except (ValueError, FetchError) as exc:
            last_err = exc if isinstance(exc, FetchError) else FetchError(str(exc))
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    raise last_err or FetchError(f"请求失败: {url}")


def fetch_list(campus: str, page: int, session=None, *, timeout: int = 20, retries: int = 3) -> dict:
    """抓列表某页，返回**原始 JSON**。

    结构异常（缺少 items 字段类型不对等）抛 FetchError；
    但 ``items`` 为 null 是站点正常行为（高页码无数据），原样返回由调用方处理。
    """
    prefix = _resolve_prefix(campus)
    url = f"{BASE_URL}/{prefix}/api/notices?page={int(page)}"
    data = _get_json(url, timeout, retries, session)
    if "items" in data:
        items = data["items"]
        if items is not None and not isinstance(items, list):
            raise FetchError(f"items 字段类型异常: {type(items).__name__} ({url})")
    elif "total" not in data and "page" not in data:
        raise FetchError(f"响应结构异常，既无 items 也无分页元信息: {url}")
    return data


def fetch_detail(campus: str, item_id: str, session=None, *, timeout: int = 20, retries: int = 3) -> dict:
    """抓单条详情。item_id 含冒号，必须 urlencode。"""
    if not item_id:
        raise FetchError("item_id 不能为空")
    prefix = _resolve_prefix(campus)
    encoded = urllib.parse.quote(str(item_id), safe="")
    url = f"{BASE_URL}/{prefix}/api/notices/{encoded}"
    data = _get_json(url, timeout, retries, session)
    if not data.get("id") and not data.get("title"):
        raise FetchError(f"详情响应缺少 id/title: {url}")
    return data
