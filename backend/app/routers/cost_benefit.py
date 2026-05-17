from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.cost_benefit_service import get_cost_benefit_service

router = APIRouter(prefix="/api/cost-benefit", tags=["CostBenefit"])


@router.get("/thresholds")
async def get_threshold_analysis(
    cost_ratio: float = Query(default=5.0, description="漏检成本/误报成本比值"),
    db: Session = Depends(get_db)
):
    """不同阈值下的成本收益分析"""
    service = get_cost_benefit_service(db)
    return service.analyze_thresholds(cost_ratio=cost_ratio)


@router.get("/summary")
async def get_business_summary(db: Session = Depends(get_db)):
    """业务摘要 - 年化成本收益"""
    service = get_cost_benefit_service(db)
    return service.get_business_summary()
