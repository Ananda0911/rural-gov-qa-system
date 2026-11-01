"""知识库 API（FR-03）：列表 / 新增草稿 / 编辑（不改状态）/ 发布（同步索引+闭环）/ 下线（原因必填）"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..audit import log_action
from ..config import settings
from ..db import get_db
from ..kb_ops import check_similar_titles, fts_delete, fts_upsert, vector_upsert
from ..models import CATEGORIES, KbEntry, KbFile, UnansweredGroup, now_str
from ..security import api_admin

router = APIRouter(prefix="/api/kb", dependencies=[Depends(api_admin)])


class KbBody(BaseModel):
    title: str
    category: str
    content: str
    basis_file: str
    publish_org: str
    expire_date: str | None = ""
    tags: list[str] = []
    source_group_id: int | None = None
    force: bool = False  # 相似标题确认后强制保存


def _validate(body: KbBody) -> None:
    if not (body.title or "").strip() or len(body.title) > 200:
        raise HTTPException(400, {"code": "invalid_title", "message": "标题必填（≤200 字）"})
    if body.category not in CATEGORIES:
        raise HTTPException(400, {"code": "invalid_category", "message": "分类取值无效"})
    for field, name in ((body.content, "正文"), (body.basis_file, "依据文件名"), (body.publish_org, "发布单位")):
        if not (field or "").strip():
            raise HTTPException(400, {"code": "missing_field", "message": f"{name} 必填（无依据不入库）"})
    if body.expire_date:
        try:
            datetime.strptime(body.expire_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, {"code": "invalid_expire", "message": "失效日期格式应为 YYYY-MM-DD"})


def _entry_dict(e: KbEntry, db=None) -> dict:
    file = None
    if db is not None:  # 归档原件元数据（无则 None；下载见 GET /{entry_id}/file）
        kf = (db.query(KbFile).filter(KbFile.entry_id == e.id)
              .order_by(KbFile.id.desc()).first())
        if kf:
            file = {"id": kf.id, "orig_filename": kf.orig_filename,
                    "size": kf.size, "uploaded_at": kf.uploaded_at}
    return {"id": e.id, "title": e.title, "category": e.category, "content": e.content,
            "basis_file": e.basis_file, "publish_org": e.publish_org,
            "expire_date": e.expire_date or "", "tags": e.tags_list(), "status": e.status,
            "source_group_id": e.source_group_id, "last_verified_at": e.last_verified_at or "",
            "offline_reason": e.offline_reason or "",
            "created_at": e.created_at, "updated_at": e.updated_at, "file": file}


@router.get("")
def list_entries(status: str = "", q: str = "", page: int = 1, db=Depends(get_db)):
    query = db.query(KbEntry)
    if status:
        query = query.filter(KbEntry.status == status)
    if q:
        query = query.filter(KbEntry.title.contains(q.strip()))
    total = query.count()
    rows = (query.order_by(KbEntry.updated_at.desc())
            .offset((max(page, 1) - 1) * 20).limit(20).all())
    return {"items": [_entry_dict(e, db) for e in rows], "total": total, "page": max(page, 1)}


@router.get("/{entry_id}")
def get_entry(entry_id: int, db=Depends(get_db)):
    e = db.get(KbEntry, entry_id)
    if not e:
        raise HTTPException(404, {"code": "not_found", "message": "条目不存在"})
    return _entry_dict(e, db)


@router.get("/{entry_id}/file")
def download_entry_file(entry_id: int, db=Depends(get_db)):
    """下载条目归档的原始文件（路由级 api_admin 已限制仅管理员）；
    存储名由系统生成，仍双保险校验路径字符防注入"""
    kf = (db.query(KbFile).filter(KbFile.entry_id == entry_id)
          .order_by(KbFile.id.desc()).first())
    if not kf:
        raise HTTPException(404, {"code": "no_file", "message": "该条目未归档原始文件"})
    if "/" in kf.stored_name or "\\" in kf.stored_name:
        raise HTTPException(400, {"code": "bad_stored_name", "message": "存储文件名非法"})
    path = Path(settings.upload_dir) / kf.stored_name
    if not path.is_file():
        raise HTTPException(404, {"code": "file_missing",
                                  "message": "归档文件已缺失，请检查服务器 data/uploads 目录"})
    return FileResponse(path, filename=kf.orig_filename)


async def create_draft(db, user, body: KbBody, via: str = "") -> dict:
    """新建草稿公共路径（手工新建 / 文档导入共用）：标题唯一 + 相似标题闭环 + 留痕"""
    _validate(body)
    if db.query(KbEntry).filter(KbEntry.title == body.title.strip()).first():
        raise HTTPException(409, {"code": "title_exists", "message": "标题已存在（数据库层唯一兜底）"})
    similar = await check_similar_titles(db, body.title.strip())
    if similar and not body.force:
        raise HTTPException(409, {"code": "similar_titles",
                                   "message": "检测到与已发布条目标题高度相似，是否为原条目的更新版本？",
                                   "similar_to": similar})
    e = KbEntry(title=body.title.strip(), category=body.category, content=body.content.strip(),
                basis_file=body.basis_file.strip(), publish_org=body.publish_org.strip(),
                expire_date=body.expire_date or None,
                tags=json.dumps([t.strip() for t in body.tags if t.strip()], ensure_ascii=False),
                source_group_id=body.source_group_id, created_by=user.id, updated_by=user.id)
    db.add(e)
    db.flush()  # 取得自增 id，保证留痕 object_id 完整
    log_action(db, user, "kb_create", "kb_entry", e.id,
               {"title": e.title, "via": via} if via else {"title": e.title})
    db.commit()
    return {"id": e.id, "similar_to": similar}


@router.post("")
async def create_entry(body: KbBody, user=Depends(api_admin), db=Depends(get_db)):
    return await create_draft(db, user, body)


@router.put("/{entry_id}")
async def update_entry(entry_id: int, body: KbBody, user=Depends(api_admin), db=Depends(get_db)):
    _validate(body)
    e = db.get(KbEntry, entry_id)
    if not e:
        raise HTTPException(404, {"code": "not_found", "message": "条目不存在"})
    dup = db.query(KbEntry).filter(KbEntry.title == body.title.strip(), KbEntry.id != entry_id).first()
    if dup:
        raise HTTPException(409, {"code": "title_exists", "message": "标题已存在"})
    similar = await check_similar_titles(db, body.title.strip(), exclude_id=entry_id)
    if similar and not body.force:
        raise HTTPException(409, {"code": "similar_titles",
                                   "message": "检测到与已发布条目标题高度相似，是否为原条目的更新版本？",
                                   "similar_to": similar})
    e.title, e.category, e.content = body.title.strip(), body.category, body.content.strip()
    e.basis_file, e.publish_org = body.basis_file.strip(), body.publish_org.strip()
    e.expire_date = body.expire_date or None
    e.tags = json.dumps([t.strip() for t in body.tags if t.strip()], ensure_ascii=False)
    e.updated_by, e.updated_at = user.id, now_str()
    # 编辑不改状态；published 保持 published 并刷新核实时间（引用卡来源）
    if e.status == "published":
        e.last_verified_at = now_str()
        fts_upsert(db, e)
        await vector_upsert(db, e)
    log_action(db, user, "kb_edit", "kb_entry", e.id, {"title": e.title})
    db.commit()
    return {"id": e.id}


@router.post("/{entry_id}/publish")
async def publish_entry(entry_id: int, user=Depends(api_admin), db=Depends(get_db)):
    e = db.get(KbEntry, entry_id)
    if not e:
        raise HTTPException(404, {"code": "not_found", "message": "条目不存在"})
    if e.status == "published":
        raise HTTPException(409, {"code": "already_published", "message": "条目已发布"})
    e.status = "published"
    e.last_verified_at = now_str()
    e.updated_by, e.updated_at = user.id, now_str()
    fts_upsert(db, e)                       # 索引与状态同一事务，即时生效
    vec_ok = await vector_upsert(db, e)     # embedding 失败：仍发布，向量路暂缺（降级设计）
    closed = None
    if e.source_group_id:                   # 发布闭环：未答组 → 已补充，双向关联
        g = db.get(UnansweredGroup, e.source_group_id)
        if g and g.status == "pending":
            g.status, g.related_entry_id = "supplemented", e.id
            g.handled_by, g.handled_at = user.id, now_str()
            closed = g.id
    log_action(db, user, "kb_publish", "kb_entry", e.id,
               {"title": e.title, "vector_indexed": vec_ok, "closed_group": closed})
    db.commit()
    return {"ok": True, "vector_indexed": vec_ok, "closed_group": closed}


@router.post("/{entry_id}/offline")
def offline_entry(entry_id: int, body: dict, user=Depends(api_admin), db=Depends(get_db)):
    reason = (body.get("reason") or "").strip() if isinstance(body, dict) else ""
    if len(reason) < 2:
        raise HTTPException(400, {"code": "reason_required", "message": "下线原因必填"})
    e = db.get(KbEntry, entry_id)
    if not e:
        raise HTTPException(404, {"code": "not_found", "message": "条目不存在"})
    if e.status != "published":
        raise HTTPException(409, {"code": "not_published", "message": "仅已发布条目可下线"})
    e.status = "offline"
    e.offline_reason = reason
    e.updated_by, e.updated_at = user.id, now_str()
    fts_delete(db, e.id)
    log_action(db, user, "kb_offline", "kb_entry", e.id, {"title": e.title, "reason": reason})
    db.commit()
    return {"ok": True}
