"""聚类路由 — 异步任务提交 + 结果查询。

重算力 (K-Means/肘部法则) → Celery 异步任务 → 轮询结果
轻量查询 (画像/3D散点) → 直接读取 DB 中的聚类标签
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.clustering_service import get_clustering_service
from app.celery_tasks.cluster import run_kmeans_task, run_elbow_task

router = APIRouter(prefix="/api/cluster", tags=["Clustering"])


# ═══════════════════════════════════════════════════════════
# 异步任务提交（重算力）
# ═══════════════════════════════════════════════════════════

@router.post("/kmeans")
async def submit_kmeans(k: int = Query(default=5, ge=2, le=20), save: bool = Query(default=False)):
    """提交 K-Means 聚类 → 返回 task_id。

    Query params:
        k:    聚类数 (2-20)
        save: 是否将标签写入 DB
    """
    task = run_kmeans_task.delay(n_clusters=k, save_to_db=save)
    return {
        "task_id": task.id,
        "status": "pending",
        "k": k,
        "save_to_db": save,
        "message": f"K-Means (k={k}) 聚类已提交，请轮询 GET /api/tasks/{task.id} 查看进度",
    }


@router.post("/kmeans/save")
async def submit_kmeans_save(k: int = Query(default=5, ge=2, le=20)):
    """K-Means 聚类 + 保存到数据库 → 返回 task_id。"""
    task = run_kmeans_task.delay(n_clusters=k, save_to_db=True)
    return {
        "task_id": task.id,
        "status": "pending",
        "k": k,
        "message": f"K-Means (k={k}) 聚类+保存已提交，请轮询 GET /api/tasks/{task.id} 查看进度",
    }


@router.post("/elbow")
async def submit_elbow():
    """提交肘部法则计算 → 返回 task_id。"""
    task = run_elbow_task.delay()
    return {
        "task_id": task.id,
        "status": "pending",
        "message": f"肘部法则计算已提交，请轮询 GET /api/tasks/{task.id} 查看进度",
    }


# ═══════════════════════════════════════════════════════════
# 同步读取（轻量 — 直接读 DB）
# ═══════════════════════════════════════════════════════════

@router.get("/profiles")
async def get_cluster_profiles(db: Session = Depends(get_db)):
    """聚类画像 — 读取 DB 中的聚类标签。

    ⚠ 名字复用同一份 profiles：此前是
        profiles = service.get_cluster_profiles()
        names = service.get_cluster_names()      # ← 内部又完整算了一遍
    同一份数据被计算两次（每次各含一次全量加载，实测各约 3 秒）。
    现把 profiles 传进去复用，只算一次。
    """
    service = get_clustering_service(db)
    profiles = service.get_cluster_profiles()
    names = service.get_cluster_names(profiles)

    for cluster in profiles.get("clusters", []):
        cluster["name"] = names.get(cluster["cluster_id"], f"Cluster {cluster['cluster_id']}")

    return profiles


@router.get("/3d-scatter")
async def get_3d_scatter_data(db: Session = Depends(get_db)):
    """3D 散点图数据 — 读取 DB 中的聚类标签 + PCA。"""
    service = get_clustering_service(db)
    return service.get_3d_scatter_data()
