"""FAISS 向量索引回归：镜像可用性、与降级路径结果一致性、维度守卫、下线即时退出"""
import pytest

from app import kb_ops, llm, search, vector_store

Q = "农村宅基地申请流程"
CONTENT = "农村宅基地申请由村民向村委会提出书面申请，经村委会审核公示后报乡镇人民政府审批。"


def test_faiss_ready_after_publish(db, publish, run):
    assert vector_store.available() is False       # 用例隔离：镜像已重置
    run(publish(Q, CONTENT))
    assert vector_store.available() is True
    assert "条目=1" in vector_store.stats()


def test_faiss_matches_python_fallback(db, publish, run, monkeypatch):
    """FAISS（归一化内积）与 Python 余弦降级路径结果须完全一致（排序与分值）"""
    long_content = ("宅基地申请流程说明。"
                    + "申请人应当提交身份证明与申请表并向村委会提出书面申请备案公示。" * 60)
    run(publish(Q, CONTENT))
    run(publish("宅基地申请材料清单", long_content))
    qvec = run(llm.embed([Q]))[0]

    faiss_res = search.vector_search_sync(db, qvec)
    monkeypatch.setattr(vector_store, "search", lambda q: None)   # 强制走 Python 余弦
    py_res = search.vector_search_sync(db, qvec)

    assert [e for e, _ in faiss_res] == [e for e, _ in py_res]
    for (e1, s1), (e2, s2) in zip(faiss_res, py_res):
        assert abs(s1 - s2) < 1e-5


def test_faiss_dim_mismatch_returns_none(db, publish, run):
    run(publish(Q, CONTENT))
    assert vector_store.search([0.1] * 10) is None    # 维度不符 → None → 检索层自动降级


def test_faiss_excludes_expired_after_offline(db, publish, run):
    e = run(publish(Q, CONTENT, expire_date="2020-01-01"))
    qvec = run(llm.embed([Q]))[0]
    assert any(eid == e.id for eid, _ in (vector_store.search(qvec) or []))
    kb_ops.expire_entries(db)
    res = vector_store.search(qvec) or []
    assert all(eid != e.id for eid, _ in res)         # 下线后即时退出镜像
