from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import sys
import threading

from app.config import settings
from app.database import engine, Base, get_db
from app.models.customer import Customer
from app.models.user import User
from app.models.work_order import WorkOrder
from app.services import auth_deps, data_source
from app.services.data_generator import get_data_generator
from app.routers import eda, clustering, models, cost_benefit, tasks, work_orders, customers, portfolio
from app.routers import agent as agent_router
from app.routers import auth as auth_router
from app.routers import users as users_router

# 导入模型模块以确保建表时被注册（Base.metadata 只认识已导入的模型）
from app.models import user as _user_model  # noqa: F401

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    debug=settings.DEBUG,
)

# ── 中间件注册顺序（实测查证 Starlette 源码后确定）────────
#
# Starlette 的 `add_middleware` 是 `user_middleware.insert(0, ...)`，
# 即**最后注册的在最外层、最先执行**。
#
# 故这里：先注册鉴权（内层），再注册 CORS（外层）。
# 顺序若反过来，鉴权会先于 CORS 执行 —— 未授权的跨域请求被 401 直接
# 返回，而 CORSMiddleware 根本没机会加上 `Access-Control-Allow-Origin`，
# 浏览器于是拦掉响应，前端只看到笼统的 "Network Error" 而不是 401。
# 那种现象极易被误判成"后端挂了"，排查成本很高。
from app.middleware.auth import AuthMiddleware  # noqa: E402
app.add_middleware(AuthMiddleware)              # 内层：先鉴权

app.add_middleware(                              # 外层：后执行，负责跨域
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
app.include_router(agent_router.router)  # /api/agent/* — 对话式任务型 Agent
app.include_router(auth_router.router)   # /api/auth/* — 内部登录（4A 仿真）
app.include_router(users_router.router)  # /api/users/* — 可指派负责人列表（派单）

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
async def get_dashboard_summary(user: User = Depends(auth_deps.current_user)):
    """Dashboard 首屏聚合数据：概览 + Top10 + 成本收益 + 风险口径。

    ⚠ 脱敏判据是 `privacy.can_view_whole_book`（identify **且** insight:view），
      **不是** `can_identify`。原因：客户专员（staff）有 identify 权限
      （要打电话），但没有全行洞察授权 —— 若用 can_identify，
      staff 会在工作台看到全行 Top10 客户姓名与余额，绕过
      "他只能看派给自己的单"这条数据级限制（名单页对他已关闭）。
      详见 privacy.can_view_whole_book 的说明。
    """
    from app.services import privacy
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

        # 5) 脱敏 Top10（无全行视图权限时替换为匿名条目）
        masked = not privacy.can_view_whole_book(user)
        if masked:
            top_customers = privacy.mask_customers(top_customers)

        return {
            "overview": overview,
            "top_customers": top_customers,
            "risk_distribution": risk_dist,
            "business_summary": summary,
            "risk_info": risk_info,
            "model_error": model_error,
            "masked": masked,
            "mask_notice": privacy.mask_notice_for(user) if masked else "",
        }
    finally:
        db.close()
