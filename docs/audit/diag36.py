"""第三十六轮：客户详情字段缺失对单条预测的影响量化。只读。

接口 A: GET  /api/customers/{id}       → 24 个键（少 has_credit_card / points_earned）
接口 B: POST /api/model/predict        → 需要 12 个特征

若前端把 A 的响应直接喂给 B，缺失字段会走 .get(...,默认值)：
    has_credit_card 默认 1（该客户真实值可能是 0）
    points_earned   默认 500（真实值 100~1000）
→ 单条预测的概率与全量打分结果**不一致**，即"点进去看到的概率和列表里不一样"。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import json, urllib.request
import numpy as np

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

def post(path, body):
    req = urllib.request.Request(
        "http://127.0.0.1:8000" + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

from app.database import SessionLocal
from app.models.customer import Customer
from app.services.data_loader import prepare_features, get_cached_customer_df
from app.services import risk_scoring as rs
import joblib
from pathlib import Path

db = SessionLocal()
df = get_cached_customer_df(db)
X, y, _ = prepare_features(df)
eng = rs._ensure_engine(db)
raw = eng["raw"]
model = joblib.load(Path("/app/saved_models") / "xgboost.joblib")

print("=" * 92)
print("一、对比：详情接口字段 vs 预测接口所需字段")
print("=" * 92)
sample_ids = ["C000001", "C050001", "C071081", "C100000"]
d0 = get(f"/api/customers/{sample_ids[0]}")
have = set(d0.keys())
need = ["credit_score","age","tenure","balance","num_products","has_credit_card",
        "is_active_member","estimated_salary","satisfaction_score","points_earned",
        "geography","gender"]
print(f"  详情接口有的字段: {len(have)} 个")
print(f"  预测需要: {len(need)} 个")
print(f"  ❌ 缺失: {[f for f in need if f not in have]}")

print()
print("=" * 92)
print("二、量化：把详情响应直接喂给 predict 的影响")
print("=" * 92)
print(f"  {'客户':>10} {'列表概率':>10} {'详情喂入概率':>13} {'差异':>10} {'风险等级':>10}")
rows = []
for cid in sample_ids:
    d = get(f"/api/customers/{cid}")
    # 列表/引擎里的概率
    idx = df.index[df["customer_id"] == cid]
    if len(idx) == 0:
        continue
    i = int(idx[0])
    p_engine = float(raw[i])
    # 用详情响应调 predict（模拟前端行为）
    body = {k: d.get(k) for k in need}
    # 补上拿不到的字段（前端可能会这么干，也可能不补 → 用后端默认值）
    r1 = post("/api/model/predict", body)
    p_detail = r1.get("probability")
    rows.append((cid, p_engine, p_detail))
    print(f"  {cid:>10} {p_engine:>10.4f} {p_detail:>13.4f} "
          f"{abs(p_engine-p_detail):>10.4f} {r1.get('risk_level'):>10}")

print()
print("=" * 92)
print("三、真实客户的具体取值（看默认值偏离多少）")
print("=" * 92)
for cid in sample_ids[:3]:
    c = db.query(Customer).filter(Customer.customer_id == cid).first()
    if c:
        print(f"  {cid}: has_credit_card={c.has_credit_card} (默认填 1 → "
              f"{'一致' if c.has_credit_card==1 else '❌ 不一致'}), "
              f"points_earned={c.points_earned} (默认填 500 → "
              f"{'一致' if c.points_earned==500 else '❌ 不一致'})")

print()
print("=" * 92)
print("四、全量统计：这两个字段的分布，估算影响面")
print("=" * 92)
print(f"  has_credit_card: 取值分布 = "
      f"{dict(df['has_credit_card'].value_counts().sort_index())}")
print(f"    → 若默认填 1，则 {int((df['has_credit_card']==0).sum())} 个客户被填错 "
      f"({(df['has_credit_card']==0).mean()*100:.1f}%)")
print(f"  points_earned: min={df['points_earned'].min()} max={df['points_earned'].max()} "
      f"mean={df['points_earned'].mean():.1f}")
print(f"    → 默认填 500，与均值的差 = {abs(500 - df['points_earned'].mean()):.1f}")
print(f"    → 恰好等于 500 的客户数 = {int((df['points_earned']==500).sum())} "
      f"({(df['points_earned']==500).mean()*100:.2f}%)")

# 这两个特征在模型里的重要性
meta = json.load(open("/app/saved_models/meta.json", encoding="utf-8"))
fi = meta["results"]["XGBoost"]["feature_importance"]
print()
print(f"  模型特征重要性（XGBoost）:")
for f in ["has_credit_card", "points_earned"]:
    print(f"    {f:20s} = {fi.get(f)}")
max_fi = max(fi.values())
print(f"    最大特征重要性 = {max_fi}")
for f in ["has_credit_card", "points_earned"]:
    print(f"    {f} 占最大值的 {fi.get(f,0)/max_fi*100:.3f}%")

print()
print("=" * 92)
print("五、结论")
print("=" * 92)
if rows:
    maxdiff = max(abs(a-b) for _c, a, b in rows)
    print(f"  抽样 {len(rows)} 个客户，最大概率差异 = {maxdiff:.4f}")
print("""
  这是**契约不一致**而非纯性能问题：
    · 详情接口（GET /api/customers/{id}）返回的字段**不足以**喂给预测接口；
    · 若前端把详情响应直接传给 POST /api/model/predict，
      has_credit_card 会填 1（约 29% 客户实际为 0），points_earned 会填 500；
    · 后果：单条预测的概率与列表页/矩阵页**不一致** ——
      正是此前花力气消除的"同一客户多个数字"那类问题。
    · 另外 SHAP 单条解释（get_shap_single）也用同样的取默认值逻辑，
      归因结果同样会被这两个占位值污染。
""")
db.close()
