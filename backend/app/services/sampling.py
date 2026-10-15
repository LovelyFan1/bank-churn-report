"""固定随机种子的比例分层抽样，人数上限精确守恒。"""
import numpy as np


def bounded_sample(df, limit):
    if len(df) <= limit: return df
    groups = list(df.groupby(['cluster_id','exited'],sort=True).indices.values())
    if len(groups) > limit: raise ValueError('抽样容量小于分层数量')
    targets = np.asarray([len(g)*limit/len(df) for g in groups])
    counts = np.maximum(1,np.floor(targets).astype(int))
    while counts.sum() > limit:
        eligible = np.where(counts>1)[0]
        counts[eligible[np.argmax(counts[eligible]-targets[eligible])]] -= 1
    while counts.sum() < limit:
        eligible = np.where(counts<np.asarray([len(g) for g in groups]))[0]
        counts[eligible[np.argmax(targets[eligible]-counts[eligible])]] += 1
    rng=np.random.default_rng(42)
    positions=sorted(np.concatenate([rng.choice(g,size=n,replace=False) for g,n in zip(groups,counts)]).tolist())
    return df.iloc[positions]
