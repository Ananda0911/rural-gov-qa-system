"""大模型客户端：OpenAI 兼容协议（httpx 异步直连），LLM_MOCK=1 时本地模拟
- chat：严格 JSON 输出 {answer, cited_ids, sufficient}，解析失败抛 LLMError（可重试）
- embed：文本 → 向量；问题与条目共用同一模型保证相似度可比较
- 不内置重试：由管线层统一做「失败 → 退避 2s → 重试 1 次」
"""
import hashlib
import json
import math
import re

import httpx

from .config import settings

MOCK_DIM = 256


class LLMError(Exception):
    """技术失败（网络/5xx/超时/解析失败），可重试"""


# ================= 演示模式（本地模拟，不出域） =================
def _mock_embed_one(text: str) -> list[float]:
    """本地演示向量：jieba 词袋 + 字符二元组哈希（同义改写相似度高于纯词袋，供三态闭环演示）"""
    import jieba
    vec = [0.0] * MOCK_DIM

    def add(tok: str, w: float) -> None:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % MOCK_DIM] += w

    for tok in jieba.lcut_for_search(text):
        tok = tok.strip()
        if len(tok) >= 2:
            add(tok, 1.0)
    clean = text.replace(" ", "").replace("？", "").replace("?", "")
    for i in range(len(clean) - 1):
        add(clean[i:i + 2], 0.5)  # 字符二元组：同话题改写仍能保持较高重叠
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


_ENTRY_RE = re.compile(r"<知识条目 id=(\d+)>\n标题：(.+?)\n正文：(.+?)(?:\n</知识条目>|$)", re.S)


def _mock_chat(user_content: str) -> dict:
    blocks = _ENTRY_RE.findall(user_content)
    if not blocks:
        return {"answer": "", "cited_ids": [], "sufficient": False}
    top = blocks[0]
    cited = [int(b[0]) for b in blocks[:2]]
    content_brief = top[2].strip()[:180]
    answer = (
        f"依据《{top[1]}》口径（演示环境模拟生成）：\n"
        f"{content_brief}\n"
        "如需办理，请按上述流程准备材料，携带相关证件前往对应窗口办理。"
    )
    return {"answer": answer, "cited_ids": cited, "sufficient": True}


# ================= 真实 API（OpenAI 兼容） =================
def _client(timeout_read: float) -> httpx.AsyncClient:
    if not settings.llm_base_url:
        raise LLMError("LLM_BASE_URL 未配置")
    return httpx.AsyncClient(
        base_url=settings.llm_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=httpx.Timeout(connect=5.0, read=timeout_read, write=10.0, pool=5.0),
    )


async def _post(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    resp = await client.post(path, json=payload)
    if resp.status_code >= 500:
        raise LLMError(f"上游 5xx: {resp.status_code}")
    if resp.status_code != 200:
        raise LLMError(f"上游拒绝: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _extract_json(text: str) -> dict:
    """容错解析：剥离代码围栏，截取首尾大括号"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"输出非 JSON: {text[:100]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}") from e


# ================= 对外接口 =================
async def embed(texts: list[str]) -> list[list[float]]:
    """批量文本转向量；服务商单批有条数上限，内部按 10 条分批；任一失败抛 LLMError（调用方降级）"""
    if settings.llm_mock:
        return [_mock_embed_one(t) for t in texts]
    out: list[list[float]] = []
    try:
        async with _client(15.0) as client:
            for i in range(0, len(texts), 10):
                chunk = texts[i:i + 10]
                data = await _post(client, "/embeddings", {
                    "model": settings.llm_embed_model,
                    "input": chunk,
                })
                items = sorted(data["data"], key=lambda x: x["index"])
                vectors = [item["embedding"] for item in items]
                if len(vectors) != len(chunk) or any(not v for v in vectors):
                    raise LLMError("embedding 返回数量与输入不符")
                out.extend(vectors)
        return out
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"embedding 调用失败: {e}") from e


async def chat(system_prompt: str, user_content: str) -> dict:
    """生成 → 解析为 {answer, cited_ids, sufficient}；失败抛 LLMError"""
    if settings.llm_mock:
        return _mock_chat(user_content)
    try:
        async with _client(30.0) as client:
            data = await _post(client, "/chat/completions", {
                "model": settings.llm_chat_model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            })
            content = data["choices"][0]["message"]["content"]
            return _extract_json(content)
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"chat 调用失败: {e}") from e
