"""问答管线回归（技术方案 5.1-5.4）：八环节、三态闭环、引用校验、未答聚合、降级兜底"""
import pytest

from app import llm, pipeline, search
from app.models import QaRecord, UnansweredGroup, UnansweredItem

Q = "农村宅基地申请流程"
CONTENT = "农村宅基地申请由村民向村委会提出书面申请，经村委会审核公示后报乡镇人民政府审批。"


def test_invalid_question_guard(db, staff, run):
    assert run(pipeline.run_ask(db, staff, "   "))["error"] == "invalid_question"
    assert run(pipeline.run_ask(db, staff, "超" * 501))["error"] == "invalid_question"


def test_sensitive_intercept_legal(db, staff, run):
    out = run(pipeline.run_ask(db, staff, "邻居欠钱不还，我想起诉他怎么办"))
    assert out["status"] == "out_of_scope"
    assert out["category"] == "涉法涉诉"
    assert "司法所" in out["advice"] or "12348" in out["advice"]
    rec = db.query(QaRecord).filter(QaRecord.status == "out_of_scope").one()
    assert rec.sensitive_category == "涉法涉诉"
    # ADR-2：敏感问题不进未答清单
    assert db.query(UnansweredItem).count() == 0


def test_sensitive_intercept_medical(db, staff, run):
    out = run(pipeline.run_ask(db, staff, "老人确诊了肺癌，治疗方案怎么选"))
    assert out["status"] == "out_of_scope"
    assert out["category"] == "医疗诊断"


def test_empty_kb_missed_with_referral(db, staff, run):
    """空知识库：正常受理、未答照常记录、必有兜底转办卡片"""
    out = run(pipeline.run_ask(db, staff, Q))
    assert out["status"] == "missed"
    assert out["related"] == []
    assert out["referral"]["org"] and out["referral"]["text"]
    assert out["disclaimer"]
    assert db.query(UnansweredGroup).count() == 1


def test_answered_flow_full_contract(db, staff, publish, run):
    e = run(publish(Q, CONTENT))
    out = run(pipeline.run_ask(db, staff, Q))
    assert out["status"] == "answered"
    assert out["answer"] and out["degraded"] is False
    # 引用四要素由服务端从 DB 组装（ADR-3：不信任模型复述）
    ids = [c["id"] for c in out["citations"]]
    assert ids and set(ids) <= {e.id}
    c0 = out["citations"][0]
    assert c0["title"] == e.title
    assert c0["publish_org"] == "县农业农村局"
    assert c0["basis_file"] and c0["snippet"]
    assert "2026-01-01" in out["timeliness"]
    # 落库口径
    rec = db.query(QaRecord).filter(QaRecord.status == "answered").one()
    assert rec.answer == out["answer"]
    assert rec.cited_ids() == ids
    assert rec.degraded == 0


def test_unrelated_question_missed_with_unanswered(db, staff, publish, run):
    run(publish(Q, CONTENT))
    out = run(pipeline.run_ask(db, staff, "量子力学中的测不准原理是说什么的"))
    assert out["status"] == "missed"
    assert out["message"]
    assert out["referral"]["org"]
    assert db.query(UnansweredGroup).count() == 1      # 未答记录照常聚合


def test_model_says_insufficient_goes_missed(db, staff, publish, run, monkeypatch):
    run(publish(Q, CONTENT))

    async def _insufficient(system_prompt, user_content):
        return {"answer": "", "cited_ids": [], "sufficient": False}
    monkeypatch.setattr(llm, "chat", _insufficient)
    out = run(pipeline.run_ask(db, staff, Q))
    assert out["status"] == "missed"                    # 尊重模型自判，转未命中兜底
    rec = db.query(QaRecord).filter(QaRecord.question == Q).one()
    assert rec.status == "missed"


def test_illegal_citation_retries_then_raises_and_never_persists(
        db, staff, publish, run, monkeypatch, no_sleep):
    run(publish(Q, CONTENT))
    calls = {"n": 0}

    async def _bad(system_prompt, user_content):
        calls["n"] += 1
        return {"answer": "答案内容", "cited_ids": [99999], "sufficient": True}
    monkeypatch.setattr(llm, "chat", _bad)

    with pytest.raises(pipeline.AskGenerationError):
        run(pipeline.run_ask(db, staff, Q))
    assert calls["n"] == 2                              # 失败退避后重试 1 次
    assert db.query(QaRecord).count() == 0              # 技术失败不留半成品


def test_strong_fts_rescues_low_vector_sim(db, staff, publish, run, monkeypatch):
    """向量相似度低于 T1 时，标题/标签强匹配应作为高相关旁证放行"""
    e = run(publish(Q, CONTENT))

    async def _fixed_vector(question):
        return [1.0] + [0.0] * 255                      # 与库内向量必然低相似
    monkeypatch.setattr(search, "embed_question", _fixed_vector)

    out = run(pipeline.run_ask(db, staff, "宅基地申请找谁办"))
    assert out["status"] == "answered"
    assert out["citations"][0]["id"] == e.id


def test_embedding_down_degrades_but_still_answers(db, staff, publish, run, monkeypatch):
    run(publish(Q, CONTENT))

    async def _none(question):
        return None                                     # 模拟问题向量化失败
    monkeypatch.setattr(search, "embed_question", _none)

    out = run(pipeline.run_ask(db, staff, Q))
    assert out["degraded"] is True
    assert out["status"] == "answered"                  # 降级纯关键词路仍可完成问答
    rec = db.query(QaRecord).filter(QaRecord.question == Q).one()
    assert rec.degraded == 1


def test_unanswered_aggregation_groups_similar(db, staff, run):
    run(pipeline.run_ask(db, staff, "新能源汽车补贴怎么申请"))
    run(pipeline.run_ask(db, staff, "新能源汽车补贴怎么申领"))
    run(pipeline.run_ask(db, staff, "村里的健身器材坏了找谁维修"))

    groups = db.query(UnansweredGroup).all()
    assert len(groups) == 2                             # 两个相似问并入同组，第三问独立成组
    assert sorted(g.question_count for g in groups) == [1, 2]
    items = db.query(UnansweredItem).count()
    assert items == 3


def test_long_entry_uses_chunk_excerpt(db, staff, publish, run):
    """超 6000 字条目走切片节选路径（防 Prompt 过长），仍能正常答出"""
    long_content = ("宅基地申请流程总体说明。" * 50
                    + "申请人需提交身份证明、申请书等材料并向村委会提出申请。" * 220)
    assert len(long_content) > pipeline.PROMPT_FULL_MAX
    e = run(publish(Q, long_content))
    out = run(pipeline.run_ask(db, staff, Q))
    assert out["status"] == "answered"
    assert out["citations"][0]["snippet"]                # 切片定位引用片段
    assert set(c["id"] for c in out["citations"]) <= {e.id}


def test_window_snippet_locates_keyword_dense_window():
    src = "开头占位文字。" * 40 + "宅基地申请的关键段落就在这里。" + "结尾占位文字。" * 40
    snip = pipeline._window_snippet(src, ["宅基地", "申请"])
    assert "宅基地" in snip
    assert len(snip) <= 165


def test_window_snippet_no_hit_takes_head():
    src = "平淡无奇的开头文字。" * 30
    snip = pipeline._window_snippet(src, ["不存在的关键词"])
    assert snip.startswith("平淡")
