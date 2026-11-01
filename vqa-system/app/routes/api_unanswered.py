"""未答问题 API（FR-04）：组列表 / 组详情 / 手动拆分 / 关闭（原因必填）"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..audit import log_action
from ..db import get_db
from ..models import KbEntry, UnansweredGroup, UnansweredItem, now_str
from ..security import api_admin

router = APIRouter(prefix="/api/unanswered", dependencies=[Depends(api_admin)])


class SplitBody(BaseModel):
    item_ids: list[int]


class CloseBody(BaseModel):
    reason: str


def _group_dict(db, g: UnansweredGroup) -> dict:
    related = None
    if g.related_entry_id:
        e = db.get(KbEntry, g.related_entry_id)
        if e:
            related = {"id": e.id, "title": e.title}
    return {"id": g.id, "representative_question": g.representative_question,
            "question_count": g.question_count, "status": g.status,
            "related_entry": related, "close_reason": g.close_reason or "",
            "first_asked_at": g.first_asked_at, "last_asked_at": g.last_asked_at}


@router.get("/groups")
def list_groups(status: str = "", page: int = 1, db=Depends(get_db)):
    query = db.query(UnansweredGroup)
    if status:
        query = query.filter(UnansweredGroup.status == status)
    total = query.count()
    rows = (query.order_by(UnansweredGroup.last_asked_at.desc())
            .offset((max(page, 1) - 1) * 20).limit(20).all())
    return {"items": [_group_dict(db, g) for g in rows], "total": total, "page": max(page, 1)}


@router.get("/groups/{group_id}")
def group_detail(group_id: int, db=Depends(get_db)):
    g = db.get(UnansweredGroup, group_id)
    if not g:
        raise HTTPException(404, {"code": "not_found", "message": "问题组不存在"})
    items = (db.query(UnansweredItem).filter(UnansweredItem.group_id == group_id)
             .order_by(UnansweredItem.created_at.desc()).all())
    return {"group": _group_dict(db, g),
            "items": [{"id": i.id, "question": i.question, "qa_record_id": i.qa_record_id,
                       "created_at": i.created_at} for i in items]}


@router.post("/groups/{group_id}/split")
def split_group(group_id: int, body: SplitBody, user=Depends(api_admin), db=Depends(get_db)):
    g = db.get(UnansweredGroup, group_id)
    if not g:
        raise HTTPException(404, {"code": "not_found", "message": "问题组不存在"})
    if g.status != "pending":
        raise HTTPException(409, {"code": "not_pending", "message": "仅待处理组可拆分"})
    items = db.query(UnansweredItem).filter(UnansweredItem.group_id == group_id).all()
    item_map = {i.id: i for i in items}
    picked = [item_map[i] for i in body.item_ids if i in item_map]
    if not picked:
        raise HTTPException(400, {"code": "no_items", "message": "请勾选要拆出的问题"})
    if len(picked) >= len(items):
        raise HTTPException(400, {"code": "cannot_split_all", "message": "不能拆出全部问题，原组至少保留一条"})
    times = [i.created_at for i in picked]
    new_g = UnansweredGroup(representative_question=picked[0].question,
                            question_count=len(picked), status="pending",
                            first_asked_at=min(times), last_asked_at=max(times))
    db.add(new_g)
    db.flush()
    for it in picked:
        it.group_id = new_g.id
    g.question_count -= len(picked)
    log_action(db, user, "unanswered_split", "unanswered_group", group_id,
               {"to_group": new_g.id, "item_count": len(picked)})
    db.commit()
    return {"ok": True, "new_group_id": new_g.id}


@router.post("/groups/{group_id}/close")
def close_group(group_id: int, body: CloseBody, user=Depends(api_admin), db=Depends(get_db)):
    reason = (body.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, {"code": "reason_required", "message": "关闭原因必填"})
    g = db.get(UnansweredGroup, group_id)
    if not g:
        raise HTTPException(404, {"code": "not_found", "message": "问题组不存在"})
    if g.status != "pending":
        raise HTTPException(409, {"code": "not_pending", "message": "仅待处理组可关闭"})
    g.status = "closed"
    g.close_reason = reason
    g.handled_by, g.handled_at = user.id, now_str()
    log_action(db, user, "unanswered_close", "unanswered_group", g.id,
               {"question": g.representative_question, "reason": reason})
    db.commit()
    return {"ok": True}
