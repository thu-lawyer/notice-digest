"""t17 回归测试：pkuknow.cn 访客会话闸门（冷 403 → 带 cookie 重放 200）。

**离线、确定性**：起一个本地假 HTTP 服务器复现站点的两道行为 ——
  * 不带访客会话 cookie 的请求 → ``403`` + ``Set-Cookie: __Host-pku_read_guest=…``
    + ``Retry-After: 1`` + body ``{"code":"READ_SESSION_REQUIRED",…}``
  * 带该 cookie 的请求 → ``200`` + 正常 JSON

断言的是**机制**（重放几次、带没带 cookie、cookie 属性），不是某次真实网络请求的
结果，所以不依赖上游可用性。

反证（deliberately removing cookie handling must turn this red）：把
``notice_digest/fetch.py`` 换回改动前的版本（裸 ``urlopen``、403 一律不重试），
本文件必须变红。命令见任务 output。
"""

from __future__ import annotations

import http.client
import http.cookiejar
import http.server
import json
import os
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from notice_digest import fetch as fetch_mod

GUEST = fetch_mod.GUEST_COOKIE_NAME

#: 与真实站点同形的一条 Set-Cookie（值用假串，不落真实会话标识）。
SET_COOKIE = (
    f"{GUEST}=11111111-2222-3333-4444-555555555555.1791565291."
    "e9051de44419cb2889f0b7b55a2aa290d7b87fb9f591e135028dca84b808f561"
    "; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax; Secure"
)

GATE_BODY = json.dumps(
    {
        "code": "READ_SESSION_REQUIRED",
        "scope": "missing_session",
        "error": "会话已更新，请刷新页面后再试。",
    },
    ensure_ascii=False,
).encode("utf-8")

LIST_BODY = json.dumps(
    {"items": [{"id": "weixinzs_1:1", "title": "t1"}], "total": 1, "page": 1, "page_size": 30},
    ensure_ascii=False,
).encode("utf-8")

DETAIL_BODY = json.dumps(
    {"id": "weixinzs_1:1", "title": "t1", "ai_event_time": "10月9日 19:00"},
    ensure_ascii=False,
).encode("utf-8")


class _LoopbackPolicy(http.cookiejar.DefaultCookiePolicy):
    """只给离线 fixture 用：环回服务器没有 TLS，放行 Secure cookie 的回送。

    真实站点是 https，RFC 6265 的「Secure cookie 只在 https 上回送」在线上天然满足；
    这里覆盖的仅是**传输层**判断，cookie 的 Secure 属性照原样解析与保存 ——
    属性本身由 test_07 用真实 https 请求单独断言。
    """

    def return_ok_secure(self, cookie, request):
        return True


class _FakeSite:
    """本地假站点；每个路径的行为可编排，并逐条记录收到的 Cookie 头。"""

    def __init__(self):
        self.requests = []  # [(path_with_query, cookie_header)]
        self.behaviour = {}  # path -> fn(cookie_header) -> (status, headers, body)
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                cookie = self.headers.get("Cookie")
                site.requests.append((self.path, cookie))
                fn = site.behaviour.get(parsed.path)
                if fn is None:
                    status, headers, body = 404, {}, b'{"error":"not found"}'
                else:
                    status, headers, body = fn(cookie)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://%s:%d" % self._server.server_address[:2]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def urls(self):
        return [u for u, _ in self.requests]

    def cookies(self):
        return [c for _, c in self.requests]


class _Base(unittest.TestCase):
    def setUp(self):
        self.site = _FakeSite()
        self.addCleanup(self.site.stop)

        # 指向假站点：BASE_URL 在调用时读取，patch 即生效。
        original_base = fetch_mod.BASE_URL
        fetch_mod.BASE_URL = self.site.base_url
        self.addCleanup(lambda: setattr(fetch_mod, "BASE_URL", original_base))

        # 每次测试一个干净 jar，读数的确定性不依赖进程级默认会话。
        self.session = fetch_mod.make_session(_LoopbackPolicy())

        # 不许真睡：记录睡眠时长，既快又能断言「按 Retry-After 稍等」。
        original_sleep = fetch_mod.time.sleep
        self.sleeps = []
        fetch_mod.time.sleep = self.sleeps.append
        self.addCleanup(lambda: setattr(fetch_mod.time, "sleep", original_sleep))

    def gate(self, cookie):
        """冷请求 403 + 下发会话 cookie；带 cookie 则 200 返回 ``body``。"""
        if cookie and GUEST in cookie:
            return 200, {}, self.ok_body
        return 403, {"Set-Cookie": SET_COOKIE, "Retry-After": "1"}, GATE_BODY

    ok_body = LIST_BODY


class TestGuestSessionGate(_Base):
    ok_body = LIST_BODY

    def test_01_cold_403_then_cookie_replay_succeeds(self):
        """冷 403 后**同一运行内**带站点下发的 cookie 重放一次即成功。"""
        self.site.behaviour["/thu/api/notices"] = self.gate
        data = fetch_mod.fetch_list("thu", 1, session=self.session)

        self.assertEqual([{"id": "weixinzs_1:1", "title": "t1"}], data["items"])
        self.assertEqual(2, len(self.site.requests), "冷 403 + 重放一次 = 恰好 2 次请求")
        self.assertIsNone(self.site.cookies()[0], "第一次请求必须是不带 cookie 的冷请求")
        self.assertIn(GUEST, self.site.cookies()[1] or "", "重放必须带上站点下发的会话 cookie")
        self.assertEqual(
            self.site.urls()[0], self.site.urls()[1], "重放打的是同一个 URL"
        )
        self.assertEqual([1.0], self.sleeps, "按站点 Retry-After: 1 稍等后再重放")
        self.assertTrue(self.session.has_cookies, "cookie 已留在会话里供后续请求复用")

    def test_02_permanent_gate_retries_at_most_once(self):
        """一直 403 时最多重放一次 —— 无死循环，也不被 retries 放大。"""
        self.site.behaviour["/thu/api/notices"] = lambda cookie: (
            403,
            {"Set-Cookie": SET_COOKIE, "Retry-After": "1"},
            GATE_BODY,
        )
        with self.assertRaises(fetch_mod.FetchError) as ctx:
            fetch_mod.fetch_list("thu", 1, session=self.session, retries=3)

        self.assertIn("403", str(ctx.exception))
        self.assertEqual(
            fetch_mod.MAX_REQUESTS_PER_URL_SESSION_RETRY, len(self.site.requests)
        )
        self.assertLessEqual(len(self.site.requests), 2, "单个 URL 绝不超过 2 次请求")

    def test_03_no_replay_when_first_response_is_ok(self):
        """首次就是 200 → 不重放（不能凭白多打一次）。"""
        self.site.behaviour["/thu/api/notices"] = lambda cookie: (200, {}, LIST_BODY)
        data = fetch_mod.fetch_list("thu", 1, session=self.session)

        self.assertEqual(1, len(data["items"]))
        self.assertEqual(1, len(self.site.requests))
        self.assertEqual([], self.sleeps)

    def test_04_403_without_session_cookie_is_not_replayed(self):
        """403 但没给 Set-Cookie → 原样重发无意义，一次即收手。"""
        self.site.behaviour["/thu/api/notices"] = lambda cookie: (403, {}, GATE_BODY)
        with self.assertRaises(fetch_mod.FetchError):
            fetch_mod.fetch_list("thu", 1, session=self.session, retries=3)

        self.assertEqual(1, len(self.site.requests))

    def test_05_detail_endpoint_recovers_through_the_same_gate(self):
        """详情端点同样冷 403（READ_SESSION_REQUIRED）→ 重放一次取到正文。"""
        path = "/thu/api/notices/" + urllib.parse.quote("weixinzs_1:1", safe="")
        self.site.behaviour[path] = self.gate
        self.ok_body = DETAIL_BODY

        data = fetch_mod.fetch_detail("thu", "weixinzs_1:1", session=self.session)

        self.assertEqual("weixinzs_1:1", data["id"])
        self.assertTrue(data.get("ai_event_time"), "详情正文非空")
        self.assertEqual(2, len(self.site.requests))
        self.assertEqual(path, urllib.parse.urlparse(self.site.urls()[0]).path)
        self.assertIsNone(self.site.cookies()[0])
        self.assertIn(GUEST, self.site.cookies()[1] or "")

    def test_06_cookie_is_reused_across_requests_in_one_run(self):
        """第一页付一次 403 的代价；第二页直接带 cookie 一次成功。"""
        self.site.behaviour["/thu/api/notices"] = self.gate
        fetch_mod.fetch_list("thu", 1, session=self.session)
        fetch_mod.fetch_list("thu", 2, session=self.session)

        self.assertEqual(3, len(self.site.requests), "2 次（冷+重放）+ 1 次（复用 cookie）")
        self.assertEqual(
            [None, GUEST, GUEST],
            [GUEST if c and GUEST in c else None for c in self.site.cookies()],
        )
        self.assertEqual([1.0], self.sleeps, "复用阶段不再撞 403，也就无需再等")

    def test_08_non_session_retry_behaviour_is_unchanged(self):
        """与闸门无关的失败照旧按 retries 重试（本次改动不削减既有重试）。"""
        self.site.behaviour["/thu/api/notices"] = lambda cookie: (500, {}, b"{}")
        with self.assertRaises(fetch_mod.FetchError):
            fetch_mod.fetch_list("thu", 1, session=self.session, retries=3)

        self.assertEqual(3, len(self.site.requests))
        self.assertEqual([1.5, 3.0], self.sleeps, "退避时长与改动前一致")


class _HeaderStub:
    """最小响应壳：只提供 cookiejar 需要的 ``info()``。"""

    def __init__(self, items):
        self._msg = http.client.HTTPMessage()
        for key, value in items:
            self._msg[key] = value

    def info(self):
        return self._msg


class TestHostPrefixedCookieHandling(unittest.TestCase):
    """``__Host-`` 前缀的三条硬要求：Secure + Path=/ + 无 Domain。"""

    def _jar_with_guest_cookie(self):
        jar = http.cookiejar.CookieJar()
        jar.extract_cookies(
            _HeaderStub([("Set-Cookie", SET_COOKIE)]),
            urllib.request.Request("https://pkuknow.cn/thu/api/notices?page=1"),
        )
        return jar

    def test_07_attributes_preserved_and_secure_only_over_https(self):
        jar = self._jar_with_guest_cookie()
        cookies = list(jar)
        self.assertEqual(1, len(cookies), "__Host- 前缀 cookie 必须被 cookiejar 收下")
        cookie = cookies[0]
        self.assertEqual(GUEST, cookie.name)
        self.assertTrue(cookie.secure, "属性 Secure 必须保留")
        self.assertEqual("/", cookie.path, "Path=/ 必须保留")
        self.assertFalse(cookie.domain_specified, "__Host- 前缀不得带 Domain ⇒ host-only")
        self.assertEqual("pkuknow.cn", cookie.domain)

        https_req = urllib.request.Request("https://pkuknow.cn/thu/api/notices?page=1")
        jar.add_cookie_header(https_req)
        self.assertIn(GUEST, https_req.get_header("Cookie") or "", "https 上必须回送")

        http_req = urllib.request.Request("http://pkuknow.cn/thu/api/notices?page=1")
        jar.add_cookie_header(http_req)
        self.assertIsNone(http_req.get_header("Cookie"), "Secure cookie 不得在 http 上回送")


class TestProxyPolicy(unittest.TestCase):
    """抓取链路只认显式环境变量代理，忽略操作系统级代理设置。

    macOS 的系统代理会被 ``urllib`` 经 ``_scproxy`` 默认读走（``getproxies()``），
    而那是 GUI 层面的开关、抓取端既看不见也管不着；t17 实测一旦那条隧道不通，
    整轮抓取会死在 ``SSL: UNEXPECTED_EOF_WHILE_READING``（不是 403、也不是超时）。
    这里**注入一个假的 ``getproxies()``** 来反证：即使「系统层面」报出代理，
    抓取链路也必须直连。

    反证（deliberately removing proxy handling must turn this red）：把
    ``Session.__init__`` 的 ``build_opener`` 改回只传 ``HTTPCookieProcessor``，
    则 ``build_opener`` 补回的默认 ``ProxyHandler()`` 会去调 ``getproxies()``
    ——也就是被本测试注入的那个系统代理——于是 opener 上出现**非空代理表**，
    第一条断言变红。（实测变体结果见任务 output。）
    """

    ENV_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")

    def _effective_proxies(self, session):
        """opener 上最终生效的代理表；空表 == 直连。

        ``build_opener`` 是按方法注册 handler 的：一个**空代理表**的
        ``ProxyHandler`` 不会生成任何 ``*_open`` 方法，因此它虽然顶掉了默认
        handler，却不会被留在 ``opener.handlers`` 里（实测
        ``build_opener(ProxyHandler({})).handlers`` 中并无 ProxyHandler）。
        所以「找不到 ProxyHandler」与「代理表为空」都必须判成直连，真正要否证的是
        **存在一个非空代理表** —— 那才是「请求会被送去代理」的充分条件。
        """
        for handler in session.opener.handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                return dict(handler.proxies)
        return {}

    def test_09_system_proxy_is_ignored_and_env_proxy_is_honoured(self):
        blank = {key: "" for key in self.ENV_KEYS}
        system_proxy = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

        with mock.patch.dict(os.environ, blank), mock.patch(
            "urllib.request.getproxies", return_value=system_proxy
        ):
            self.assertEqual(
                {},
                self._effective_proxies(fetch_mod.Session()),
                "系统级代理（getproxies）不得进入抓取链路",
            )
            self.assertEqual({}, fetch_mod._proxy_handler().proxies)

        with mock.patch.dict(os.environ, {**blank, "https_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(
                {"https": "http://127.0.0.1:9"},
                fetch_mod._proxy_handler().proxies,
                "显式环境变量代理必须照用",
            )

        with mock.patch.dict(os.environ, {**blank, "HTTPS_PROXY": "http://127.0.0.1:8"}):
            self.assertEqual(
                {"https": "http://127.0.0.1:8"},
                fetch_mod._proxy_handler().proxies,
                "大写环境变量同样生效",
            )

        with mock.patch.dict(os.environ, {**blank, "http_proxy": "http://127.0.0.1:7"}):
            self.assertEqual(
                {"http": "http://127.0.0.1:7"},
                fetch_mod._proxy_handler().proxies,
                "http_proxy 与 https_proxy 各自独立",
            )


if __name__ == "__main__":
    unittest.main()


class _StubResponse:
    """最低层打桩用的假响应壳：只实现 ``read()``/``status``/上下文协议。

    与 ``tests/test_integration.py`` 的 ``_FakeHTTPResponse`` 同形 —— 那边把
    ``urllib.request.urlopen`` 换成这个壳，用真实的重试循环与 JSON 解析驱动用例。
    """

    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class TestLowLevelTransportSeam(unittest.TestCase):
    """最低层传输打桩点必须有效：替换 ``urllib.request.urlopen`` 必须能拦下请求。

    这是 ``tests/test_integration.py``（致命退出码分级）依赖的接口约定：打桩点放在
    最底层 ``urlopen``，从而让重试循环、JSON 解析、异常归类真实执行。谁把请求改走
    别的通道（例如直接调 ``session.opener.open`` 而不看打桩点），本用例的
    ``calls`` 就是 0，立刻变红。
    """

    def test_10_low_level_urlopen_stub_still_intercepts_requests(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append((req.full_url, timeout))
            return _StubResponse(LIST_BODY)

        session = fetch_mod.make_session()
        self.assertTrue(
            hasattr(session, "get"),
            "make_session() 必须返回会话式对象（可以是 None，但不能是没有 get 的壳）",
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = fetch_mod.fetch_list("thu", 1, session=session, timeout=20, retries=3)
        self.assertEqual(len(calls), 1, f"最低层打桩点未被命中：calls={calls}")
        self.assertEqual("thu", urllib.parse.urlparse(calls[0][0]).path.split("/")[1])
        self.assertEqual(20, calls[0][1], "timeout 必须原样传到打桩点")
        self.assertEqual([{"id": "weixinzs_1:1", "title": "t1"}], data["items"])
