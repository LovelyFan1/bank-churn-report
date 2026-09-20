"""聚类 Celery 任务 — 在独立 Worker 进程中执行。

包含:
- run_kmeans_task: K-Means / MiniBatchKMeans 聚类
- run_elbow_task: 肘部法则
"""

import os

import numpy as np
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.customer import Customer
from app.services.data_loader import DataLoader, prepare_cluster_features, CLUSTER_FEATURES
from app.config import settings

from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.metrics import silhouette_score

import json
from pathlib import Path

CLUSTER_DIR = Path(__file__).parent.parent.parent / "saved_models"


def _choose_kmeans(n_clusters: int, n_samples: int):
    """根据数据量自动选择 KMeans 或 MiniBatchKMeans。"""
    if n_samples > 50000:
        return MiniBatchKMeans(
            n_clusters=n_clusters,
            random_state=settings.RANDOM_STATE,
            batch_size=10000,
            n_init=3,
        )
    return KMeans(
        n_clusters=n_clusters,
        random_state=settings.RANDOM_STATE,
        n_init=10,
    )


@celery_app.task(bind=True, name="run_kmeans")
def run_kmeans_task(self, n_clusters: int = 5, save_to_db: bool = False) -> dict:
    """Celery 任务: 执行 K-Means 聚类。

    Args:
        n_clusters: 聚类数
        save_to_db: 是否将聚类标签写回 customers 表

    Returns:
        {status, n_clusters, inertia, silhouette_score, cluster_sizes, ...}
    """
    db = SessionLocal()
    try:
        self.update_state(state="PROGRESS", meta={"step": "loading", "message": "加载数据..."})

        loader = DataLoader(db)
        total = loader.total_count

        self.update_state(state="PROGRESS", meta={
            "step": "preprocessing",
            "message": "特征标准化...",
        })

        # ── 用前几个 chunk 拟合 StandardScaler ────────────────
        #
        # ⚠ 这里曾有一个导致「聚类功能从未成功过」的越界 bug：
        #     先是一个 for chunk in loader.iter_chunks() 的循环，循环体内
        #     `all_scaled_chunks` 始终为空、X_scaled 算完即丢 —— 它对 10 万行
        #     **完整扫了一遍表却没有任何产出**，属死代码，已删除。
        #     然后是取样拟合的偏移计算：
        #         range(min(3, max(1, total // loader.chunksize + 1)))
        #     当 total 能被 chunksize 整除时会**多算一个 chunk**：
        #         total=100000, chunksize=50000 → 100000//50000+1 = 3 → 取 offset 0/50000/100000
        #     而 load_chunk(100000) 返回**空 DataFrame（0 行 0 列）**，
        #     prepare_cluster_features 取 df[CLUSTER_FEATURES] 立刻抛 KeyError：
        #         "None of [Index(['credit_score', ...])] are in the [columns]"
        #     该异常被本任务的 except 吞进返回值（Celery 状态仍是 SUCCESS），
        #     前端 pollTask 只看 Celery 状态、不看 body.status，于是静默失败 ——
        #     表现为「点聚类没反应，页面一直显示尚未执行聚类」。
        #
        # 改为按**实际存在的** offset 取样：range(0, total, chunksize) 天然不会越界。
        sample_offsets = list(range(0, total, loader.chunksize))[:3]
        scaler = StandardScaler()
        scaler.fit(np.vstack([
            prepare_cluster_features(loader.load_chunk(offset))
            for offset in sample_offsets
        ]))  # 用前 3 个（或更少的）chunk 拟合 scaler

        # 分块训练聚类
        self.update_state(state="PROGRESS", meta={
            "step": "clustering",
            "message": f"K-Means 聚类 (k={n_clusters})...",
        })

        kmeans = _choose_kmeans(n_clusters, total)

        # MiniBatchKMeans 支持 partial_fit，KMeans 需要全量数据
        if isinstance(kmeans, MiniBatchKMeans):
            for chunk in loader.iter_chunks():
                X_chunk = prepare_cluster_features(chunk)
                X_scaled = scaler.transform(X_chunk)
                kmeans.partial_fit(X_scaled)
            labels = kmeans.labels_
            # MiniBatchKMeans predict for all data
            all_labels = []
            for chunk in loader.iter_chunks():
                X_chunk = prepare_cluster_features(chunk)
                X_scaled = scaler.transform(X_chunk)
                all_labels.append(kmeans.predict(X_scaled))
            labels = np.concatenate(all_labels)

            # Sample for silhouette (全量太慢)
            sample_size = min(5000, total)
            sample_indices = np.random.choice(total, sample_size, replace=False)
            sample_chunks = []
            for chunk in loader.iter_chunks():
                sample_chunks.append(chunk)
            sample_df = loader.load_chunk(0, sample_size)
            X_sample = scaler.transform(prepare_cluster_features(sample_df))
            sample_labels = kmeans.predict(X_sample)
            silhouette = silhouette_score(X_sample, sample_labels)
        else:
            # 全量加载（KMeans 需要）
            df = loader.load_all()
            X = prepare_cluster_features(df)
            X_scaled = scaler.fit_transform(X)
            labels = kmeans.fit_predict(X_scaled)
            silhouette = silhouette_score(X_scaled, labels)

        # PCA 降维（用于 3D 可视化）
        pca = PCA(n_components=3)
        # 抽样 PCA 拟合（千万级全量 PCA 太慢）
        fit_sample = X_scaled[:min(5000, len(X_scaled))]
        pca.fit(fit_sample)

        # 聚类大小统计
        unique, counts = np.unique(labels, return_counts=True)
        cluster_sizes = {int(k): int(v) for k, v in zip(unique, counts)}

        # 保存到 DB（可选）
        if save_to_db:
            self.update_state(state="PROGRESS", meta={"step": "saving", "message": "写入聚类标签..."})
            offset = 0
            chunk_idx = 0
            for chunk in loader.iter_chunks():
                chunk_labels = labels[offset:offset + len(chunk)]
                for j, row in chunk.iterrows():
                    customer = db.query(Customer).filter(Customer.id == int(row["id"])).first()
                    if customer:
                        customer.cluster_id = int(chunk_labels[j - offset])
                offset += len(chunk)
                chunk_idx += 1
            db.commit()

        # 保存聚类模型
        # ⚠ 原子写入：`open(path,'w')` 会先截断为 0 字节再写，读取方
        # （clustering_service.get_cluster_meta → json.load）会拿到空文件而报错。
        # 与 train.py 的 _save_results_to_disk 同一处理方式：临时文件 + 原子替换。
        CLUSTER_DIR.mkdir(parents=True, exist_ok=True)
        meta = {
            "n_clusters": n_clusters,
            "silhouette": round(float(silhouette), 4),
            "cluster_sizes": cluster_sizes,
            "feature_names": CLUSTER_FEATURES,
        }
        meta_final = CLUSTER_DIR / "cluster_meta.json"
        meta_tmp = meta_final.with_name(meta_final.name + ".tmp")
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(meta_tmp, meta_final)

        return {
            "status": "completed",
            "n_clusters": n_clusters,
            "inertia": round(float(kmeans.inertia_), 2),
            "silhouette_score": round(float(silhouette), 4),
            "cluster_sizes": cluster_sizes,
            "total_customers": total,
        }

    except SoftTimeLimitExceeded:
        return {"status": "timeout", "error": "聚类计算超时"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


@celery_app.task(bind=True, name="run_elbow")
def run_elbow_task(self) -> dict:
    """Celery 任务: 肘部法则 — 确定最优 K 值 (k=2..10)。"""
    db = SessionLocal()
    try:
        loader = DataLoader(db)
        total = loader.total_count

        # 抽样（肘部法则不需要全量）
        sample_size = min(5000, total)
        self.update_state(state="PROGRESS", meta={
            "step": "sampling",
            "message": f"抽样 {sample_size}/{total} 条数据...",
        })

        df = loader.load_chunk(0, sample_size)
        X = prepare_cluster_features(df)
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        k_range = range(2, 11)
        inertias = []
        silhouette_scores = []

        for k in k_range:
            self.update_state(state="PROGRESS", meta={
                "step": "elbow",
                "current": k,
                "total": 10,
                "message": f"测试 k={k}...",
            })
            kmeans = KMeans(n_clusters=k, random_state=settings.RANDOM_STATE, n_init=10)
            labels = kmeans.fit_predict(X_scaled)
            inertias.append(round(float(kmeans.inertia_), 2))
            silhouette_scores.append(round(float(silhouette_score(X_scaled, labels)), 4))

        return {
            "status": "completed",
            "k_range": list(k_range),
            "inertias": inertias,
            "silhouette_scores": silhouette_scores,
        }

    except SoftTimeLimitExceeded:
        return {"status": "timeout", "error": "肘部法则计算超时"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
