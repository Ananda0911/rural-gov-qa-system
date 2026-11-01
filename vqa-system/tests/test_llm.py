"""LLM 客户端回归（Mock 模式）：JSON 容错解析、演示向量、演示生成三态口径"""
import pytest

from app import llm
from app.search import cosine


def test_extract_json_plain():
    assert llm._extract_json('{"answer": "x", "cited_ids": [1], "sufficient": true}') == {
        "answer": "x", "cited_ids": [1], "sufficient": True}


def test_extract_json_strips_code_fence():
    s = "```json\n{\"answer\": \"分点回答\", \"cited_ids\": [1], \"sufficient\": true}\n```"
    out = llm._extract_json(s)
    assert out["answer"] == "分点回答" and out["sufficient"] is True


def test_extract_json_with_surrounding_noise():
    assert llm._extract_json('好的，以下是结果：{"a": {"b": 2}} 以上。') == {"a": {"b": 2}}


def test_extract_json_rejects_non_json():
    with pytest.raises(llm.LLMError):
        llm._extract_json("抱歉，我没有办法输出 JSON。")


def test_mock_embed_dimension_and_normalized():
    v = llm._mock_embed_one("农村宅基地申请流程")
    assert len(v) == 256
    assert abs(sum(x * x for x in v) ** 0.5 - 1.0) < 1e-6   # 归一化


def test_mock_embed_topic_similarity_ordering():
    """同话题近改写相似度须显著高于无关文本，且达到 T2 聚合阈值口径（0.82）
    （Mock 词袋向量的已知特性：换虚词的改写相似度偏低，近义实词改写才稳定过 T2，
      真实 embedding 服务下同义改写相似度会更高，T2 口径为真实模型校准）"""
    q = llm._mock_embed_one("新能源汽车补贴怎么申请")
    same = llm._mock_embed_one("新能源汽车补贴怎么申领")
    other = llm._mock_embed_one("今天天气晴朗适合晒被子")
    assert cosine(q, same) > cosine(q, other)
    assert cosine(q, same) >= 0.82


def test_mock_chat_with_entry():
    content = ("问题：宅基地怎么申请\n\n"
               "<知识条目 id=1>\n标题：宅基地申请流程\n正文：村民向村委会提出书面申请。\n</知识条目>\n")
    out = llm._mock_chat(content)
    assert out["sufficient"] is True
    assert out["cited_ids"] == [1]
    assert "宅基地申请流程" in out["answer"]


def test_mock_chat_without_entry_refuses():
    out = llm._mock_chat("问题：随便问点什么\n（无知识条目）")
    assert out == {"answer": "", "cited_ids": [], "sufficient": False}


def test_chat_and_embed_public_api_use_mock():
    """LLM_MOCK=1 时对外接口走本地模拟，不出域"""
    import asyncio
    vecs = asyncio.run(llm.embed(["测试文本一", "测试文本二"]))
    assert len(vecs) == 2 and all(len(v) == 256 for v in vecs)
    out = asyncio.run(llm.chat("sys", "问题：x\n<知识条目 id=1>\n标题：t\n正文：b\n</知识条目>\n"))
    assert out["sufficient"] is True
