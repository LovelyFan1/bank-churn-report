from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.model_service import get_model_service

router = APIRouter(prefix="/api/model", tags=["Models"])


@router.get("/shap-global")
async def get_shap_global(db: Session = Depends(get_db)):
    """全局SHAP特征重要性分析"""
    service = get_model_service(db)
    return service.get_shap_global()


@router.post("/shap-single")
async def get_shap_single(customer_data: dict, db: Session = Depends(get_db)):
    """单客户SHAP预测解释"""
    service = get_model_service(db)
    return service.get_shap_single(customer_data)


@router.post("/train")
async def train_models(db: Session = Depends(get_db)):
    """训练所有模型"""
    service = get_model_service(db)
    results = service.train_all_models()
    return {"message": "Models trained successfully", "results": results}


@router.get("/comparison")
async def get_model_comparison(db: Session = Depends(get_db)):
    """获取模型对比结果"""
    service = get_model_service(db)
    return service.get_model_comparison()


@router.get("/roc-curves")
async def get_roc_curves(db: Session = Depends(get_db)):
    """获取ROC曲线数据"""
    service = get_model_service(db)
    return service.get_roc_curves()


@router.get("/feature-importance")
async def get_feature_importance(db: Session = Depends(get_db)):
    """获取特征重要性"""
    service = get_model_service(db)
    return service.get_feature_importance()


@router.get("/confusion-matrices")
async def get_confusion_matrices(db: Session = Depends(get_db)):
    """获取混淆矩阵"""
    service = get_model_service(db)
    return service.get_confusion_matrices()


@router.post("/predict")
async def predict_single(customer_data: dict, db: Session = Depends(get_db)):
    """单个客户预测"""
    service = get_model_service(db)
    return service.predict_single(customer_data)


@router.get("/batch-score")
async def batch_score(top_n: int = Query(default=100, ge=1, le=1000), db: Session = Depends(get_db)):
    """批量打分所有客户，返回 Top N 高风险客户 + 风险分布"""
    service = get_model_service(db)
    return service.batch_score_all(top_n=top_n)


@router.post("/save")
async def save_models(db: Session = Depends(get_db)):
    """保存模型和结果"""
    service = get_model_service(db)
    service.save_results_to_db()
    return {"message": "Models and results saved successfully"}
