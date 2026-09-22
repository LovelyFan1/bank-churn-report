"""核查：引入 LangGraph 是否会动到已 pin 死的核心依赖。

本项目的 requirements.txt 把关键版本全部钉死：
    fastapi==0.115.0  sqlalchemy==2.0.35  pydantic-settings==2.5.0
    pandas==2.2.2     numpy==1.26.4       scikit-learn==1.5.2
    lightgbm==4.5.0   xgboost==2.1.1      celery==5.4.0

若 LangGraph 会升级其中任何一个，风险就不只是"多 32 个包" ——
而是可能动摇整个训练与推理链路。故必须逐项核对。

方法：pip --dry-run 的 report 里，凡已安装且满足要求的包**不会出现**。
      故只需检查核心包是否出现在 install 列表中，以及版本是否变化。
"""
import json
import sys

report_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rep.json"
rep = json.load(open(report_path, encoding="utf-8"))

installs = {}
for item in rep.get("install", []):
    md = item["metadata"]
    installs[md["name"].lower()] = md["version"]

CORE = [
    "fastapi", "starlette", "uvicorn", "sqlalchemy", "pydantic",
    "pydantic-settings", "pandas", "numpy", "scipy",
    "scikit-learn", "lightgbm", "xgboost", "shap", "joblib",
    "celery", "redis", "python-dateutil", "pytz",
]

print("=" * 76)
print("一、LangGraph 方案会新装/变更哪些包")
print("=" * 76)
print(f"  共 {len(installs)} 个")
print()

print("=" * 76)
print("二、核心依赖是否被触碰（决定风险等级）")
print("=" * 76)
touched = []
for name in CORE:
    if name in installs:
        touched.append((name, installs[name]))
        print(f"  [会被变更] {name:<20} → {installs[name]}")
    else:
        print(f"  [不受影响] {name}")

print()
print("=" * 76)
print("三、结论")
print("=" * 76)
if touched:
    print(f"  ⚠ 有 {len(touched)} 个核心依赖会被变更，需评估影响：")
    for n, v in touched:
        print(f"      {n} → {v}")
else:
    print("  ✔ 全部核心依赖（含 fastapi/sqlalchemy/pydantic/numpy/pandas/")
    print("    scikit-learn/lightgbm/xgboost/celery）**均不受影响**。")
    print("    即 LangGraph 只做纯增量安装，不触碰训练与推理链路。")
