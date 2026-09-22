"""认证核心逻辑单测 —— 不依赖网络与数据库，纯函数级验证。

覆盖：口令派生、TOTP（含时钟漂移窗口）、JWT（含篡改与过期）。
"""

import time

from app.services import auth_service as a

fails = []


def ok(cond, msg):
    print(f"  [{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


print("一、口令派生（PBKDF2-SHA256）")
h = a.hash_password("Bank@2026")
ok(h.startswith("pbkdf2_sha256$"), "格式含算法标识与迭代数")
ok(a.verify_password("Bank@2026", h), "正确口令通过")
ok(not a.verify_password("bank@2025", h), "大小写不同应失败")
ok(not a.verify_password("", h), "空口令失败")
ok(not a.verify_password("x", "坏格式"), "损坏哈希失败（不放行）")
h2 = a.hash_password("Bank@2026")
ok(h != h2, "同一口令两次派生结果不同（随机盐生效）")
ok(a.verify_password("Bank@2026", h2), "第二个哈希也能验证")

print("\n二、TOTP（RFC 6238）")
s = a.gen_totp_secret()
ok(len(s) >= 16 and s.isupper() or s.isalnum(), f"密钥为 base32: {s[:12]}…")
code = a.totp_now(s)
ok(len(code) == 6 and code.isdigit(), f"6 位数字口令: {code}")
ok(a.totp_verify(s, code), "当前口令通过")

t = time.time()
ok(a.totp_verify(s, a.totp_now(s, at=t - 30), at=t), "前 1 步窗口内通过（时钟漂移容忍）")
ok(not a.totp_verify(s, a.totp_now(s, at=t - 300), at=t), "5 分钟前的口令拒绝")
ok(not a.totp_verify(s, "000000", at=t) or a.totp_now(s, at=t) == "000000",
   "错误口令拒绝")
ok(not a.totp_verify(s, "abcdef"), "非数字拒绝")
ok(not a.totp_verify("", code), "空密钥拒绝")
# 不同密钥应产出不同口令（极大概率）
other = a.gen_totp_secret()
ok(a.totp_verify(other, code) is False, "换密钥后原口令不通过")

print("\n三、JWT")
tok = a.make_token({"sub": "liming", "role": "manager", "scope": "full"},
                   ttl_minutes=10)
ok(tok.count(".") == 2, "三段式")
p = a.decode_token(tok)
ok(p["sub"] == "liming" and p["role"] == "manager", "解析出正确载荷")
ok("exp" in p and "jti" in p, "含 exp 与 jti")
ok(p["jti"] != a.decode_token(a.make_token({"sub": "x"}))["jti"], "jti 不重复")

# 篡改载荷
head, body, sig = tok.split(".")
import base64, json
forged_body = base64.urlsafe_b64encode(
    json.dumps({"sub": "zhaomin", "role": "admin",
                "exp": int(time.time()) + 9999}).encode()).decode().rstrip("=")
try:
    a.decode_token(f"{head}.{forged_body}.{sig}")
    ok(False, "篡改载荷应被拒绝")
except a.TokenError as e:
    ok(True, f"篡改载荷被拒绝：{e}")

try:
    a.decode_token("not.a.token")
    ok(False, "乱格式应被拒绝")
except a.TokenError as e:
    ok(True, f"乱格式被拒绝：{e}")

try:
    a.decode_token(a.make_token({"sub": "x"}, ttl_minutes=-1))
    ok(False, "过期令牌应被拒绝")
except a.TokenError as e:
    ok(True, f"过期令牌被拒绝：{e}")

print("\n四、授权（RBAC）")
ok(a.has_perm("admin", a.PERM_USER_ADMIN), "admin 可管用户")
ok(not a.has_perm("manager", a.PERM_USER_ADMIN), "manager 不可管用户")
ok(a.has_perm("manager", a.PERM_ORDER_WRITE), "manager 可写工单")
ok(not a.has_perm("viewer", a.PERM_ORDER_WRITE), "viewer 不可写工单")
ok(not a.has_perm("viewer", a.PERM_AGENT_USE), "viewer 不可用助手")
ok(not a.has_perm("", a.PERM_VIEW), "空角色无任何权限")
ok(a.has_perm("admin", a.PERM_AUDIT_VIEW), "admin 可看审计")
ok(not a.has_perm("manager", a.PERM_AUDIT_VIEW), "manager 不可看审计")

print("\n五、登录失败锁定策略")
ok(a.lock_seconds(1) == 0, "第 1 次失败不锁")
ok(a.lock_seconds(4) == 0, "第 4 次失败不锁（容忍手误）")
ok(a.lock_seconds(5) > 0, f"第 5 次开始锁：{a.lock_seconds(5)}s")
ok(a.lock_seconds(6) > a.lock_seconds(5), "锁定时长递增")
ok(a.lock_seconds(99) <= 3600, f"上限 1 小时：{a.lock_seconds(99)}s")

print("\n" + "=" * 70)
print("结论：" + ("PASS —— 认证核心逻辑全部通过" if not fails
                  else f"FAIL —— {len(fails)} 项未通过"))
for f in fails:
    print("  ×", f)
