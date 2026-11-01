"""检索层回归：分词、FTS 表达式、向量编解码、双路召回、RRF 融合、降级与强匹配旁证"""
import pytest

from app import kb_ops, llm, search
from app.models import KbEntry

Q = "农村宅基地申请流程"
CONTENT = "农村宅基地申请由村民向村委会提出书面申请，经村委会审核公示后报乡镇人民政府审批。"


def test_jieba_tokens_filters_stopwords_and_short():
    toks = search.jieba_tokens("宅基地的申请流程是什么")
    assert "的" not in toks and "是" not in toks and "什么" not in toks
    assert all(len(t) >= 2 for t in toks)
    assert len(toks) == len(set(toks))          # 去重保序


def test_jieba_tokens_keeps_meaningful_words():
    toks = search.jieba_tokens("农村合作医疗缴费标准")
    assert any("医疗" in t for t in toks)
    assert any("缴费" in t for t in toks)


def test_fts_expr_quotes_truncates_and_skips_embedded_quote():
    parts = search.fts_expr([f"词{i}" for i in range(15)]).split(" OR ")
    assert len(parts) == 12                      # 截断至 12 个
    assert all(p.startswith('"') and p.endswith('"') for p in parts)
    assert search.fts_expr(['含"引号', "正常"]) == '"正常"'   # 含引号的词跳过防注入


def test_pack_unpack_roundtrip():
    v = [0.123456, -0.789, 3.14]
    out = search.unpack_vec(search.pack_vec(v))
    assert len(out) == 3
    assert all(abs(a - b) < 1e-6 for a, b in zip(v, out))


def test_cosine_identical_orthogonal_and_dim_mismatch():
    assert abs(search.cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(search.cosine([1, 0], [0, 1])) < 1e-9
    assert search.cosine([1, 0], [1, 0, 0]) == -1.0    # 维度不符 → -1


def test_fts_filters_draft_and_expired_even_if_index_leftover(db, publish, run):
    """ADR-4 双保险：即使索引中残留未发布/过期条目，检索 SQL 也必须过滤"""
    e_pub = run(publish(Q, CONTENT))
    d = KbEntry(title="宅基地申请流程草案", category="办事流程", content=CONTENT,
                basis_file="x.pdf", publish_org="y", status="draft")
    x = KbEntry(title="宅基地申请流程旧版", category="办事流程", content=CONTENT,
                basis_file="x.pdf", publish_org="y", status="published", expire_date="2020-01-01")
    db.add_all([d, x])
    db.flush()
    kb_ops.fts_upsert(db, d)          # 故意写入索引，模拟「索引未同步清理」残留
    kb_ops.fts_upsert(db, x)
    db.commit()

    ids = search.fts_search(db, search.jieba_tokens(Q))
    assert e_pub.id in ids
    assert d.id not in ids and x.id not in ids


def test_vector_search_filters_unpublished(db, publish, run):
    e_pub = run(publish(Q, CONTENT))
    qvec = run(llm.embed([Q]))[0]
    res = dict(search.vector_search_sync(db, qvec))
    assert e_pub.id in res
    # 草稿条目即使向量索引残留也不可召回
    d = KbEntry(title="宅基地申请流程草案", category="办事流程", content=CONTENT,
                basis_file="x.pdf", publish_org="y", status="draft")
    db.add(d)
    db.flush()
    run(kb_ops.vector_upsert(db, d))
    db.commit()
    res2 = dict(search.vector_search_sync(db, qvec))
    assert d.id not in res2 and e_pub.id in res2


def test_hybrid_retrieve_top_candidate_and_fusion(db, publish, run):
    e = run(publish(Q, CONTENT))
    r = run(search.hybrid_retrieve(db, Q))
    assert r["degraded"] is False and r["qvec"] is not None
    assert r["candidates"], "候选不应为空"
    assert r["candidates"][0]["id"] == e.id
    assert r["candidates"][0]["sim"] is not None      # 双路至少一向给出相似度


def test_vector_path_reaches_t1_with_short_entry(db, publish, run):
    """向量链路正确性：条目全文与问题高度一致时，向量路相似度须显著高于 T1 校准值 0.55
    （mock 词袋向量下长正文会稀释相似度，此处用短条目隔离验证向量计算链路本身）"""
    e = run(publish(Q, Q))
    r = run(search.hybrid_retrieve(db, Q))
    assert r["candidates"][0]["id"] == e.id
    assert r["candidates"][0]["sim"] >= 0.75


def test_hybrid_retrieve_degrades_to_keyword_only(db, publish, run, monkeypatch):
    async def _down(texts):
        raise llm.LLMError("embedding 服务不可用")
    monkeypatch.setattr(llm, "embed", _down)
    e = run(publish(Q, CONTENT))      # 向量化失败 → 条目无向量索引（符合降级设计）
    r = run(search.hybrid_retrieve(db, Q))
    assert r["degraded"] is True and r["qvec"] is None
    assert r["candidates"][0]["id"] == e.id           # 纯关键词路仍可用


def test_strong_fts_match_by_title_overlap(db, publish, run):
    e = run(publish(Q, "正文为与问题无关的占位说明文字" * 10))
    assert search.strong_fts_match(db, e, search.jieba_tokens("农村宅基地申请")) is True
    assert search.strong_fts_match(db, e, search.jieba_tokens("低保复核")) is False


def test_strong_fts_match_requires_min_coverage():
    class Fake:
        title = "城乡低保动态管理"
        content = "复核流程说明"
        def tags_list(self):
            return []
    # 全文覆盖路径：3 个词全覆盖（≥3 且 ≥40%）→ 旁证成立
    assert search.strong_fts_match(None, Fake(), ["低保", "复核", "管理"]) is True
    # 仅 1 个重叠词既不够标题阈值也不够覆盖阈值 → 不误报
    assert search.strong_fts_match(None, Fake(), ["低保"]) is False
