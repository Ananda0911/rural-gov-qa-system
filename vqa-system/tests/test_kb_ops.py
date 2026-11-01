"""知识库索引一致性回归：FTS/向量幂等重建、T3 标题相似、到期下线闭环、append-only 留痕"""
import pytest
from sqlalchemy import text

from app import kb_ops, llm, search
from app.models import KbChunk, KbEntry, KbVector

Q = "农村宅基地申请流程"
CONTENT = "农村宅基地申请由村民向村委会提出书面申请，经村委会审核公示后报乡镇人民政府审批。"


def test_fts_upsert_idempotent(db, publish, run):
    e = run(publish(Q, CONTENT))
    kb_ops.fts_upsert(db, e)                     # 重复 upsert 不产生重复行
    kb_ops.fts_upsert(db, e)
    db.commit()
    ids = search.fts_search(db, search.jieba_tokens(Q))
    assert ids.count(e.id) == 1


def test_vector_upsert_rebuild_no_duplicate_chunks(db, publish, run):
    long_content = "宅基地申请材料说明。" + "申请人应当提交身份证明与申请表并向村委会备案。" * 60
    e = run(publish(Q, long_content))
    n1 = db.query(KbChunk).filter(KbChunk.entry_id == e.id).count()
    assert n1 >= 2                               # 长文确实切出多片
    run(kb_ops.vector_upsert(db, e))             # 删旧插新，数量不变
    db.commit()
    n2 = db.query(KbChunk).filter(KbChunk.entry_id == e.id).count()
    assert n1 == n2
    assert db.query(KbVector).filter(KbVector.entry_id == e.id).count() == 1


def test_vector_upsert_failure_returns_false(db, run, monkeypatch):
    e = KbEntry(title="无向量条目", category="办事流程", content=CONTENT,
                basis_file="x.pdf", publish_org="y", status="published")
    db.add(e)
    db.flush()

    async def _down(texts):
        raise llm.LLMError("embedding 不可用")
    monkeypatch.setattr(llm, "embed", _down)
    assert run(kb_ops.vector_upsert(db, e)) is False    # 发布仍可继续（降级设计）


def test_check_similar_titles_t3_and_self_exclusion(db, publish, run):
    # 注意：T3 比较基准是条目「全文向量」（标题+正文，复用 kb_vectors，不为标题单独建向量），
    # 与标题不相关的正文会显著稀释相似度（实测 0.876 → 0.43），正文需同话题才是真实使用形态
    med_content = ("2026年城乡居民医保个人缴费标准为每人每年400元，集中缴费期截止至12月31日，"
                   "请各村通知到户。")
    e = run(publish("2026年城乡居民医保缴费通知", med_content))
    sims = run(kb_ops.check_similar_titles(db, "2026年城乡居民医保缴费通知（更新版）"))
    assert any(s["id"] == e.id for s in sims)           # 近似标题 ≥ T3 触发提示
    assert run(kb_ops.check_similar_titles(db, e.title, exclude_id=e.id)) == []  # 排除自身


def test_expire_entries_full_flow(db, publish, run):
    e = run(publish(Q, CONTENT, expire_date="2020-01-01"))
    n = kb_ops.expire_entries(db)
    assert n >= 1
    db.expire_all()
    db.refresh(e)
    assert e.status == "expired"
    assert db.get(KbVector, e.id) is None               # 向量索引随下线清理
    assert db.query(KbChunk).filter(KbChunk.entry_id == e.id).count() == 0
    # 检索层双保险：过期条目不再召回
    r = run(search.hybrid_retrieve(db, Q))
    assert all(c["id"] != e.id for c in r["candidates"])
    assert db.get(KbEntry, e.id).offline_reason is None  # 到期下线不填 manual 原因


def test_operation_logs_append_only_trigger(db, staff):
    from app.audit import log_action

    log_action(db, staff, "login_success")              # 走 ORM 插入（默认值生效）
    db.commit()
    with pytest.raises(Exception):                       # 触发器拦截 UPDATE
        db.execute(text("UPDATE operation_logs SET action = 'tampered'"))
    db.rollback()
    with pytest.raises(Exception):                       # 触发器拦截 DELETE
        db.execute(text("DELETE FROM operation_logs"))
    db.rollback()
