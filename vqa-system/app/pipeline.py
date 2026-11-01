"""问答管线（技术方案 5.1-5.4）：一次提问最多 8 个环节，失败有明确出口，不留半成品
① 校验 → ② 敏感拦截（本地，先于一切 API 调用）→ ③ 混合检索 → ④ 相关性判定
→ ⑤ Prompt 组装 → ⑥ LLM 生成 → ⑦ 引用校验 → ⑧ 单事务落库
技术失败（超时/5xx/解析失败/引用非法）自动重试 1 次，仍失败抛 AskGenerationError（不落库）；
模型自认依据不足（sufficient=false）→ 尊重其判断，转未命中兜底（正常业务结果）。
"""
import asyncio
import json
import re

from sqlalchemy import text

from . import llm, search
from .config import settings
from .models import (KbChunk, KbEntry, QaRecord, ReferralMap, SensitiveRule,
                     UnansweredGroup, UnansweredItem, now_str)
from .seed import GENERIC_REFERRAL

SYSTEM_PROMPT = """你是村务政策问答助手，服务基层工作人员，回答口径供工作人员向村民复述。
严格规则：
1. 仅依据 <知识条目> 提供的内容作答，禁止使用条目以外的知识，禁止编造；
2. 条目内容不足以可靠回答时，必须将 sufficient 置为 false，answer 置为空字符串，并停止作答；
3. 答案分点、直接回应问题，不超过 500 字；
4. cited_ids 只能填写所提供条目的 id；
5. 只输出 JSON，不要输出任何其他文字：{"answer": "...", "cited_ids": [条目id...], "sufficient": true|false}"""

DISCLAIMER = "本回答不构成专业意见，具体请以正式文件与当面咨询为准。"


class AskGenerationError(Exception):
    """LLM 生成失败（已重试 1 次）：返回 503 retryable，不落库"""


# ---------- 敏感规则（内存缓存，P0 初始 4 类） ----------
_RULE_CACHE: list[dict] | None = None


def _load_rules(db) -> list[dict]:
    global _RULE_CACHE
    if _RULE_CACHE is None:
        rules = []
        for r in db.query(SensitiveRule).filter(SensitiveRule.enabled == 1).all():
            try:
                patterns = [re.compile(p) for p in json.loads(r.patterns)]
            except Exception:
                continue
            rules.append({"name": r.name, "advice": r.advice, "patterns": patterns})
        _RULE_CACHE = rules
    return _RULE_CACHE


def check_sensitive(db, question: str):
    """命中返回 {name, advice}；不命中返回 None（ADR-2：敏感不出域、不进未答清单）"""
    for rule in _load_rules(db):
        for pat in rule["patterns"]:
            if pat.search(question):
                return rule
    return None


# ---------- 未答聚合（5.5） ----------
def _aggregate_unanswered(db, question: str, qvec, qa_record_id: int) -> None:
    """与待处理组内全部实例求平均余弦，≥ T2 并入，否则新建组（无向量时各自成组）"""
    if qvec is not None:
        best_group, best_sim = None, -1.0
        groups = db.query(UnansweredGroup).filter(UnansweredGroup.status == "pending").all()
        for g in groups:
            items = (db.query(UnansweredItem)
                     .filter(UnansweredItem.group_id == g.id, UnansweredItem.embedding.isnot(None))
                     .all())
            if not items:
                continue
            sims = [search.cosine(qvec, search.unpack_vec(it.embedding)) for it in items]
            avg = sum(sims) / len(sims)
            if avg > best_sim:
                best_group, best_sim = g, avg
        if best_group is not None and best_sim >= settings.t2_aggregate:
            db.add(UnansweredItem(group_id=best_group.id, question=question,
                                  qa_record_id=qa_record_id, embedding=search.pack_vec(qvec)))
            best_group.question_count += 1
            best_group.last_asked_at = now_str()
            return
    g = UnansweredGroup(representative_question=question, question_count=1, status="pending")
    db.add(g)
    db.flush()
    db.add(UnansweredItem(group_id=g.id, question=question, qa_record_id=qa_record_id,
                          embedding=search.pack_vec(qvec) if qvec else None))


# ---------- 兜底 ----------
def _referral(db, category: str | None) -> dict:
    if category:
        row = db.get(ReferralMap, category)
        if row:
            return {"org": row.org, "text": row.text}
    return GENERIC_REFERRAL


def _related_entries(db, candidates: list[dict]) -> list[dict]:
    out = []
    for c in candidates[:3]:
        e = db.get(KbEntry, c["id"])
        if e:
            out.append({"id": e.id, "title": e.title, "category": e.category,
                        "publish_org": e.publish_org})
    return out


def _persist_missed(db, user, question, candidates, qvec, degraded) -> dict:
    """未命中 = 正常业务结果：单事务落库 + 同事务生成未答实例并聚合 + 兜底卡片"""
    rec = QaRecord(user_id=user.id, question=question, status="missed",
                   degraded=1 if degraded else 0)
    db.add(rec)
    db.flush()
    _aggregate_unanswered(db, question, qvec, rec.id)
    category = None
    if candidates:
        top = db.get(KbEntry, candidates[0]["id"])
        category = top.category if top else None
    related = _related_entries(db, candidates)
    db.commit()
    return {"status": "missed", "record_id": rec.id,
            "message": "暂无可靠答案（知识库中暂无与该问题高度相关的依据条目）",
            "related": related, "referral": _referral(db, category),
            "degraded": degraded, "disclaimer": DISCLAIMER}


# ---------- 切片定位（方案 B：长文节选 + 引用片段） ----------
PROMPT_FULL_MAX = 6000  # 条目超过此字数时仅送命中切片节选，防 Prompt 过长


def _best_chunks(db, entry_id: int, qvec, tokens: list[str], take: int = 1, neighbors: int = 1) -> list:
    """定位条目中与问题最相关的切片及相邻切片：qvec 余弦为主，
    叠加关键词重叠小系数校正（降低长切片内容稀释导致的定位漂移）；
    降级/维度不符时退化为纯关键词重叠"""
    chunks = (db.query(KbChunk).filter(KbChunk.entry_id == entry_id)
              .order_by(KbChunk.chunk_index).all())
    if not chunks:
        return []
    q = set(tokens or [])
    scored: list[tuple[float, int]] = []
    if qvec is not None:
        for i, c in enumerate(chunks):
            if c.dim != len(qvec):
                continue
            sim = search.cosine(qvec, search.unpack_vec(c.embedding))
            sim += 0.05 * len(set(search.jieba_tokens(c.chunk_text)) & q)
            scored.append((sim, i))
    if not scored:
        scored = [(float(len(set(search.jieba_tokens(c.chunk_text)) & q)), i)
                  for i, c in enumerate(chunks)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: set[int] = set()
    for _s, i in scored[:take]:
        for j in range(max(0, i - neighbors), min(len(chunks), i + neighbors + 1)):
            picked.add(j)
    return [chunks[j] for j in sorted(picked)]


def _window_snippet(src: str, tokens: list[str]) -> str:
    """在文本内定位问题关键词最密集的 160 字窗口；无命中时取开头"""
    flat = re.sub(r"\s+", " ", src).strip()
    if not flat:
        return ""
    qs = {t for t in (tokens or []) if len(t) >= 2 and t in flat}
    if not qs:
        return flat[:160] + ("…" if len(flat) > 160 else "")
    best_start, best_hits = 0, -1
    for t in qs:
        start = max(0, flat.find(t) - 40)
        hits = sum(1 for x in qs if x in flat[start:start + 160])
        if hits > best_hits or (hits == best_hits and start < best_start):
            best_start, best_hits = start, hits
    snip = flat[best_start:best_start + 160]
    return ("…" if best_start > 0 else "") + snip + ("…" if best_start + 160 < len(flat) else "")


def citation_snippet(db, entry: KbEntry, qvec, tokens: list[str]) -> str:
    """引用片段：定位命中切片内关键词最密集的 160 字窗口（条目无切片时退回正文定位）"""
    hits = _best_chunks(db, entry.id, qvec, tokens, take=1, neighbors=0)
    src = hits[0].chunk_text if hits else (entry.content or "")
    return _window_snippet(src, tokens)


# ---------- 管线主体 ----------
async def run_ask(db, user, question: str) -> dict:
    """返回三种业务结果卡片之一；技术失败抛 AskGenerationError"""
    # ① 校验（路由已做，此处兜底）
    question = question.strip()
    if not question or len(question) > 500:
        return {"error": "invalid_question"}

    # ② 敏感拦截：先于一切 API 调用
    hit = check_sensitive(db, question)
    if hit:
        rec = QaRecord(user_id=user.id, question=question, status="out_of_scope",
                       sensitive_category=hit["name"])
        db.add(rec)
        db.commit()
        return {"status": "out_of_scope", "record_id": rec.id,
                "category": hit["name"], "advice": hit["advice"], "disclaimer": DISCLAIMER}

    # ③ 混合检索（候选集恒为已发布未过期，ADR-4）
    retrieved = await search.hybrid_retrieve(db, question)
    candidates = retrieved["candidates"]
    qvec, degraded = retrieved["qvec"], retrieved["degraded"]

    has_published = db.execute(
        text("SELECT COUNT(*) FROM kb_entries e WHERE e.status='published' "
             "AND (e.expire_date IS NULL OR e.expire_date > :t)"),
        {"t": now_str()[:10]},
    ).scalar()

    if not has_published:
        # 知识库为空 → 直达未命中兜底（空态提问照常受理，未答照常记录并聚合）
        return _persist_missed(db, user, question, [], qvec, degraded)

    # ④ 相关性判定：Top1 向量相似度 ≥ T1，或标题/标签强匹配
    high = False
    if candidates:
        best = candidates[0]
        if best["sim"] is not None and best["sim"] >= settings.t1_relevance:
            high = True
        else:
            e = db.get(KbEntry, best["id"])
            if e and search.strong_fts_match(db, e, retrieved["tokens"]):
                high = True
    if not high:
        return _persist_missed(db, user, question, candidates, qvec, degraded)

    # ⑤ Prompt 组装（Top 3，低相关干扰不送；超长条目仅送命中切片及相邻各 1 片）
    entries = []
    for c in candidates[:3]:
        e = db.get(KbEntry, c["id"])
        if e:
            entries.append(e)
    if not entries:
        return _persist_missed(db, user, question, candidates, qvec, degraded)
    user_content = f"问题：{question}\n"
    for e in entries:
        if len(e.content) > PROMPT_FULL_MAX:
            hits = _best_chunks(db, e.id, qvec, retrieved["tokens"])
            body = ("（节选）\n" + "\n".join(c.chunk_text for c in hits)) if hits else e.content[:PROMPT_FULL_MAX]
        else:
            body = e.content
        user_content += f"\n<知识条目 id={e.id}>\n标题：{e.title}\n正文：{body}\n</知识条目>\n"
    provided_ids = {e.id for e in entries}

    # ⑥+⑦ 生成与引用校验：技术失败退避 2s 重试 1 次
    parsed = None
    insufficient = False
    last_err = None
    for attempt in (1, 2):
        try:
            data = await llm.chat(SYSTEM_PROMPT, user_content)
            answer = str(data.get("answer") or "").strip()
            cited = data.get("cited_ids") or []
            cited = [int(x) for x in cited] if isinstance(cited, list) else []
            sufficient = data.get("sufficient") is True
            if not sufficient or not answer:
                insufficient = True  # 模型自认依据不足 → 转未命中兜底
                break
            illegal = [x for x in cited if x not in provided_ids]
            if illegal:
                raise llm.LLMError(f"引用了未提供的条目: {illegal}")
            if not cited:
                cited = [entries[0].id]  # 未标注引用时以首条兜底
            parsed = {"answer": answer, "cited": cited}
            break
        except (llm.LLMError, ValueError, TypeError) as e:
            last_err = e
            if attempt == 1:
                await asyncio.sleep(2)
    if insufficient:
        return _persist_missed(db, user, question, candidates, qvec, degraded)
    if parsed is None:
        raise AskGenerationError(str(last_err) if last_err else "生成失败")

    # 引用四要素由服务端从 DB 组装（ADR-3：不信任模型复述）
    citations, latest_verified = [], ""
    for cid in parsed["cited"]:
        e = db.get(KbEntry, cid)
        if e:
            citations.append({"id": e.id, "title": e.title, "category": e.category,
                              "publish_org": e.publish_org, "basis_file": e.basis_file,
                              "last_verified_at": e.last_verified_at or "",
                              "snippet": citation_snippet(db, e, qvec, retrieved["tokens"])})
            if (e.last_verified_at or "") > latest_verified:
                latest_verified = e.last_verified_at or ""

    # ⑧ 单事务落库
    rec = QaRecord(user_id=user.id, question=question, answer=parsed["answer"], status="answered",
                   cited_entry_ids=json.dumps(parsed["cited"]),
                   degraded=1 if degraded else 0)
    db.add(rec)
    db.commit()
    return {"status": "answered", "record_id": rec.id, "answer": parsed["answer"],
            "citations": citations,
            "timeliness": f"答案依据条目最后核实时间：{latest_verified}" if latest_verified else "",
            "degraded": degraded, "disclaimer": DISCLAIMER}
