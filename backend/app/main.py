from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.config import settings
from app.database import engine, Base, get_db
from app.models.customer import Customer
from app.models.cluster import Cluster
from app.models.model_result import ModelResult
from app.services.data_generator import get_data_generator
from app.routers import eda, clustering, models, cost_benefit

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    debug=settings.DEBUG
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(eda.router)
app.include_router(clustering.router)
app.include_router(models.router)
app.include_router(cost_benefit.router)

# 允许查询的字段列表
ALLOWED_FIELDS = [
    "credit_score", "geography", "gender", "age", "tenure",
    "balance", "num_products", "has_credit_card", "is_active_member",
    "estimated_salary", "exited", "complain", "satisfaction_score",
    "card_type", "points_earned", "age_group"
]


@app.on_event("startup")
async def startup_event():
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
            "churn_rate": round(churned / total * 100, 2),
            "avg_age": round(avg_age, 1),
            "avg_balance": round(avg_balance, 2),
            "avg_salary": round(avg_salary, 2)
        }
    finally:
        db.close()


@app.get("/api/data/distribution/{field}")
async def get_field_distribution(field: str):
    # 输入验证
    if field not in ALLOWED_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid field: {field}. Allowed fields: {ALLOWED_FIELDS}"
        )

    db = next(get_db())
    try:
        results = db.query(
            getattr(Customer, field),
            func.count(Customer.id)
        ).group_by(getattr(Customer, field)).all()

        return {
            "field": field,
            "data": [{"value": str(r[0]), "count": r[1]} for r in results]
        }
    finally:
        db.close()
