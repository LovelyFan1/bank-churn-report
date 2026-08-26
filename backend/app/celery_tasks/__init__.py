"""Celery 异步任务包。

任务按领域拆分:
- train.py   : 模型训练、SHAP、保存
- predict.py : 批量预测、单条预测
- cluster.py : K-Means 聚类、肘部法则
"""
