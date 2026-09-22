"""认证端到端测试（打真实 HTTP 接口）。

覆盖：
  1. 策略接口免鉴权
  2. 未登录访问受保护接口 → 401
  3. 单因素登录（manager）
  4. 双因素登录（admin：口令 → 临时票据 → 动态口令）
  5. 临时票据**不能**访问业务接口（关键防线）
  6. 篡改/伪造令牌 → 401
  7. RBAC：viewer 不能建单、不能看审计、不能用助手
  8. 登录失败锁定
  9. 审计日志记录

⚠ 全程只读：建单类只验证被 403 拦下，不真正建单。
"""

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
fails = []


def ok(cond, msg):
    print(f"  [{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


def call(method, path, body=None, token=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


PWD = "Bank@2025"

print("=" * 74)
print("一、策略接口免鉴权")
c, d = call("GET", "/api/auth/policy")
ok(c == 200, f"GET /api/auth/policy 免鉴权可访问（{c}）")
ok("require_totp" in d, f"返回登录策略：require_totp={d.get('require_totp')}")

print("\n二、未登录访问受保护接口 → 401")
for path in ["/api/auth/me", "/api/customers", "/api/agent/capabilities"]:
    c, d = call("GET", path)
    ok(c == 401, f"{path} 未登录被拒（{c}）")

print("\n三、口令错误 → 401，且措辞不泄露用户是否存在")
c1, d1 = call("POST", "/api/auth/login",
              {"username": "liming", "password": "wrong"})
c2, d2 = call("POST", "/api/auth/login",
              {"username": "nobody_xyz", "password": "wrong"})
ok(c1 == 401 and c2 == 401, f"两种情况都是 401（{c1}/{c2}）")
ok(d1.get("detail") == d2.get("detail"),
   f"措辞一致，不可枚举用户名：{d1.get('detail')}")

print("\n四、单因素登录（manager，未绑 TOTP）")
c, d = call("POST", "/api/auth/login",
            {"username": "liming", "password": PWD})
ok(c == 200, f"登录成功（{c}）")
ok(d.get("need_totp") is False, "无需双因素（该账号未绑定）")
mgr_tok = d.get("token")
ok(bool(mgr_tok), "拿到令牌")
ok(d.get("user", {}).get("role") == "manager", "角色为 manager")
ok("order:write" in (d.get("user", {}).get("permissions") or []), "含建单权限")

c, d = call("GET", "/api/auth/me", token=mgr_tok)
ok(c == 200 and d.get("user", {}).get("username") == "liming",
   f"带令牌可访问 /me：{d.get('user', {}).get('display_name')}")

print("\n五、双因素登录（admin，已绑 TOTP）")
c, d = call("POST", "/api/auth/login",
            {"username": "zhaomin", "password": PWD})
ok(c == 200, f"第一步通过（{c}）")
ok(d.get("need_totp") is True, "要求第二步动态口令")
ok("token" not in d, "⚠ 第一步**不返回**正式令牌（防止绕过第二因子）")
ticket = d.get("ticket")
ok(bool(ticket), "拿到临时票据")

print("\n六、临时票据不能访问业务接口（关键防线）")
c, d = call("GET", "/api/auth/me", token=ticket)
ok(c == 401 or c == 403, f"临时票据访问 /me 被拒（{c}）：{str(d.get('detail'))[:40]}")
c, d = call("GET", "/api/customers", token=ticket)
ok(c == 401 or c == 403, f"临时票据访问 /api/customers 被拒（{c}）")

print("\n七、动态口令校验")
c, d = call("POST", "/api/auth/login/totp",
            {"ticket": ticket, "code": "000000"})
ok(c == 401, f"错误动态口令被拒（{c}）")

# 用真实算法算当前口令（模拟 Authenticator App）
from app.services import auth_service as A  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
_db = SessionLocal()
_secret = _db.query(User).filter(User.username == "zhaomin").first().totp_secret
_db.close()
code = A.totp_now(_secret)
print(f"      （用密钥算出当前动态口令：{code}）")
c, d = call("POST", "/api/auth/login/totp", {"ticket": ticket, "code": code})
ok(c == 200, f"正确动态口令登录成功（{c}）")
admin_tok = d.get("token")
ok(bool(admin_tok), "拿到正式令牌")
ok(d.get("user", {}).get("role") == "admin", "角色为 admin")
ok("audit:view" in (d.get("user", {}).get("permissions") or []), "含审计权限")

print("\n八、令牌安全：伪造与篡改")
head, body, sig = mgr_tok.split(".")
forged = f"{head}.{body}.{'A' * len(sig)}"
c, d = call("GET", "/api/auth/me", token=forged)
ok(c == 401, f"伪造签名被拒（{c}）")
c, d = call("GET", "/api/auth/me", token="garbage")
ok(c == 401, f"乱格式令牌被拒（{c}）")

print("\n九、RBAC：viewer 只读")
c, d = call("POST", "/api/auth/login",
            {"username": "chenjie", "password": PWD})
view_tok = d.get("token")
ok(c == 200 and bool(view_tok), "viewer 登录成功")
c, d = call("GET", "/api/customers", token=view_tok)
ok(c == 200, f"viewer 可查客户（{c}）")
c, d = call("POST", "/api/agent/confirm",
            {"action": "create_work_order",
             "payload": {"customer_id": "C071081"}}, token=view_tok)
ok(c == 403, f"⚠ viewer 建单被拒（{c}）：{str(d.get('detail'))[:40]}")
c, d = call("POST", "/api/agent/ask",
            {"question": "现在决策阈值是多少"}, token=view_tok)
ok(c == 403, f"⚠ viewer 用助手被拒（{c}）：{str(d.get('detail'))[:40]}")
c, d = call("GET", "/api/auth/audit", token=view_tok)
ok(c == 403, f"viewer 看审计被拒（{c}）")
c, d = call("GET", "/api/auth/audit", token=mgr_tok)
ok(c == 403, f"manager 看审计也被拒（{c}）")
c, d = call("GET", "/api/auth/audit", token=admin_tok)
ok(c == 200, f"admin 可看审计（{c}，共 {d.get('total')} 条）")

print("\n十、登录失败锁定（用独立账号，避免影响演示账号）")
c, d = call("POST", "/api/auth/login",
            {"username": "wangfang", "password": "wrong"})
for i in range(4):
    c, d = call("POST", "/api/auth/login",
                {"username": "wangfang", "password": "wrong"})
print(f"      连续失败 5 次后状态码={c} 提示={str(d.get('detail'))[:40]}")
ok(c == 423, f"⚠ 第 5 次起账号锁定（{c}）")

# 锁定期间即使口令正确也应被拒（先判锁定再做哈希）
c, d = call("POST", "/api/auth/login",
            {"username": "wangfang", "password": PWD})
ok(c == 423, f"锁定期间正确口令也被拒（{c}）—— 锁定在口令校验之前判定")

print("\n十一、审计日志内容")
c, d = call("GET", "/api/auth/audit?limit=100", token=admin_tok)
items = d.get("items") or []
acts = {i["action"] for i in items}
ok("login" in acts, f"记录了登录：{sorted(acts)}")
fails_logged = [i for i in items if not i["success"]]
ok(len(fails_logged) > 0, f"记录了失败尝试 {len(fails_logged)} 条（审计要能回答'谁试过但失败'）")
# ⚠ 断言必须查**真实的口令值/哈希特征**，不能只搜 "password" 字样 ——
#   action 名 `change_password` 本身就含这个词，早先的宽断言会误报
#   （实测确认：库里 0 条记录含真实口令或 pbkdf2 哈希，是断言写错了）
_leak = [i for i in items
         if "Bank@2025" in json.dumps(i, ensure_ascii=False)
         or "pbkdf2" in json.dumps(i, ensure_ascii=False)]
ok(not _leak, "⚠ 审计中不含真实口令或口令哈希")
lk = [i for i in items if i.get("action") == "login" and i.get("username") == "liming"
      and i["success"]]
ok(len(lk) > 0, "记录了成功的登录者用户名")
if lk:
    print(f"      示例：{lk[0]['display_name']}@{lk[0]['ip']} "
          f"{lk[0]['created_at']} — {lk[0]['message']}")

print("\n十二、改口令")
c, d = call("POST", "/api/auth/change-password",
            {"old_password": "wrongold", "new_password": "NewPass123"},
            token=mgr_tok)
ok(c == 401, f"原口令错误被拒（{c}）")
c, d = call("POST", "/api/auth/change-password",
            {"old_password": PWD, "new_password": "short"}, token=mgr_tok)
ok(c == 422, f"弱口令被拒（{c}）：{str(d.get('detail'))[:40]}")
c, d = call("POST", "/api/auth/change-password",
            {"old_password": PWD, "new_password": PWD}, token=mgr_tok)
ok(c == 422, f"与原口令相同被拒（{c}）：{str(d.get('detail'))[:40]}")

print("\n" + "=" * 74)
print("结论：" + ("PASS —— 认证/授权/审计/双因素全部按预期工作" if not fails
                  else f"FAIL —— {len(fails)} 项未通过"))
for f in fails:
    print("  ×", f)
