"""E05 回归 —— 工具只读请求继承真实用户权限。

对应 docs/AGENT_REMEDIATION_HANDOFF_2026-10-23.md 的 E05-T01…T10。
不修改正式账号与角色；不改默认权限矩阵。
"""
import threading
import unittest
import urllib.request
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent import tools
from app.config import settings
from app.middleware.auth import AuthMiddleware
from app.services import auth_service


class HeaderIdentity(unittest.TestCase):
    def tearDown(self):
        tools._request_token.set(None)

    def test_user_token_is_forwarded_verbatim(self):
        """E05-T01：请求范围内绑定的是**本次用户**的令牌，不是服务级令牌。"""
        handle = tools.set_request_token("user-token-abc")
        try:
            headers = tools._headers()
            self.assertEqual(headers["Authorization"], "Bearer user-token-abc")
            self.assertNotIn(auth_service.internal_token(), headers["Authorization"])
        finally:
            tools.reset_request_token(handle)

    def test_missing_identity_is_refused_not_upgraded(self):
        """E05-T05/T07：没有身份就拒绝，不回退任何服务级身份。"""
        tools._request_token.set(None)
        with patch.object(settings, "AUTH_ENABLED", True):
            with self.assertRaises(tools.AgentIdentityMissing):
                tools._headers()
            r = tools.call_tool("list_customers", {"limit": 1})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "IDENTITY_REQUIRED")
        self.assertFalse(r["retryable"])

    def test_identity_is_request_scoped_across_threads(self):
        """E05-T06：两个线程交错请求不串号。"""
        seen = {}

        def worker(name, token):
            handle = tools.set_request_token(token)
            try:
                seen[name] = tools._headers()["Authorization"]
            finally:
                tools.reset_request_token(handle)

        t1 = threading.Thread(target=worker, args=("a", "token-a"))
        t2 = threading.Thread(target=worker, args=("b", "token-b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(seen["a"], "Bearer token-a")
        self.assertEqual(seen["b"], "Bearer token-b")

    def test_request_is_get_only(self):
        """E05-T08：工具通道只有只读 GET，没有写方法入口。"""
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        def fake_open(req, timeout=None):
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            return _Resp()

        handle = tools.set_request_token("token")
        try:
            with patch.object(tools._opener, "open", side_effect=fake_open):
                tools._get("/api/customers", page_size=1)
        finally:
            tools.reset_request_token(handle)
        self.assertEqual(captured["method"], "GET")
        self.assertTrue(captured["url"].startswith("http://127.0.0.1:8000/api/customers"))


class DestinationLimits(unittest.TestCase):
    def test_external_self_base_is_rejected(self):
        """E05-T10：自调地址只能是受信任的本服务地址。"""
        for base in ("http://evil.example.com:8000", "ftp://127.0.0.1:8000",
                     "http://10.0.0.9:8000"):
            with self.subTest(base=base), patch.dict("os.environ", {"AGENT_SELF_BASE": base}):
                with self.assertRaises(tools.ToolError):
                    tools._base_url()
        with patch.dict("os.environ", {"AGENT_SELF_BASE": "http://127.0.0.1:8000"}):
            self.assertEqual(tools._base_url(), "http://127.0.0.1:8000")

    def test_cross_origin_redirect_never_carries_credentials(self):
        """E05-T10：跨源重定向直接中止。"""
        handler = tools._SameOriginRedirect()
        req = urllib.request.Request("http://127.0.0.1:8000/api/customers",
                                     headers={"Authorization": "Bearer secret"})
        with self.assertRaises(tools.ToolUpstreamError):
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://evil.example.com/steal")


class MiddlewareBoundary(unittest.TestCase):
    def _client(self):
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/api/probe")
        def probe(request):
            state = getattr(request.state, "auth_user", None)
            return {"user": (state or {}).get("username"), "role": (state or {}).get("role")}

        return TestClient(app)

    def test_internal_service_token_is_no_longer_admin(self):
        """E05-T06：内部令牌不再被换成 admin 身份。"""
        client = self._client()
        with patch.object(settings, "AUTH_ENABLED", True), \
                patch.object(auth_service, "is_internal_request", return_value=True):
            resp = client.get("/api/probe", headers={
                "Authorization": f"Bearer {auth_service.internal_token()}"})
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("admin", resp.text)

    def test_default_permissions_unchanged_for_staff_and_viewer(self):
        """E05-T02：默认权限矩阵不变（staff/viewer 仍无 agent:use）。"""
        self.assertFalse(auth_service.has_perm("staff", auth_service.PERM_AGENT_USE))
        self.assertFalse(auth_service.has_perm("viewer", auth_service.PERM_AGENT_USE))
        self.assertTrue(auth_service.has_perm("admin", auth_service.PERM_AGENT_USE))
        self.assertTrue(auth_service.has_perm("manager", auth_service.PERM_AGENT_USE))


if __name__ == "__main__":
    unittest.main()
