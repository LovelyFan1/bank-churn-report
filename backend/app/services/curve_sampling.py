"""保留端点的 ROC 折线简化；纵向误差上限，不修改模型评估指标。"""
import numpy as np


def simplify(curve, tolerance=.0005):
    x = np.asarray(curve.get('fpr', []), dtype=float)
    y = np.asarray(curve.get('tpr', []), dtype=float)
    if len(x) != len(y) or not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(np.diff(x) < 0):
        raise ValueError('ROC 坐标无效')
    keep = {0, len(x)-1} if len(x) else set()
    stack = [(0,len(x)-1)] if len(x)>2 else []
    while stack:
        start,end = stack.pop()
        if end-start < 2: continue
        if x[end] == x[start]: continue  # 垂直段仍由端点精确表达
        predicted = y[start]+(y[end]-y[start])*(x[start+1:end]-x[start])/(x[end]-x[start])
        delta = np.abs(y[start+1:end]-predicted)
        split = start+1+int(np.argmax(delta))
        if float(delta.max()) > tolerance:
            keep.add(split); stack.extend([(start,split),(split,end)])
    indices = sorted(keep)
    result = {key: [values[i] for i in indices] for key,values in curve.items() if isinstance(values,list) and len(values)==len(x)}
    result['sampling'] = {'original_points':len(x),'plotted_points':len(indices),'max_vertical_error':tolerance,'method':'bounded_polyline'}
    return result
