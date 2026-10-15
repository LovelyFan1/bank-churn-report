"""二分类正类SHAP形状与贡献加和契约。"""
import numpy as np


def positive_class(model):
    classes = np.asarray(model.classes_)
    matches = np.flatnonzero(classes == 1)
    if len(matches) != 1:
        raise ValueError('模型没有唯一流失正类1')
    return int(matches[0])


def normalize(values, expected_value, model, shape):
    index = positive_class(model)
    if isinstance(values, list):
        values = values[index]
    values = np.asarray(values, dtype=float)
    if values.ndim == 3:
        if values.shape[:2] != shape or values.shape[2] != len(model.classes_):
            raise ValueError('SHAP类别或样本维度不匹配')
        values = values[:, :, index]
    if values.shape != shape or not np.isfinite(values).all():
        raise ValueError('SHAP特征或样本维度不匹配')
    baseline = np.asarray(expected_value, dtype=float).reshape(-1)
    if len(baseline) == len(model.classes_):
        baseline = baseline[index:index + 1]
    if len(baseline) != 1 or not np.isfinite(baseline).all():
        raise ValueError('SHAP基准值格式不支持')
    return values, float(baseline[0])


def explain(model, features, background=None):
    import shap
    features = np.asarray(features)
    if hasattr(model, 'feature_importances_'):
        explainer = shap.TreeExplainer(model)
    elif background is not None:
        explainer = shap.KernelExplainer(model.predict_proba, shap.sample(background, min(100, len(background))))
    else:
        raise ValueError('此模型解释需要背景样本')
    values, baseline = normalize(explainer.shap_values(features), explainer.expected_value,
                                 model, features.shape)
    reconstructed = baseline + values.sum(axis=1)
    positive = positive_class(model)
    probability = model.predict_proba(features)[:, positive]
    if np.allclose(reconstructed, probability, atol=1e-4, rtol=1e-4):
        space = 'probability'
    else:
        if hasattr(model, 'booster_'):
            margin = model.predict(features, raw_score=True)
        elif hasattr(model, 'get_booster'):
            margin = model.predict(features, output_margin=True)
        else:
            raise ValueError('模型解释输出空间不支持')
        if not np.allclose(reconstructed, margin, atol=1e-4, rtol=1e-4):
            raise ValueError('SHAP贡献加和与模型输出不一致')
        space = 'raw_margin'
    return {'values': values, 'base_value': baseline, 'output_space': space,
            'positive_class': 1, 'class_index': positive, 'additivity_checked': True}
