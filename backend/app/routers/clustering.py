from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.clustering_service import get_clustering_service

router = APIRouter(prefix="/api/cluster", tags=["Clustering"])


@router.get("/kmeans")
async def get_kmeans_clustering(k: int = 5, db: Session = Depends(get_db)):
    """K-Means聚类（不保存到数据库）"""
    service = get_clustering_service(db)
    result = service.fit_kmeans(n_clusters=k)
    return result


@router.post("/kmeans/save")
async def save_kmeans_clustering(k: int = 5, db: Session = Depends(get_db)):
    """K-Means聚类并保存到数据库"""
    service = get_clustering_service(db)
    result = service.fit_kmeans(n_clusters=k)
    service.assign_clusters_to_customers()
    return result


@router.get("/elbow")
async def get_elbow_method(db: Session = Depends(get_db)):
    """肘部法则 - 确定最优K值"""
    service = get_clustering_service(db)
    return service.get_elbow_method()


@router.get("/profiles")
async def get_cluster_profiles(db: Session = Depends(get_db)):
    """获取聚类画像"""
    service = get_clustering_service(db)
    profiles = service.get_cluster_profiles()
    names = service.get_cluster_names()

    # Add names to profiles
    for cluster in profiles["clusters"]:
        cluster["name"] = names.get(cluster["cluster_id"], f"Cluster {cluster['cluster_id']}")

    return profiles


@router.get("/3d-scatter")
async def get_3d_scatter_data(db: Session = Depends(get_db)):
    """获取3D散点图数据"""
    service = get_clustering_service(db)
    return service.get_3d_scatter_data()
