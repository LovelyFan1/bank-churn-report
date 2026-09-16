from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.cost_benefit_service import get_cost_benefit_service

router = APIRouter(prefix="/api/cost-benefit", tags=["CostBenefit"])


@router.get("/thresholds")
async def get_threshold_analysis(
    cost_ratio: float = Query(default=None, description="漏检成本/误报成本比值，缺省取 settings.COST_RATIO"),
    db: Session = Depends(get_db)
):
    """不同阈值下的成本收益分析"""
    service = get_cost_benefit_service(db)
    return service.analyze_thresholds(cost_ratio=cost_ratio)


@router.get("/summary")
async def get_business_summary(db: Session = Depends(get_db)):
    """业务摘要 - 年化成本收益（**推算值**：模型测试集指标 × 假设客单价）"""
    service = get_cost_benefit_service(db)
    return service.get_business_summary()


@router.get("/retention-summary")
async def get_retention_summary(db: Session = Depends(get_db)):
    """挽留效果复盘（**实测值**：来自 work_orders 的真实处理结果）。

    与 /summary 口径不同：/summary 回答「模型理论上能省多少」，
    本接口回答「实际执行后挽留了多少」。工单表为空时返回 has_data=false，
    前端据此显示空态 —— 不得用推算值顶替。
    """
    service = get_cost_benefit_service(db)
    return service.get_retention_summary()
