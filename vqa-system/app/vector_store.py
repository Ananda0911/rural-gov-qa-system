"""FAISS 向量索引（内存镜像）：SQLite BLOB 为唯一事实源，启动与向量变更时全量重建。

设计要点：
- IndexIDMap2(IndexFlatIP)：向量 L2 归一化后内积 == 余弦，与 Python 降级路径结果完全一致
  （IndexFlat 为精确检索，无 ANN 近似损失）；查询 k=ntotal，等价于全量扫描后排序
- 不持久化 .index 文件：村级规模重建毫秒级，从根上杜绝索引与库的状态漂移；
  规模上量后可改为增量 add/remove + 文件持久化（接口不变）
- 双索引：条目全文向量 + 切片向量；切片 id 编码 = entry_id*100000 + chunk_index
- 不可用（未装 faiss / 库无向量 / 维度不符）时返回 None，检索层自动回退 sqlite-vec / Python 余弦
"""
import logging

log = logging.getLogger("vqa.vector_store")

try:
    import faiss
    import numpy as np
except Exception:  # pragma: no cover
    faiss = None
    np = None

_entry_index = None
_chunk_index = None
_CHUNK_ID_BASE = 100000


def _norm(arr) -> "np.ndarray | None":
    n = float(np.linalg.norm(arr))
    if n == 0.0 or not np.isfinite(n):
        return None
    return (arr / n).astype(np.float32)


def available() -> bool:
    return faiss is not None and _entry_index is not None


def reset() -> None:
    """清空内存索引（测试隔离用；下次 rebuild 或应用启动时自动恢复）"""
    global _entry_index, _chunk_index
    _entry_index = _chunk_index = None


def rebuild(db) -> bool:
    """从 kb_vectors / kb_chunks 全量重建两个内存索引；返回是否建出可用索引"""
    global _entry_index, _chunk_index
    if faiss is None:
        return False
    from .models import KbChunk, KbVector

    vectors = db.query(KbVector).all()
    dim = vectors[0].dim if vectors else 0
    if dim <= 0:
        _entry_index = _chunk_index = None
        return False
    e_ids, e_vecs, c_ids, c_vecs = [], [], [], []
    for v in vectors:
        if v.dim != dim or len(v.embedding) != dim * 4:
            continue          # 维度混杂（换 embedding 模型未 reindex）：跳过，交给降级路径兜底
        arr = _norm(np.frombuffer(v.embedding, dtype=np.float32))
        if arr is not None:
            e_ids.append(v.entry_id)
            e_vecs.append(arr)
    for c in db.query(KbChunk).all():
        if c.dim != dim or len(c.embedding) != dim * 4:
            continue
        arr = _norm(np.frombuffer(c.embedding, dtype=np.float32))
        if arr is not None:
            c_ids.append(c.entry_id * _CHUNK_ID_BASE + c.chunk_index)
            c_vecs.append(arr)
    if not e_vecs and not c_vecs:
        _entry_index = _chunk_index = None
        return False

    def _build(ids, vecs):
        idx = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
        if ids:
            idx.add_with_ids(np.vstack(vecs), np.asarray(ids, dtype="int64"))
        return idx

    _entry_index = _build(e_ids, e_vecs)
    _chunk_index = _build(c_ids, c_vecs)
    return True


def search(qvec: list[float]):
    """聚合检索：条目向量与切片向量按条目取最高分，返回 [(entry_id, sim)] 降序；
    索引不可用或维度不符返回 None（调用方回退下一级后端）。
    注意：结果未做发布状态过滤——ADR-4 双保险的 SQL 过滤由调用方负责"""
    if not available():
        return None
    q = _norm(np.asarray(qvec, dtype=np.float32))
    if q is None or q.shape[0] != _entry_index.d:
        return None
    sims: dict[int, float] = {}
    for index, decode in ((_entry_index, lambda i: i),
                          (_chunk_index, lambda i: i // _CHUNK_ID_BASE)):
        if index.ntotal == 0:
            continue
        D, I = index.search(q[None, :], index.ntotal)
        for sim, iid in zip(D[0].tolist(), I[0].tolist()):
            if iid >= 0 and sim > sims.get(decode(iid), -2.0):
                sims[decode(iid)] = sim
    return sorted(sims.items(), key=lambda x: x[1], reverse=True)


def stats() -> str:
    if not available():
        return "不可用（回退 SQLite/Python 余弦）"
    return f"dim={_entry_index.d} 条目={_entry_index.ntotal} 切片={_chunk_index.ntotal}"
