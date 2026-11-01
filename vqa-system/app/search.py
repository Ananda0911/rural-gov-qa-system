"""混合检索：FTS5 关键词（jieba 分词）+ 向量余弦，RRF(k=60) 融合取 Top5
候选集恒为「已发布且未过期」（ADR-4 双保险）；embedding 不可用自动降级纯关键词。
"""
import json
import math
import struct

import jieba
from sqlalchemy import text

from . import llm, vector_store
from .config import settings
from .db import vec_available

_STOP = set("""的 了 和 是 就 都 而 及 与 或者 一个 我们 你们 他们 什么 怎么 怎么样 为什么
哪 哪些 吗 呢 吧 啊 呀 这 那 这个 那个 有 在 不 请 告诉 一下 谢谢""".split())


def jieba_tokens(q: str) -> list[str]:
    """分词（检索模式）：去停用词，保留长度≥2 的词（含数字/字母组合）"""
    out = []
    for tok in jieba.lcut_for_search(q):
        tok = tok.strip()
        if not tok or tok in _STOP:
            continue
        if len(tok) < 2:
            continue
        out.append(tok)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def fts_expr(tokens: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in tokens[:12] if '"' not in t)


def pack_vec(v: list[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def unpack_vec(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


PUBLISHED_SQL = "e.status='published' AND (e.expire_date IS NULL OR e.expire_date > :today)"


def fts_search(db, tokens: list[str], limit: int = 20) -> list[int]:
    expr = fts_expr(tokens)
    if not expr:
        return []
    sql = text(
        f"SELECT f.rowid AS eid, bm25(kb_fts) AS score "
        f"FROM kb_fts f JOIN kb_entries e ON e.id = f.rowid "
        f"WHERE kb_fts MATCH :m AND {PUBLISHED_SQL} "
        f"ORDER BY score LIMIT :lim"
    )
    rows = db.execute(sql, {"m": expr, "today": _today(), "lim": limit}).fetchall()
    return [r[0] for r in rows]


def vector_search_sync(db, qvec: list[float], limit: int = 20) -> list[tuple[int, float]]:
    """同步向量检索（已发布未过期条目）：条目全文向量与切片向量并查，按条目聚合取最高相似度；
    三级后端自动降级：FAISS 内存索引 → sqlite-vec SQL → Python 余弦（结果口径一致）"""
    today = _today()
    res = vector_store.search(qvec)          # 一级：FAISS；None = 不可用/维度不符，降级下一级
    if res is not None:
        if not res:
            return []
        # FAISS 镜像不过滤发布状态（与表同步重建），ADR-4 双保险由 SQL 兜底过滤
        valid = {r[0] for r in db.execute(
            text(f"SELECT e.id FROM kb_entries e WHERE {PUBLISHED_SQL}"), {"today": today})}
        return [(eid, s) for eid, s in res if eid in valid][:limit]
    if vec_available():
        sql = text(
            f"SELECT entry_id, MAX(sim) AS s FROM ("
            f" SELECT v.entry_id, 1 - vec_distance_cosine(v.embedding, vec_f32(:qj)) AS sim "
            f" FROM kb_vectors v JOIN kb_entries e ON e.id = v.entry_id WHERE {PUBLISHED_SQL} "
            f" UNION ALL "
            f" SELECT c.entry_id, 1 - vec_distance_cosine(c.embedding, vec_f32(:qj)) AS sim "
            f" FROM kb_chunks c JOIN kb_entries e ON e.id = c.entry_id WHERE {PUBLISHED_SQL} "
            f") GROUP BY entry_id ORDER BY s DESC LIMIT :lim"
        )
        rows = db.execute(sql, {"qj": json.dumps(qvec), "today": today, "lim": limit}).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    sims: dict[int, float] = {}
    for table, alias in (("kb_vectors", "v"), ("kb_chunks", "c")):
        rows = db.execute(
            text(f"SELECT {alias}.entry_id, {alias}.embedding FROM {table} {alias} "
                 f"JOIN kb_entries e ON e.id = {alias}.entry_id WHERE {PUBLISHED_SQL}"),
            {"today": today},
        ).fetchall()
        for eid, emb in rows:
            s = cosine(qvec, unpack_vec(emb))
            if s > sims.get(eid, -2.0):
                sims[eid] = s
    scored = sorted(sims.items(), key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _today() -> str:
    from .models import today_str
    return today_str()


async def embed_question(question: str):
    """问题向量；失败返回 None（调用方降级 degraded=1）"""
    try:
        vecs = await llm.embed([question])
        return vecs[0]
    except llm.LLMError:
        return None


async def hybrid_retrieve(db, question: str) -> dict:
    """双路召回 + RRF 融合，返回 {candidates:[{id,sim,rrf}], degraded, qvec}"""
    tokens = jieba_tokens(question)
    fts_ids = fts_search(db, tokens)
    qvec = await embed_question(question)
    degraded = qvec is None
    vec_ids: list[tuple[int, float]] = []
    if qvec is not None:
        vec_ids = vector_search_sync(db, qvec)

    scores: dict[int, dict] = {}
    for rank, eid in enumerate(fts_ids):
        scores.setdefault(eid, {"id": eid, "sim": None, "rrf": 0.0})
        scores[eid]["rrf"] += 1.0 / (60 + rank + 1)
    for rank, (eid, sim) in enumerate(vec_ids):
        scores.setdefault(eid, {"id": eid, "sim": None, "rrf": 0.0})
        scores[eid]["sim"] = sim
        scores[eid]["rrf"] += 1.0 / (60 + rank + 1)

    candidates = sorted(scores.values(), key=lambda x: x["rrf"], reverse=True)[:5]
    return {"candidates": candidates, "degraded": degraded, "qvec": qvec, "tokens": tokens}


def strong_fts_match(db, entry, tokens: list[str]) -> bool:
    """FTS 明确命中（T1 之外的高相关旁证）：
    ① 标题/标签与问题词重叠 ≥ 2；或 ② 问题关键词在条目全文覆盖 ≥ 3 个且 ≥ 40%（防单词误报）"""
    if not tokens:
        return False
    q = set(tokens)
    title_tags = set(jieba_tokens(entry.title)) | set(jieba_tokens(" ".join(entry.tags_list())))
    if len(title_tags & q) >= 2:
        return True
    full = title_tags | set(jieba_tokens(entry.content))
    shared = full & q
    return len(shared) >= 3 and len(shared) / len(q) >= 0.4
