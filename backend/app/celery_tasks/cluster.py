"""聚类 Celery 任务 — 在独立 Worker 进程中执行。

包含:
- run_kmeans_task: K-Means 聚类 + 落盘 scaler/PCA/t-SNE（供散点图复用）
- run_elbow_task: 肘部法则
"""

import json
import logging
import os
from pathlib import Path

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
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.manifold import TSNE

logger = logging.getLogger(__name__)

CLUSTER_DIR = Path(__file__).parent.parent.parent / "saved_models"

# ── t-SNE 参数（实测确定，见 run_kmeans_task 内的对照数据）──────
# 散点图最多画这么多点。t-SNE 的耗时随 n 近似线性：
#   5000点 6.8s / 10000点 15.3s / 15000点 23.0s / 20000点 31.6s
# 15000 与前端 MAX_SCATTER_POINTS 一致，既够密又控制在 ~23s。
TSNE_N_POINTS = 15000
# perplexity 实测（15000 点）：15→密度峰1/最大格47，30→峰2/最大格66，50→峰6/最大格65。
# 取 30：结构最均衡，且是 t-SNE 的常用默认值。
TSNE_PERPLEXITY = 30


def _compute_tsne(X_scaled: np.ndarray, labels: np.ndarray):
    """计算 t-SNE 二维嵌入。返回 (embedding, sample_index) 或 None。

    ⚠ 为什么抽样：t-SNE 是 O(n log n) 但常数很大，全量 10 万点实测远超
      可接受范围；且画布只有 838×360，15000 点在物理上已无法分辨。
      **分层抽样**（每簇按同比例）保证各簇在图上的相对比例不失真 ——
      这一点比"随机抽样"重要：随机抽样会让小簇（1251 人的低薪簇）的点数
      波动很大，而分层抽样让它稳定按比例出现。

    ⚠ 返回 sample_index 是为了让调用方知道"嵌入对应的是哪几行"，
      从而能把 cluster_id / exited 正确对齐回原始数据。
    """
    n = len(X_scaled)
    if n == 0:
        return None

    if n > TSNE_N_POINTS:
        frac = TSNE_N_POINTS / n
        keep = []
        for cid in np.unique(labels):
            sub = np.where(labels == cid)[0]
            k = max(1, int(round(len(sub) * frac)))
            picked = np.random.default_rng(settings.RANDOM_STATE).choice(
                sub, size=min(k, len(sub)), replace=False)
            keep.extend(picked.tolist())
        sample_index = np.array(sorted(keep))
    else:
        sample_index = np.arange(n)

    emb = TSNE(
        n_components=2,
        perplexity=TSNE_PERPLEXITY,
        # init="pca"：比默认的随机初始化**更稳定**（实测同种子两次结果
        # 最大坐标差 0.0），且收敛更快。
        init="pca",
        learning_rate="auto",
        max_iter=500,
        random_state=settings.RANDOM_STATE,
    ).fit_transform(X_scaled[sample_index])

    return emb, sample_index


def _choose_kmeans(n_clusters: int, n_samples: int):
    """选择聚类器 —— 小样本用 KMeans，大样本用 KMeans 的**分块等价实现**。

    ⚠ 历史与实测（10 万行真实数据的 A/B 结果，勿回退）：

    旧实现在 n_samples > 50000 时返回
        MiniBatchKMeans(batch_size=10000, n_init=3)
    导致**一整个客群从未被分出来**。全量 KMeans 在 k=3/4/5/6/7/8
    **每一个 k 下都稳定浮现一个约 1250 人的小簇**（estimated_salary
    均值 1917 元，是全体 100,240 的 1/52），而 MiniBatch 分支永远找不到它。

    根因不是初始化不好，而是 **batch 太小导致小簇代表性不足**：
    占比 1.25% 的簇在 batch_size=10000 的随机批次里平均每批只出现约
    125 个样本，质心被主流样本持续拉回，永远占不到位。

    实测对照（同一份 10 万行数据、同一 scaler、同 random_state=42）：

        batch_size=10000  n_init=3   → 簇大小 [25577,22760,22649,14671,14343]  最小簇 14343  ❌
        batch_size=10000  n_init=10  → 输出**一字不差**                          ❌
        batch_size=50000  n_init=3   → [32370,25588,21587,19363,1092]           ✅
        batch_size=100000 n_init=3   → 同上                                     ✅
        KMeans 全量       n_init=10  → [28382,25539,23748,21080,1251]           ✅

    ⚠ 关键：**提高 n_init 完全无效（3→10 输出逐位相同）**。
      n_init 只解决"初始质心选得好不好"，解决不了"代表性不足"。
      所以"把 n_init 调大"是一个看起来合理、实测无效的错解。

    ⚠ 而 batch_size 调大虽然在本 seed 下有效，但**不稳定**：
      换 seed 实测（5 个种子 × 小簇成员 Jaccard 一致性）：
          batch_size=25000/50000/100000：
              seed42 ✅ / seed0 ❌ / seed1 ❌ / seed2 ✅ / seed7 ❌
              各种子间 Jaccard 最低 **0.0000**（找到的完全不是同一批人）
          KMeans 全量：
              5 个种子**全部**找到小簇（1249~1253 人）
              各种子间 Jaccard **0.9936 ~ 1.0000**
      即 batch_size 的"成功"是撞运气，**不是可复现的修复**。

    ⚠ 因此这里**不再按数据量分流到 MiniBatchKMeans**。
      代价实测（真实 10 万行，含加载 + 标准化 + 拟合 + silhouette）：
          当前 MiniBatch 流程 = 6.19s
          全量 KMeans 流程    = 7.75s   （仅慢 1.56s）
      可扩展性实测（11 维，k=5，n_init=10，纯拟合）：
          10 万行 1.79s / RSS 264MB
          100 万行 3.82s / RSS 357MB
      也即"全量"在这个维度规模下并非瓶颈，没有理由为了省 1.5 秒
      牺牲一整个客群。

    ⚠ 还有一个旧实现的隐性缺陷：MiniBatch 分支按 **chunk 分别**
      调用 prepare_cluster_features，而该函数在每个 chunk 内部独立算
      winsorize 分位数。实测 chunksize=50000 时两块对
      balance_salary_ratio 的截断上限分别是 86.6398 / 83.8548（差 2.78），
      balance 分别 192104.34 / 192391.78（差 287.44）。
      全量路径天然只有一份阈值，此问题一并消除。
    """
    # 全量拟合。⭐ 不要改回按 n_samples 分流到 MiniBatchKMeans —— 见上方实测。
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

        # ── 全量加载 ──────────────────────────────────────────
        #
        # ⚠ 为什么从「分块 partial_fit」改成「一次性全量加载」：
        #   见 _choose_kmeans 上方说明 —— 分块 MiniBatch 会**漏掉一整个客群**。
        #   实测代价（真实 10 万行，含加载+标准化+拟合+silhouette）：
        #       分块 MiniBatch 流程 = 6.19s
        #       全量 KMeans 流程    = 7.75s   ← 仅慢 1.56s
        #   可扩展性（11 维、k=5、n_init=10、纯拟合）：100 万行 3.82s / RSS 357MB。
        #   即"全量"在这个维度规模下不是瓶颈，没有理由为省 1.5 秒牺牲一个客群。
        #
        # ⚠ 附带修掉一个隐性不一致：旧实现按 chunk 分别调用
        #   prepare_cluster_features，而该函数在**每个 chunk 内部**独立计算
        #   winsorize 分位数。实测 chunksize=50000 时两块对
        #   balance_salary_ratio 的截断上限为 86.6398 / 83.8548（差 2.78），
        #   balance 为 192104.34 / 192391.78（差 287.44）。
        #   全量路径天然只有一份阈值，该问题一并消除。
        #
        # ⚠ 同时删除一个曾导致「聚类功能从未成功过」的越界 bug 的遗留写法：
        #   旧代码用 range(min(3, max(1, total // chunksize + 1))) 取 offset，
        #   当 total 能被 chunksize 整除时会**多算一块**：
        #       total=100000, chunksize=50000 → 100000//50000+1 = 3
        #       → 取 offset 0/50000/100000，而 load_chunk(100000) 返回
        #         **空 DataFrame（0 行 0 列）**，prepare_cluster_features 取
        #         df[CLUSTER_FEATURES] 立刻抛 KeyError：
        #         "None of [Index(['credit_score', ...])] are in the [columns]"
        #   该异常被本任务 except 吞进返回值（Celery 状态仍是 SUCCESS），
        #   前端只看 Celery 状态、不看 body.status → 静默失败。
        #   改成全量加载后，这个 offset 计算彻底消失，不可能再犯。
        df = loader.load_all()
        X = prepare_cluster_features(df)
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        self.update_state(state="PROGRESS", meta={
            "step": "clustering",
            "message": f"K-Means 聚类 (k={n_clusters})...",
        })

        kmeans = _choose_kmeans(n_clusters, total)

        # 全量拟合（_choose_kmeans 现在只返回 KMeans，见其说明）
        labels = kmeans.fit_predict(X_scaled)

        # silhouette：**必须抽样算**（全量 10 万行约 3.2s，可接受但没必要）。
        # ⚠ 旧实现用 loader.load_chunk(0, 5000)，即「取数据库 id 最小的 5000 条」。
        #   实测该做法**偏差很小，不是严重问题**（如实记录，避免夸大）：
        #       字段              前5000行      随机5000行     全量      偏差
        #       credit_score       648.70       651.35      650.52    -0.28%
        #       balance         76,545.25    77,940.07   77,010.98   -0.60%
        #       estimated_salary 99,695.80  100,125.51  100,240.11   -0.54%
        #       流失率             20.22%       20.56%      20.42%     -0.20pp
        #       salary<5000 人数      138          125         2584     略偏多
        #   即这不是「系统性漏掉尾部」（实测它反而略多），只是"非随机"这一
        #   方法学瑕疵。改用随机抽样是为了**正确性上的干净**，不是为了修大偏差。
        n_sil = min(20000, total)
        if total > n_sil:
            rng = np.random.default_rng(settings.RANDOM_STATE)
            sil_idx = rng.choice(total, n_sil, replace=False)
            silhouette = silhouette_score(X_scaled[sil_idx], labels[sil_idx])
        else:
            silhouette = silhouette_score(X_scaled, labels)

        # PCA 降维（用于 3D 可视化）
        #
        # ⚠ 旧实现有两处问题，这里一并修掉：
        #   1) 它用 X_scaled[:5000] 拟合 PCA，而 X_scaled 在旧的 MiniBatch
        #      分支里是**最后一个 chunk**（不是全量），即 PCA 只用了 1/2 数据；
        #   2) 拟合完的 pca 对象**从未被保存或使用**，是纯死代码
        #      （属于"算了 100000×11 的降维却扔掉"）。
        #   现在 fitted 的 PCA 会随模型一起落盘，供散点图复用，
        #   避免前端每次请求都重新 fit（那正是散点图与聚类不同源的根源）。
        pca = PCA(n_components=3, random_state=settings.RANDOM_STATE)
        pca.fit(X_scaled)

        # ── t-SNE 2D 嵌入（散点图用）──────────────────────────
        #
        # ⚠ 为什么改用 t-SNE（实测数据，勿回退到只用 PCA）：
        #   PCA 的前两个主成分只覆盖 **24.41%** 方差，且横纵轴几乎只反映
        #   balance / num_products / estimated_salary / balance_salary_ratio
        #   这 4 个特征，其余 8 个（credit_score / age / tenure / 满意度…）
        #   载荷全部 < 0.05 —— 在图上根本看不见。
        #
        #   后果是散点图呈现「一坨一坨」，且这些团块**不是客群**：
        #   实测团块是 `num_products` 的 4 个取值切出的 4 条**斜带**，
        #   但因「层间中心距离 1.243 < 层内离散度 1.345」（信噪比 0.92 < 1），
        #   相邻带互相重叠糊在一起，看起来像团块。
        #
        #   t-SNE 对照实测（15000 点、perplexity=30）：
        #       指标            PCA      t-SNE
        #       最大格子人数     293  →    66
        #       密度峰个数        10  →     2
        #       5-NN 可分性    44.2%  →  99.2%
        #   即结构被真正摊开。
        #
        # ⚠ 代价与取舍：
        #   · 耗时实测：5000点 6.8s / 10000点 15.3s / **15000点 23.0s** / 20000点 31.6s
        #     —— 无法每请求实时计算，故**在此预计算并落盘**（与 scaler/pca 同机制）。
        #   · perplexity 实测对比（15000 点）：15→峰1/最大格47，30→峰2/最大格66，
        #     50→峰6/最大格65。取 **30**（结构最均衡，也是常用默认值）。
        #   · 固定 random_state，实测两次结果**最大坐标差 0.0**，完全可复现。
        #   · t-SNE 的轴**无物理含义**、簇间距离不可解释 —— 前端必须标注这一点。
        #   · t-SNE 没有 `transform`，新增客户需重跑，因此只用于**展示**，
        #     不参与任何判定（分级/排序/筛选仍走 risk_scoring 的分位数体系）。
        #
        # ⚠ 失败不能拖垮聚类：t-SNE 是可视化增强，若因内存/数值问题失败，
        #   把 tsne 置 None 继续走完流程（前端会回退到 PCA 显示）。
        #   实测在 20 万行以内未失败，但该保护必须有。
        tsne_embedding = None
        try:
            self.update_state(state="PROGRESS", meta={
                "step": "embedding", "message": "计算 t-SNE 二维嵌入...",
            })
            tsne_embedding = _compute_tsne(X_scaled, labels)
        except Exception as e:
            logger.warning("t-SNE 计算失败，散点图将回退为 PCA: %s: %s",
                           type(e).__name__, e)

        # 聚类大小统计
        unique, counts = np.unique(labels, return_counts=True)
        cluster_sizes = {int(k): int(v) for k, v in zip(unique, counts)}

        # 保存到 DB（可选）
        if save_to_db:
            self.update_state(state="PROGRESS", meta={"step": "saving", "message": "写入聚类标签..."})

            # ⚠ 旧实现有两个严重问题，勿回退：
            #
            #   1) **索引表达式错误（被巧合掩盖）**：
            #          chunk_labels = labels[offset:offset + len(chunk)]
            #          for j, row in chunk.iterrows():
            #              customer.cluster_id = int(chunk_labels[j - offset])
            #      `chunk.iterrows()` 的 j 是 **chunk 内部** 0-based 索引，
            #      而 chunk_labels 已是本 chunk 的局部切片，正确写法是
            #      `chunk_labels[j]`，写 `j - offset` 是多余的。
            #      当前 total=100000 / chunksize=50000 → 2 块**等长**，
            #      第 2 块取 chunk_labels[j-50000]，负索引恰好从尾部绕回
            #      chunk_labels[0..49999]，**碰巧正确**（实测 DB 数据无误，
            #      复现一致率 100.0000%）。但换任何其他分块方式必炸：
            #          chunksize=30000 → 第2、3块 IndexError: index -60000
            #          chunksize=25000 → 第2、3块 IndexError: index -50000
            #          chunksize=70000 → 第1块   IndexError: index -70000
            #      现改为**按 id 显式映射**，不依赖任何索引算术，
            #      无论分块方式如何都正确。
            #
            #   2) **逐行 SELECT + 逐行 UPDATE（10 万次往返）**：
            #          customer = db.query(Customer).filter(Customer.id == ...).first()
            #      实测 2000 行耗时 0.74s → 外推 10 万行约 **37 秒**，
            #      在 Celery 软超时（1800s）内虽不至于失败，但纯属浪费。
            #      改用 bulk_update_mappings，实测 10 万行 **1.34 秒**（约 28 倍）。
            #
            # ⚠ labels 与 df 的顺序对齐依据：
            #   load_all() 与 iter_chunks() 都用 ORDER BY id，实测两者
            #   id 序列完全一致（且 id 唯一、连续，1..100000）。
            #   下面仍显式校验一次长度，避免将来有人改动 load_all 的排序。
            if len(labels) != len(df):
                raise RuntimeError(
                    f"标签数({len(labels)})与数据行数({len(df)})不一致，拒绝写入 "
                    f"—— 这会导致整批 cluster_id 错位"
                )
            mappings = [
                {"id": int(i), "cluster_id": int(l)}
                for i, l in zip(df["id"].values, labels)
            ]
            db.bulk_update_mappings(Customer, mappings)
            db.commit()

            # ⚠ 必须让 **backend 进程** 也感知到 cluster_id 变了。
            #
            # 实测缺陷：不加这一步时，Celery worker 写库成功后，
            # backend 的 get_cached_customer_df() 仍返回旧快照，
            # 接口继续吐旧簇 —— 实测 DB 已是 {28382,23748,25539,21080,1251}
            # 而 /api/cluster/profiles 返回 {25577,22760,22649,14343,14671}，
            # 并会持续到 CACHE_TTL（1 小时）到期或 backend 重启。
            # 这正是「点重新聚类，页面像没反应」的根因。
            #
            # invalidate_customer_cache() 现在除了清本进程内存，
            # 还会递增磁盘上的版本号（见 data_loader 的说明），
            # backend 每次读缓存时会比对版本号，从而立即刷新。
            from app.services.data_loader import invalidate_customer_cache
            invalidate_customer_cache()

        # 保存聚类模型
        # ⚠ 原子写入：`open(path,'w')` 会先截断为 0 字节再写，读取方
        # （clustering_service.get_cluster_meta → json.load）会拿到空文件而报错。
        # 与 train.py 的 _save_results_to_disk 同一处理方式：临时文件 + 原子替换。
        CLUSTER_DIR.mkdir(parents=True, exist_ok=True)

        # ⚠ 把 fitted 的 scaler + PCA 一并落盘（原子写）。
        #
        # 为什么必须保存：散点图（clustering_service.get_3d_scatter_data）
        # 此前是**自己重新 fit** 一份 scaler 和 PCA，与聚类时用的不是同一组
        # 变换。虽然同样参数下结果数值接近，但只要数据或代码有一处不同步，
        # 「图上看到的簇」就不再是「聚类算出的簇」——这是方法学上的隐患。
        # 保存后散点图可直接复用，保证**同源**。
        #
        # ⚠ 用 joblib.dump 而非 pickle，与 train.py 的模型保存方式一致。
        import joblib
        for obj, fname in ((scaler, "cluster_scaler.joblib"),
                           (pca, "cluster_pca.joblib")):
            final = CLUSTER_DIR / fname
            tmp = final.with_name(final.name + ".tmp")
            joblib.dump(obj, tmp)
            os.replace(tmp, final)

        # ⚠ t-SNE 嵌入落盘（原子写）。
        #
        # 为什么必须落盘：实测 15000 点耗时 **23.0 秒**，不可能每请求实时计算。
        # 而它是散点图的主显示依据（PCA 只覆盖 24.41% 方差、团块重叠严重）。
        #
        # ⚠ 用 **np.savez_compressed**（.npz）而非 joblib/pickle：
        #   这里存的是两个纯数值数组（embedding + sample_index），
        #   npz 是跨版本稳定、可被任何 numpy 读取的格式，且自带压缩。
        #   不存对象也避免了 pickle 的版本耦合风险。
        #
        # ⚠ 与 meta.json 的发布顺序：meta 最后写（见下），
        #   读取方以 meta 为"聚类已完成"的信号，不会读到半套产物。
        if tsne_embedding is not None:
            emb, sample_index = tsne_embedding
            final = CLUSTER_DIR / "cluster_tsne.npz"
            # ⚠ 临时文件名必须**以 .npz 结尾**。
            #   np.savez_compressed 对不以 .npz 结尾的路径会自动补后缀：
            #       写 "cluster_tsne.npz.tmp" → 实际生成 "cluster_tsne.npz.tmp.npz"
            #   于是紧接着的 os.replace(tmp, final) 抛 FileNotFoundError，
            #   整个聚类任务失败。实测确认该行为（numpy 1.26.4）。
            #   这里用 tmp_name + 显式 .npz 后缀规避。
            tmp = final.with_name(final.stem + ".tmp.npz")
            np.savez_compressed(tmp, embedding=emb, sample_index=sample_index)
            os.replace(tmp, final)
        else:
            # t-SNE 失败时删掉旧文件，否则散点图会继续用**上一次**的嵌入
            # 与**这一次**的簇标签配对 —— 图上的簇会完全错位（比没有图更糟）。
            stale = CLUSTER_DIR / "cluster_tsne.npz"
            if stale.exists():
                try:
                    stale.unlink()
                    logger.warning("已删除过期的 cluster_tsne.npz（本次 t-SNE 未成功）")
                except OSError as e:
                    logger.warning("删除过期 t-SNE 文件失败: %s", e)

        meta = {
            "n_clusters": n_clusters,
            "silhouette": round(float(silhouette), 4),
            "cluster_sizes": cluster_sizes,
            "feature_names": CLUSTER_FEATURES,
            # 记录实际使用的算法，便于事后追溯「这批标签是怎么算出来的」。
            # 背景：本项目曾因 MiniBatch/全量 的差异漏掉一整个客群，
            # 而 meta 里不留算法信息时，事后无法从产物反推。
            "algorithm": type(kmeans).__name__,
            "n_init": getattr(kmeans, "n_init", None),
            "inertia": round(float(kmeans.inertia_), 2),
            # PCA 解释方差一并留存，避免前端把写死的百分比当作实时值
            "explained_variance": [round(float(v), 6)
                                   for v in pca.explained_variance_ratio_],
            # t-SNE 元信息 —— 前端据此标注"轴无物理含义"
            "tsne": (
                {
                    "available": True,
                    "n_points": int(len(tsne_embedding[1])),
                    "perplexity": TSNE_PERPLEXITY,
                    "random_state": settings.RANDOM_STATE,
                }
                if tsne_embedding is not None
                else {"available": False, "reason": "t-SNE 计算失败，散点图回退 PCA"}
            ),
        }
        meta_final = CLUSTER_DIR / "cluster_meta.json"
        meta_tmp = meta_final.with_name(meta_final.name + ".tmp")
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        # ⚠ meta.json 必须**最后**发布：它是读取方判断"聚类已完成"的信号。
        #   若先落盘再写模型，读者可能拿到新 meta + 旧模型。
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
