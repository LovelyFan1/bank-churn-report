"""唯一模型发布清单：显式新版本与legacy路径，禁止缺文件时回读旧模型。"""
import json
from dataclasses import dataclass
from pathlib import Path


def select_model(meta):
    results = meta.get('results')
    if not isinstance(results, dict) or not results:
        raise ValueError('模型清单缺少评估结果')
    name = meta.get('best_model')
    if name is None:
        name = max(results, key=lambda key: results[key].get('cv_auc_mean', results[key].get('auc', -1)))
    if name not in results:
        raise ValueError('启用模型不在发布清单中')
    return name


def artifact(meta, name, root):
    root = Path(root).resolve()
    published = 'version' in meta or 'model_files' in meta
    if published:
        if not meta.get('version') or not isinstance(meta.get('model_files'), dict):
            raise ValueError('新版本清单不完整')
        relative = meta['model_files'].get(name)
        if not relative:
            raise ValueError('发布清单缺少模型文件映射')
    else:
        relative = name.lower().replace(' ', '_') + '.joblib'
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('模型文件超出发布目录')
    if not path.is_file():
        raise ValueError('发布模型文件缺失')
    return path


def revision(root):
    root = Path(root)
    try:
        marker = (root / '.model_version').read_text(encoding='utf-8').strip()
    except OSError:
        marker = ''
    try:
        stamp = str((root / 'meta.json').stat().st_mtime_ns)
    except OSError:
        stamp = '0'
    return marker + ':' + stamp


@dataclass(frozen=True)
class Manifest:
    meta: dict
    best_model: str
    path: Path
    revision: str
    status: str


def resolve(root, meta=None):
    root = Path(root)
    before = revision(root)
    if meta is None:
        meta = json.loads((root / 'meta.json').read_text(encoding='utf-8'))
    if not isinstance(meta, dict):
        raise ValueError('模型清单格式错误')
    name = select_model(meta)
    published = 'version' in meta or 'model_files' in meta
    # 一份发布必须是完整集合，不能让不同评分路径看到不同完整程度。
    if published:
        for model_name in meta['results']:
            artifact(meta, model_name, root)
    path = artifact(meta, name, root)
    if meta.get('feature_names'):
        from app.services.data_loader import FEATURE_NAMES
        if meta['feature_names'] != FEATURE_NAMES:
            raise ValueError('模型输入特征schema不兼容')
    if meta is not None and before != revision(root):
        raise ValueError('模型发布正在切换，请重试')
    return Manifest(meta, name, path, before, 'published' if published else 'legacy')
