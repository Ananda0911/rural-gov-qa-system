"""知识库索引管理：FTS5 与向量表在条目发布/编辑的同一事务内同步（5.6 即时生效）"""
import json
from datetime import datetime, timedelta

from sqlalchemy import text

from . import docing, llm, search, vector_store
from .audit import log_action
from .config import settings
from .models import KbChunk, KbEntry, KbVector, now_str, today_str


# ---------- FTS5 ----------
def fts_upsert(db, entry: KbEntry) -> None:
    db.execute(text("DELETE FROM kb_fts WHERE rowid = :id"), {"id": entry.id})
    idx = " ".join(search.jieba_tokens(entry.title) + search.jieba_tokens(entry.content)
                   + search.jieba_tokens(" ".join(entry.tags_list())))
    if idx:
        db.execute(text("INSERT INTO kb_fts(rowid, title, content, tags) VALUES (:id, :t, :c, :g)"),
                   {"id": entry.id, "t": _tok(entry.title), "c": _tok(entry.content),
                    "g": _tok(" ".join(entry.tags_list()))})


def _tok(s: str) -> str:
    return " ".join(search.jieba_tokens(s or ""))


def fts_delete(db, entry_id: int) -> None:
    db.execute(text("DELETE FROM kb_fts WHERE rowid = :id"), {"id": entry_id})


# ---------- 向量（条目全文 + 切片同事务） ----------
async def vector_upsert(db, entry: KbEntry) -> bool:
    """条目全文向量（标题+正文）与切片向量同事务重建；
    embedding 任一失败返回 False（发布仍成功，该条暂缺向量索引，符合降级设计）"""
    try:
        vecs = await llm.embed([f"{entry.title}\n{entry.content}"])
        vec = vecs[0]
        chunks = docing.split_chunks(entry.content)
        cvecs = await llm.embed(chunks) if chunks else []
    except llm.LLMError:
        return False
    kv = db.get(KbVector, entry.id)
    if kv is None:
        kv = KbVector(entry_id=entry.id)
        db.add(kv)
    kv.embedding = search.pack_vec(vec)
    kv.model = settings.llm_embed_model
    kv.dim = len(vec)
    kv.updated_at = now_str()
    # 切片重建：删旧插新，与条目状态同一事务
    db.query(KbChunk).filter(KbChunk.entry_id == entry.id).delete()
    for i, (ct, cv) in enumerate(zip(chunks, cvecs)):
        db.add(KbChunk(entry_id=entry.id, chunk_index=i, chunk_text=ct,
                       embedding=search.pack_vec(cv), dim=len(cv), updated_at=now_str()))
    db.flush()                        # autoflush=False：先落 pending 写入，镜像才能读到最新态
    vector_store.rebuild(db)      # FAISS 镜像随向量变更即时重建（BLOB 为事实源）
    return True


def vector_delete(db, entry_id: int) -> None:
    kv = db.get(KbVector, entry_id)
    if kv:
        db.delete(kv)
    db.query(KbChunk).filter(KbChunk.entry_id == entry_id).delete()
    db.flush()                        # 同上：删除先落库再重建镜像
    vector_store.rebuild(db)


# ---------- 标题相似提示（T3） ----------
async def check_similar_titles(db, title: str, exclude_id: int | None = None) -> list[dict]:
    """新增/编辑保存时与已发布条目标题比对，≥ T3 返回相似清单（前端提示是否为更新版本）"""
    try:
        vecs = await llm.embed([title])
        tvec = vecs[0]
    except llm.LLMError:
        return []
    today = today_str()
    rows = db.execute(
        text("SELECT v.entry_id, v.embedding, v.dim FROM kb_vectors v "
             "JOIN kb_entries e ON e.id = v.entry_id "
             "WHERE e.status='published' AND (e.expire_date IS NULL OR e.expire_date > :t)"),
        {"t": today},
    ).fetchall()
    out = []
    for eid, emb, dim in rows:
        if exclude_id and eid == exclude_id:
            continue
        if dim != len(tvec):
            continue
        sim = search.cosine(tvec, search.unpack_vec(emb))
        if sim >= settings.t3_similar:
            e = db.get(KbEntry, eid)
            if e:
                out.append({"id": e.id, "title": e.title, "sim": round(sim, 3)})
    out.sort(key=lambda x: x["sim"], reverse=True)
    return out[:3]


# ---------- 到期下线（定时任务 + 手动入口共用） ----------
def expire_entries(db) -> int:
    """published 且 expire_date ≤ 今日 → expired + 留痕；检索层恒过滤为双保险"""
    today = today_str()
    rows = db.query(KbEntry).filter(
        KbEntry.status == "published", KbEntry.expire_date.isnot(None),
        KbEntry.expire_date <= today,
    ).all()
    for e in rows:
        e.status = "expired"
        e.updated_at = now_str()
        fts_delete(db, e.id)
        vector_delete(db, e.id)
        log_action(db, None, "kb_expire", "kb_entry", e.id,
                   {"title": e.title, "expire_date": e.expire_date})
    if rows:
        db.commit()
        vector_store.rebuild(db)   # 到期下线后镜像同步（下线的条目/切片即时退出 FAISS）
    return len(rows)


def year_ago_str() -> str:
    return (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
