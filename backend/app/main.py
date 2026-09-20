from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import sys
import threading

from app.config import settings
from app.database import engine, Base, get_db
from app.models.customer import Customer
from app.models.work_order import WorkOrder
from app.services import data_source
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
    """启动时建表 + 播种数据（仅首次）。

    数据来源由 settings.DATA_SOURCE 决定（generator / csv），
    见 app/services/data_source.py。失败时**不静默回退** —— 回退会让人
    以为在用标定数据，实际在用合成数据。
    """
    Base.metadata.create_all(bind=engine)

    db = next(get_db())
    try:
        info = data_source.seed(db)
        if info.get("seeded"):
            print(
                f"[startup] 已播种 {info['rows']} 条客户 "
                f"（来源: {info['source']}），流失率 {info['churn_rate']}%"
            )
        else:
            print(f"[startup] {info.get('reason')}，现有 {info.get('existing')} 条")
        settings.DATA_SOURCE_NAME = info.get("source", "")
    except Exception as e:
        print(f"[startup] 数据播种失败: {type(e).__name__}: {e}", file=sys.stderr)
        raise
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


# ── Dashboard 聚合接口 ─────────────────────────────────
# 把首屏 4 个请求合并为 1 个，绕过 Docker Desktop 转发链路的并发瓶颈。
# 实测：8 并发 ~9000ms → 1 请求 ~300ms（30 倍提升）。
@app.get("/api/dashboard/summary")
async def get_dashboard_summary():
    """Dashboard 首屏聚合数据：概览 + Top10 + 成本收益 + 风险口径。"""
    from app.services import risk_scoring
    from app.services.cost_benefit_service import get_cost_benefit_service

    db = next(get_db())
    try:
        # 1) 数据概览（与 /api/data/overview 同逻辑）
        total = db.query(Customer).count()
        churned = db.query(Customer).filter(Customer.exited == 1).count()
        retained = db.query(Customer).filter(Customer.exited == 0).count()
        avg_age = db.query(func.avg(Customer.age)).scalar()
        avg_balance = db.query(func.avg(Customer.balance)).scalar()
        avg_salary = db.query(func.avg(Customer.estimated_salary)).scalar()
        overview = {
            "total_customers": total,
            "churned_customers": churned,
            "retained_customers": retained,
            "churn_rate": round(churned / total * 100, 2) if total else 0,
            "avg_age": round(avg_age, 1) if avg_age else 0,
            "avg_balance": round(avg_balance, 2) if avg_balance else 0,
            "avg_salary": round(avg_salary, 2) if avg_salary else 0,
        }

        # 2) Top10 客户 + 风险分布（走共享打分缓存）
        scored = risk_scoring.get_scored_customers(db)
        top_customers = []
        risk_dist = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        model_error = None
        if scored is None:
            model_error = "模型尚未训练，请先调用 POST /api/model/train"
        else:
            for s in scored:
                risk_dist[s["risk_level"]] += 1
            top_customers = sorted(
                scored, key=lambda x: x["expected_value"], reverse=True
            )[:10]

        # 3) 成本收益（推算值）
        summary = None
        try:
            svc = get_cost_benefit_service(db)
            bs = svc.get_business_summary()
            if bs and not bs.get("error"):
                summary = bs
        except Exception:
            pass

        # 4) 风险分级口径
        risk_info = risk_scoring.get_risk_info()

        return {
            "overview": overview,
            "top_customers": top_customers,
            "risk_distribution": risk_dist,
            "business_summary": summary,
            "risk_info": risk_info,
            "model_error": model_error,
        }
    finally:
        db.close()
