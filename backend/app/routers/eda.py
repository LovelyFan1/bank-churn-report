from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.eda_service import get_eda_service

router = APIRouter(prefix="/api/eda", tags=["EDA"])


@router.get("/comprehensive")
async def get_comprehensive_eda(db: Session = Depends(get_db)):
    """综合EDA分析"""
    service = get_eda_service(db)
    return service.get_comprehensive_eda()


@router.get("/correlation")
async def get_correlation_matrix(db: Session = Depends(get_db)):
    """相关性矩阵"""
    service = get_eda_service(db)
    return service.get_correlation_matrix()


@router.get("/churn-analysis")
async def get_churn_analysis(db: Session = Depends(get_db)):
    """流失分析：流失客户 vs 留存客户对比"""
    service = get_eda_service(db)
    return service.get_churn_analysis()


@router.get("/churn-by/{feature}")
async def get_churn_by_category(feature: str, db: Session = Depends(get_db)):
    """按分类特征统计流失率"""
    service = get_eda_service(db)
    return service.get_churn_by_category(feature)


@router.get("/age-distribution")
async def get_age_distribution(db: Session = Depends(get_db)):
    """年龄分布分析"""
    service = get_eda_service(db)
    return service.get_age_distribution()


@router.get("/product-overload")
async def get_product_overload_effect(db: Session = Depends(get_db)):
    """产品过载效应分析"""
    service = get_eda_service(db)
    return service.get_product_overload_effect()


@router.get("/distribution/{feature}")
async def get_numeric_distribution(feature: str, db: Session = Depends(get_db)):
    """数值特征分布"""
    service = get_eda_service(db)
    return service.get_numeric_distribution(feature)
