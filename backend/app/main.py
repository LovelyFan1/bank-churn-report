from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import threading

from app.config import settings
from app.database import engine, Base, get_db
from app.models.customer import Customer
from app.models.work_order import WorkOrder
from app.services.data_generator import get_data_generator
from app.routers import eda, clustering, models, cost_benefit, tasks, work_orders, customers, portfolio

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    debug=settings.DEBUG,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(tasks.router)         # /api/tasks/{task_id} — 任务状态查询
app.include_router(eda.router)           # /api/eda/*
app.include_router(clustering.router)    # /api/cluster/*
app.include_router(models.router)        # /api/model/*
app.include_router(cost_benefit.router)  # /api/cost-benefit/*
app.include_router(work_orders.router)  # /api/work-orders/*
app.include_router(customers.router)   # /api/customers/*
app.include_router(portfolio.router)   # /api/portfolio/* — 价值层 × 风险等级矩阵

# 允许查询的字段列表
ALLOWED_FIELDS = [
    "credit_score", "geography", "gender", "age", "tenure",
    "balance", "num_products", "has_credit_card", "is_active_member",
    "estimated_salary", "exited", "complain", "satisfaction_score",
    "card_type", "points_earned", "age_group",
]


@app.on_event("startup")
async def startup_event():
    """启动时建表 + 种子数据生成（仅首次）。"""
    Base.metadata.create_all(bind=engine)

    db = next(get_db())
    try:
        count = db.query(Customer).count()
        if count == 0:
            generator = get_data_generator()
            df = generator.generate()
            generator.save_to_db(db, df)
            print(f"Generated {len(df)} customer records")
    finally:
        db.close()

    # 后台预热风险评分引擎（避免首次请求等待全量打分）
    def _warmup_risk_scoring():
        from app.database import SessionLocal
        from app.services import risk_scoring
        wdb = SessionLocal()
        try:
            risk_scoring.get_scored_customers(wdb)
            print("[startup] risk scoring engine warmed up")
        except Exception as e:
            print(f"[startup] risk scoring warmup failed: {e}")
        finally:
            wdb.close()

    threading.Thread(target=_warmup_risk_scoring, daemon=True).start()


@app.get("/")
async def root():
    return {"message": settings.APP_NAME, "version": settings.VERSION}


@app.get("/api/data/overview")
async def get_data_overview():
    db = next(get_db())
    try:
        total = db.query(Customer).count()
        churned = db.query(Customer).filter(Customer.exited == 1).count()
        retained = db.query(Customer).filter(Customer.exited == 0).count()

        avg_age = db.query(func.avg(Customer.age)).scalar()
        avg_balance = db.query(func.avg(Customer.balance)).scalar()
        avg_salary = db.query(func.avg(Customer.estimated_salary)).scalar()

        return {
            "total_customers": total,
            "churned_customers": churned,
            "retained_customers": retained,
            "churn_rate": round(churned / total * 100, 2) if total else 0,
            "avg_age": round(avg_age, 1) if avg_age else 0,
            "avg_balance": round(avg_balance, 2) if avg_balance else 0,
            "avg_salary": round(avg_salary, 2) if avg_salary else 0,
        }
    finally:
        db.close()


@app.get("/api/data/distribution/{field}")
async def get_field_distribution(field: str):
    if field not in ALLOWED_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid field: {field}. Allowed fields: {ALLOWED_FIELDS}",
        )

    db = next(get_db())
    try:
        results = (
            db.query(getattr(Customer, field), func.count(Customer.id))
            .group_by(getattr(Customer, field))
            .all()
        )
        return {
            "field": field,
            "data": [{"value": str(r[0]), "count": r[1]} for r in results],
        }
    finally:
        db.close()
