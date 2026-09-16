from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(String, index=True)       # 客户编号 C000142
    customer_name = Column(String)                  # 客户姓名
    geography = Column(String)                      # 地区
    risk_level = Column(String)                     # CRITICAL / HIGH / MEDIUM / LOW
    probability = Column(Float)                     # 流失概率
    balance = Column(Float)                         # 客户余额
    risk_factors = Column(Text)                     # 风险因素 JSON 字符串
    strategy = Column(String)                       # 建议策略
    status = Column(String, default="pending")      # pending / in_progress / completed / lost
    result = Column(String)                         # retained / lost / null
    assignee = Column(String)                       # 负责人
    note = Column(Text)                             # 备注

    # ── 分级依据快照 ─────────────────────────────────
    # 风险等级由**分位数边界**（P95/P70/P35）判定，而这些边界是在当时全量
    # 概率上算出来的，会随模型重训整体位移。工单里的 risk_level 一经写入即冻结，
    # 若不留存依据，事后无法回答「这个高危当年是按什么标准判的」，
    # 也会出现「工单说高危、客户列表说低危」而无法解释的情况。
    thresholds_snapshot = Column(Text)              # {"critical":0.99,"high":0.16,"medium":0.09}
    model_used = Column(String)                     # 产出该等级的模型名，如 LightGBM

    # ── 价值层快照 ───────────────────────────────────
    # 与 risk_level 同理：价值层由 VALUE_TIER_HIGH 这个**业务假设值**划分，
    # 该假设被调整后，同一客户的历史工单会显示成另一层，事后无法复现当时的
    # 决策依据。故一并冻结。
    value_tier_snapshot = Column(String)            # HIGH / LOW / ZERO
    expected_value_snapshot = Column(Float)         # 建单时的 概率 × 余额（元）

    # ── 渠道与覆盖留痕 ───────────────────────────────
    # 渠道由价值层硬定（零余额 → 自动化触达），但允许人工覆盖以处理例外
    # （如余额为 0 但资产在他行的客户）。覆盖必须留下理由，否则事后无法解释
    # 「为什么给一个零余额客户派了客户经理」。
    channel = Column(String)                        # relationship / outbound / automated
    channel_overridden = Column(Integer, default=0)  # 1 = 人工改过推荐渠道
    override_reason = Column(Text)                  # 覆盖原因（覆盖时必填）

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime)                 # 完成时间
