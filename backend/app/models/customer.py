from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    row_number = Column(Integer)
    customer_id = Column(String, unique=True, index=True)
    surname = Column(String)
    credit_score = Column(Integer)
    geography = Column(String)
    gender = Column(String)
    age = Column(Integer)
    tenure = Column(Integer)
    balance = Column(Float)
    num_products = Column(Integer)
    has_credit_card = Column(Integer)
    is_active_member = Column(Integer)
    estimated_salary = Column(Float)
    exited = Column(Integer)
    complain = Column(Integer)
    satisfaction_score = Column(Integer)
    card_type = Column(String)
    points_earned = Column(Integer)

    # Derived features
    age_group = Column(String)
    balance_salary_ratio = Column(Float)
    cluster_id = Column(Integer)
    risk_score = Column(Float)
    risk_level = Column(String)
