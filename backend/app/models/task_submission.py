from sqlalchemy import Column, String, Text, Float, UniqueConstraint
from app.database import Base


class TaskSubmission(Base):
    __tablename__ = 'task_submissions'
    __table_args__ = (UniqueConstraint('username', 'idempotency_key', name='uq_user_task_key'),)
    task_id = Column(String(64), primary_key=True)
    username = Column(String, nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    kind = Column(String(32), nullable=False)
    input_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    model_version = Column(String)
    phase = Column(String(16), nullable=False, default='created')
    lease_until = Column(Float, nullable=False, default=0)
    result_json = Column(Text)
