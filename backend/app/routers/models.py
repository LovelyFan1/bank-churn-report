"""模型路由 — 异步任务提交 + 结果查询。

重型计算 (训练/SHAP/批量预测) → Celery 异步任务 → 轮询结果
轻量查询 (对比/ROC/特征重要性) → 直接读取磁盘/DB
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import risk_scoring
from app.services.model_service import get_model_service
from app.celery_tasks.train import train_all_models_task
from app.celery_tasks.predict import batch_score_task

router = APIRouter(prefix="/api/model", tags=["Models"])


# ═══════════════════════════════════════════════════════════
# 异步任务提交（重算力）
# ═══════════════════════════════════════════════════════════

@router.post("/train")
async def train_models():
    """提交训练任务 → 返回 task_id，前端轮询 GET /api/tasks/{task_id}。"""
    task = train_all_models_task.delay()
    return {
        "task_id": task.id,
        "status": "pending",
        "message": f"训练任务已提交，请轮询 GET /api/tasks/{task.id} 查看进度",
    }


@router.get("/shap-global")
async def get_shap_global(db: Session = Depends(get_db)):
    """全局 SHAP 特征重要性 — 从磁盘读取（训练时已自动计算）。"""
    service = get_model_service(db)
    return service.get_shap_global()


@router.get("/batch-score")
async def batch_score(top_n: int = Query(default=100, ge=1, le=1000)):
    """提交批量预测 → 返回 task_id。"""
    task = batch_score_task.delay(top_n=top_n)
    return {
        "task_id": task.id,
        "status": "pending",
        "message": f"批量预测已提交 (Top {top_n})，请轮询 GET /api/tasks/{task.id} 查看进度",
    }


# ═══════════════════════════════════════════════════════════
# 同步读取（轻量 — 直接读磁盘/DB）
# ═══════════════════════════════════════════════════════════

@router.get("/risk-info")
async def get_risk_info(db: Session = Depends(get_db)):
    """当前风险分级标准 — 阈值 / 最优阈值 / 成本比。

    这是全系统分级口径的**唯一对外出口**：风险等级由 risk_scoring 的分位数
    边界（P95/P70/P35）决定，前端各页面必须读这里的值，不得自行写死阈值。

    ⚠ 这里显式传 db 并调用 _ensure_engine，而不是只读缓存：
      该接口此前没有 db 依赖，于是**永远不会**触发引擎构建，
      只能依赖 main.py 启动时的预热线程。一旦预热失败（或该 worker 未被
      覆盖），它会永久返回冷路径的占位阈值（旧实现是写死的 0.7/0.3/0.1，
      与实测的 0.6796/0.2475/0.0682 相差最多 10 倍），而前端会把它当
      真实分级标准展示。改为主动构建后，多 worker 下每个 worker 首次
      请求都会把引擎准备好，不存在"永久冷"的 worker。
    """
    risk_scoring._ensure_engine(db)      # 确保缓存就绪（幂等，命中缓存时零开销）
    return risk_scoring.get_risk_info()


@router.get("/comparison")
async def get_model_comparison(db: Session = Depends(get_db)):
    """模型对比结果 — 从磁盘读取。"""
    service = get_model_service(db)
    return service.get_model_comparison()


@router.get("/roc-curves")
async def get_roc_curves(db: Session = Depends(get_db)):
    """ROC 曲线数据 — 从磁盘读取。"""
    service = get_model_service(db)
    return service.get_roc_curves()


@router.get("/feature-importance")
async def get_feature_importance(db: Session = Depends(get_db)):
    """特征重要性 — 从磁盘读取。"""
    service = get_model_service(db)
    return service.get_feature_importance()


@router.get("/confusion-matrices")
async def get_confusion_matrices(db: Session = Depends(get_db)):
    """混淆矩阵 — 从磁盘读取。"""
    service = get_model_service(db)
    return service.get_confusion_matrices()


@router.post("/predict")
async def predict_single(customer_data: dict, db: Session = Depends(get_db)):
    """单客户预测 — 同步（加载模型 + 推理 = 毫秒级）。"""
    service = get_model_service(db)
    return service.predict_single(customer_data)


@router.post("/shap-single")
async def get_shap_single(customer_data: dict, db: Session = Depends(get_db)):
    """单客户 SHAP 解释 — 同步。"""
    service = get_model_service(db)
    return service.get_shap_single(customer_data)
