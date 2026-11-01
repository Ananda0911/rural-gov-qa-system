"""问答 API：POST /api/ask（三态卡片 / 503 可重试）、记录反馈（仅本人已回答记录）"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..audit import log_action
from ..db import get_db
from ..models import QaRecord, now_str
from ..pipeline import AskGenerationError, run_ask
from ..security import api_user

router = APIRouter(prefix="/api")


class AskBody(BaseModel):
    question: str


class FeedbackBody(BaseModel):
    value: str  # useful / useless


@router.post("/ask")
async def ask(body: AskBody, user=Depends(api_user), db=Depends(get_db)):
    q = (body.question or "").strip()
    if not q or len(q) > 500:
        raise HTTPException(400, {"code": "invalid_question", "message": "问题不能为空且不超过 500 字"})
    try:
        result = await run_ask(db, user, q)
    except AskGenerationError:
        # 技术失败不落库，无半成品（5.4）
        return JSONResponse(status_code=503, content={
            "code": "llm_failed", "message": "生成失败，请稍后点击重试", "retryable": True})
    return result


@router.post("/records/{record_id}/feedback")
def feedback(record_id: int, body: FeedbackBody, user=Depends(api_user), db=Depends(get_db)):
    rec = db.get(QaRecord, record_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(403, {"code": "forbidden", "message": "只能评价自己的提问记录"})
    if rec.status != "answered":
        raise HTTPException(409, {"code": "not_answered", "message": "仅已回答的记录可评价"})
    if body.value not in ("useful", "useless"):
        raise HTTPException(400, {"code": "invalid_value", "message": "评价取值无效"})
    rec.feedback = body.value
    rec.feedback_at = now_str()
    log_action(db, user, "record_feedback", "qa_record", rec.id, {"value": body.value})
    db.commit()
    return {"ok": True, "feedback": body.value}
