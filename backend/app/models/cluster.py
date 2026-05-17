from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    description = Column(String)
    centroid_features = Column(String)
    customer_count = Column(Integer)
    churn_rate = Column(Float)
