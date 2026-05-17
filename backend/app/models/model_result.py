from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class ModelResult(Base):
    __tablename__ = "model_results"

    id = Column(Integer, primary_key=True, index=True)
    model_name = Column(String)
    auc = Column(Float)
    accuracy = Column(Float)
    precision_score = Column(Float)
    recall = Column(Float)
    f1_score = Column(Float)
    training_time = Column(Float)
    roc_curve_data = Column(String)
    feature_importance = Column(String)
