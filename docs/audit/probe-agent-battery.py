"""Agent 问答能力探针 —— 一次打一批问题，看清真实短板。

只读：只用 /api/agent/ask（POST 但无副作用），不调 confirm。
运行于宿主：python docs/audit/probe-agent-battery.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def _post(path, payload, token=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def login():
    # demo TOTP 端点直接给当前口令
    d = _post("/api/auth/login", {"username": "zhaomin", "password": "Bank@2026"})
    if d.get("need_totp"):
        # ⚠ demo/totp 只收 ticket（见 DemoTotpRequest 的注释），
        #   传 username 会 422 —— 实测踩过
        t = _post("/api/auth/demo/totp", {"ticket": d["ticket"]})
        d2 = _post("/api/auth/login/totp", {"ticket": d["ticket"], "code": t["code"]})
        # ⚠ 响应字段是 token（不是 access_token），见 auth._issue
        return d2["token"]
    return d["token"]


QUESTIONS = [
    # ── A. 单实体深挖 ──
    "C034525 为什么风险这么高？",
    "这个客户值不值得打电话？",              # 指代，靠 context 解析
    "他有几张单",                            # 再指代
    # ── B. 排序 / 对比 ──
    "余额最高的3个客户是谁",
    "风险概率排前十的客户",
    # ── C. 聚合 / 分组（本次重点怀疑对象）──
    "高危客户一共多少人",
    "各地区的高危人数对比",
    "价值层是怎么分布的",
    "按年龄段看，哪个年龄段流失率最高",
    "产品数量和流失率是什么关系",
    # ── D. 解释类（口径）──
    "为什么决策线是 0.6 而不是 0.5",
    "期望挽回到底怎么算出来的",
    # ── E. 工单 ──
    "有哪些工单待处理",
    "把待处理的工单列出来给我",
    # ── F. 模糊 / 复合 ──
    "帮我看看最近有什么值得关注的",
    "如果我只有10个电话，应该打给谁",
    "这批客户应该分别走什么渠道",
    # ── G. 边界 / 寒暄 ──
    "你好",
    "谢谢",
    "你能做什么",
]


def main():
    token = login()
    print("logged in, token len", len(token))
    out = []
    # ⚠ 必须像前端那样回传 context，否则「这个客户」这类指代**必然**失败 ——
    #   那是探针没做对，不是系统缺陷。第一版探针没回传，得出错误结论。
    ctx = None
    for q in QUESTIONS:
        t0 = time.time()
        try:
            payload = {"question": q, "session_id": "probe"}
            if ctx:
                payload["context"] = ctx
            r = _post("/api/agent/ask", payload, token)
            ctx = r.get("context") or ctx
            dt = time.time() - t0
            ans = r.get("answer") or {}
            out.append({
                "q": q,
                "sec": round(dt, 2),
                # ⚠ 响应字段名是 basis（不是 answer_basis）——本轮探针第一版读错，
                #   导致 18 条全部显示 None，看着像系统没出口径
                "basis": r.get("basis"),
                "verify_failed": r.get("verify_failed"),
                "kind": ans.get("kind"),
                "headline": ans.get("headline"),
                "insights": ans.get("insights"),
                "text": (ans.get("text") or "")[:300],
                "warning": (ans.get("warning") or {}).get("text") if ans.get("warning") else None,
                "entities": len(ans.get("entities") or []),
                "facts": len(ans.get("facts") or []),
                # 工具名 + 实参 —— 判断"答非所问"必须看它到底查了什么
                "tools": [{"n": c.get("name"), "a": c.get("args")}
                          for c in (r.get("tool_calls") or [])],
            })
        except Exception as e:
            out.append({"q": q, "error": f"{type(e).__name__}: {e}"})
        print(f"  [{out[-1].get('sec','-')}s] {out[-1].get('basis')} | {q}")
    with open("docs/audit/probe-agent-battery.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved docs/audit/probe-agent-battery.json")


if __name__ == "__main__":
    main()
