"""客户组合矩阵 — 「概率 → 决策」中间层的对外出口。

此前的系统只有「流失概率 → 风险等级」一个维度，决策退化成「按概率分四档」，
不管客户值 20 万还是 0 都走同一套动作。本路由暴露**价值层 × 风险等级**的
二维视图，让「该做什么」有据可依。

口径：描述性统计（basis=descriptive）—— 描述测试集历史分布，不外推未来收益。
与 /api/cost-benefit/summary（推算值）、/retention-summary（实测值）口径不同，
前端必须分别标注来源。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.cost_benefit_service import get_cost_benefit_service

router = APIRouter(prefix="/api/portfolio", tags=["Portfolio"])


@router.get("/matrix")
async def get_segment_matrix(db: Session = Depends(get_db)):
    """价值层 × 风险等级 矩阵。

    每个格子给出：人数 / 历史流失率 / 平均余额 / 预估可挽回金额 / 建议渠道。
    样本不足（< min_cell_sample）的格子只回人数，统计量置 null ——
    前端须显示「样本不足」而非数字，避免小样本误差被当成结论。
    """
    service = get_cost_benefit_service(db)
    return service.get_segment_matrix()
