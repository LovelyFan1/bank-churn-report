"""第二十三轮：核实「决策阈值」在全系统各处的口径是否真的统一。

⚠ 背景：代码注释声称已统一为 decision_threshold=0.20、recall=0.7796。
   但注释不是证据 —— 这条注释是之前某轮改的，必须**重新实测**。
   重点疑点：
     risk_scoring._ensure_engine 用**全量 10 万行**的 raw 概率选阈值，
     而 train.py 是用**训练集**选阈值。两者若不等，则系统里又有两个数。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, json
from sandbox import session
from sqlalchemy import text

print("=" * 92)
print("一、直接调用各接口，抓取所有「召回率 / 阈值」相关数字")
print("=" * 92)
import urllib.request
def get(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__err__": f"{type(e).__name__}: {e}"}

for path in ["/api/model/risk-info", "/api/cost-benefit/summary",
             "/api/model/comparison", "/api/dashboard/summary"]:
    d = get(path)
    print(f"\n  ── {path} ──")
    if "__err__" in d:
        print(f"     ❌ {d['__err__']}")
        continue
    # 递归找所有含 recall/threshold 的键
    found = []
    def walk(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                kk = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    walk(v, kk)
                elif any(s in k.lower() for s in
                         ["recall", "threshold", "precision", "coverage", "f1"]):
                    found.append((kk, v))
        elif isinstance(o, list):
            for i, v in enumerate(o[:5]):
                walk(v, f"{prefix}[{i}]")
    walk(d)
    for k, v in found:
        print(f"     {k:55s} = {v}")

print()
print("=" * 92)
print("二、读 saved_models/meta.json —— 训练时写下的口径")
print("=" * 92)
meta = json.load(open("/app/saved_models/meta.json", encoding="utf-8"))
print(f"  顶层键: {list(meta.keys())}")
if "best_model" in meta:
    print(f"  best_model = {meta['best_model']}")
res = meta.get("results", {})
print(f"\n  各模型指标:")
for name, r in res.items():
    keys = [k for k in r.keys() if not isinstance(r[k], dict)]
    print(f"\n    {name}:")
    for k in keys:
        print(f"      {k:22s} = {r[k]}")
    # 子字典
    for k, v in r.items():
        if isinstance(v, dict):
            print(f"      {k} (dict): {json.dumps(v, ensure_ascii=False)[:300]}")

print()
print("=" * 92)
print("三、实测 risk_scoring 引擎自己算出的决策阈值")
print("=" * 92)
from app.services import risk_scoring as rs
from app.database import SessionLocal
db = SessionLocal()
eng = rs._ensure_engine(db)
if eng is None:
    print("  ⚠ 引擎返回 None（模型未就绪）")
else:
    print(f"  model name            = {eng['name']}")
    print(f"  thresholds            = {eng['thresholds']}")
    print(f"  optimal_threshold     = {eng['optimal_threshold']}")
    print(f"  decision_threshold    = {eng['decision_threshold']}")
    print(f"  decision_coverage     = {eng['decision_coverage']}")
    raw = eng["raw"]; df = eng["df"]
    y = df["exited"].values
    print(f"  raw.shape = {raw.shape}")
    print(f"  全量 (raw >= dt) 占比 = {(raw >= eng['decision_threshold']).mean():.4f}")

    # 在全量上算 recall/precision（这是 Dashboard 可能用的口径）
    dt = eng["decision_threshold"]
    pred = (raw >= dt).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    print(f"\n  若在【全量 10 万行】上评估阈值 {dt}:")
    print(f"    TP={tp} FP={fp} FN={fn}")
    print(f"    recall    = {tp/(tp+fn):.4f}")
    print(f"    precision = {tp/(tp+fp):.4f}")

    # 在训练集上选出来的阈值
    from sklearn.model_selection import train_test_split
    from app.config import settings
    idx = np.arange(len(df))
    train_idx, test_idx = train_test_split(
        idx, test_size=settings.TEST_SIZE,
        random_state=settings.RANDOM_STATE, stratify=y)
    t_train = rs._optimal_threshold(raw[train_idx], y[train_idx])
    t_test = rs._optimal_threshold(raw[test_idx], y[test_idx])
    t_full = rs._optimal_threshold(raw, y)
    print(f"\n  三个口径分别选出的最优阈值:")
    print(f"    训练集选 → {t_train}")
    print(f"    测试集选 → {t_test}")
    print(f"    全量选   → {t_full}")
    print(f"    引擎实际用的 decision_threshold = {dt}")
    print(f"    → 引擎用的是{'全量' if dt == t_full else ('训练集' if dt == t_train else '未知')}口径")

db.close()
