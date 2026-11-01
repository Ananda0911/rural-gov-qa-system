"""页面路由（Jinja2 服务端渲染）：未登录跳 /login；未改密只放行 /change-password"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .. import search
from ..config import BASE_DIR
from ..db import get_db
from ..kb_ops import year_ago_str
from ..models import CATEGORIES, KbEntry, OperationLog, QaRecord
from ..pipeline import citation_snippet
from ..security import MCP_FREE_API, resolve_session

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _redirect(url: str) -> HTTPException:
    return HTTPException(status_code=302, headers={"Location": url})


def page_user(request: Request, db=Depends(get_db)):
    resolved = resolve_session(db, request)
    if not resolved:
        raise _redirect("/login")
    sess, user = resolved
    request.state.session, request.state.user = sess, user
    if user.must_change_password and request.url.path != "/change-password":
        raise _redirect("/change-password")
    return user


def page_admin(user=Depends(page_user)):
    if user.role != "admin":
        raise _redirect("/")
    return user


def _base_ctx(request, user, **extra) -> dict:
    return {"request": request, "user": user,
            "csrf_token": request.state.session.csrf_token, **extra}


@router.get("/login")
def login_page(request: Request, db=Depends(get_db)):
    resolved = resolve_session(db, request)
    if resolved:
        u = resolved[1]
        raise _redirect("/change-password" if u.must_change_password else "/")
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": ""})


@router.get("/change-password")
def change_password_page(request: Request, user=Depends(page_user)):
    return templates.TemplateResponse(request, "change_password.html",
                                      _base_ctx(request, user, initial=user.must_change_password == 1))


@router.get("/")
def workbench(request: Request, user=Depends(page_user), db=Depends(get_db)):
    recent = (db.query(QaRecord).filter(QaRecord.user_id == user.id)
              .order_by(QaRecord.created_at.desc()).limit(10).all())
    return templates.TemplateResponse(request, "index.html",
                                      _base_ctx(request, user, recent=recent))


@router.get("/records")
def records_page(request: Request, status: str = "", user=Depends(page_user), db=Depends(get_db)):
    query = db.query(QaRecord).filter(QaRecord.user_id == user.id)
    if status in ("answered", "missed", "out_of_scope"):
        query = query.filter(QaRecord.status == status)
    rows = query.order_by(QaRecord.created_at.desc()).limit(100).all()
    records = []
    for r in rows:
        citations = []
        if r.status == "answered":
            for cid in r.cited_ids():
                e = db.get(KbEntry, cid)
                if e:
                    citations.append({"id": e.id, "title": e.title, "category": e.category,
                                      "publish_org": e.publish_org, "basis_file": e.basis_file,
                                      "entry_status": e.status,
                                      "snippet": citation_snippet(db, e, None,
                                                                  search.jieba_tokens(r.question))})
        records.append({"id": r.id, "question": r.question, "answer": r.answer or "",
                         "status": r.status, "citations": citations, "degraded": r.degraded,
                         "feedback": r.feedback, "created_at": r.created_at,
                         "sensitive_category": r.sensitive_category or ""})
    return templates.TemplateResponse(request, "records.html",
                                      _base_ctx(request, user, records=records,
                                                status=status or ""))


@router.get("/admin/kb")
def admin_kb(request: Request, user=Depends(page_admin)):
    return templates.TemplateResponse(request, "admin_kb.html",
                                      _base_ctx(request, user, categories=CATEGORIES))


@router.get("/admin/unanswered")
def admin_unanswered(request: Request, user=Depends(page_admin)):
    return templates.TemplateResponse(request, "admin_unanswered.html", _base_ctx(request, user))


@router.get("/admin/accounts")
def admin_accounts(request: Request, user=Depends(page_admin)):
    return templates.TemplateResponse(request, "admin_accounts.html", _base_ctx(request, user))


@router.get("/admin/logs")
def admin_logs(request: Request, action: str = "", user=Depends(page_admin), db=Depends(get_db)):
    query = db.query(OperationLog)
    if action:
        query = query.filter(OperationLog.action == action)
    rows = query.order_by(OperationLog.id.desc()).limit(300).all()
    old_count = db.query(QaRecord).filter(QaRecord.created_at < year_ago_str()).count()
    actions = [a[0] for a in db.query(OperationLog.action).distinct().all()]
    return templates.TemplateResponse(request, "admin_logs.html",
                                      _base_ctx(request, user, logs=rows, actions=sorted(actions),
                                                current_action=action, old_count=old_count))
