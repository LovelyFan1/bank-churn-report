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
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime)                 # 完成时间
