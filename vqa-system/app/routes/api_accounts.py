"""账号 API（FR-05）：创建（初始密码仅返回一次）/ 停用（记录保留）/ 重置密码（临时密码仅返回一次）"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..audit import log_action
from ..db import get_db
from ..models import Session, User, now_str
from ..security import api_admin, generate_password, hash_password

router = APIRouter(prefix="/api/accounts", dependencies=[Depends(api_admin)])


class AccountBody(BaseModel):
    username: str
    display_name: str
    org_label: str = ""
    role: str = "staff"


def _user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "org_label": u.org_label, "role": u.role, "status": u.status,
            "must_change_password": u.must_change_password,
            "failed_attempts": u.failed_attempts, "locked_until": u.locked_until or "",
            "created_at": u.created_at}


@router.get("")
def list_accounts(db=Depends(get_db)):
    rows = db.query(User).order_by(User.id).all()
    return {"items": [_user_dict(u) for u in rows]}


@router.post("")
def create_account(body: AccountBody, user=Depends(api_admin), db=Depends(get_db)):
    username = body.username.strip()
    if not (2 <= len(username) <= 30) or not username.replace("_", "").isalnum():
        raise HTTPException(400, {"code": "invalid_username",
                                  "message": "用户名 2-30 位，仅限字母/数字/下划线"})
    if not body.display_name.strip():
        raise HTTPException(400, {"code": "invalid_display_name", "message": "姓名必填"})
    if body.role not in ("staff", "admin"):
        raise HTTPException(400, {"code": "invalid_role", "message": "角色取值无效"})
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(409, {"code": "username_exists", "message": "用户名已存在"})
    initial_pw = generate_password()
    u = User(username=username, display_name=body.display_name.strip(),
             org_label=body.org_label.strip(), role=body.role,
             password_hash=hash_password(initial_pw), must_change_password=1,
             created_by=user.id)
    db.add(u)
    log_action(db, user, "account_create", "user", u.id,
               {"username": u.username, "role": u.role})
    db.commit()
    # 初始密码仅在本次响应中出现一次，不落日志
    return {"id": u.id, "username": u.username, "initial_password": initial_pw}


@router.post("/{user_id}/disable")
def disable_account(user_id: int, user=Depends(api_admin), db=Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, {"code": "not_found", "message": "账号不存在"})
    if target.id == user.id:
        raise HTTPException(409, {"code": "cannot_disable_self", "message": "不能停用自己的账号"})
    if target.role == "admin":
        active_admins = db.query(User).filter(User.role == "admin", User.status == "active").count()
        if active_admins <= 1:
            raise HTTPException(409, {"code": "last_admin", "message": "系统至少保留一名可用管理员"})
    target.status = "disabled"
    target.updated_at = now_str()
    # 停用即时生效：删除其所有会话
    db.query(Session).filter(Session.user_id == target.id).delete()
    log_action(db, user, "account_disable", "user", target.id, {"username": target.username})
    db.commit()
    return {"ok": True}


@router.post("/{user_id}/reset-password")
def reset_password(user_id: int, user=Depends(api_admin), db=Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, {"code": "not_found", "message": "账号不存在"})
    if target.status != "active":
        raise HTTPException(409, {"code": "account_disabled", "message": "账号已停用，先启用再重置"})
    temp_pw = generate_password()
    target.password_hash = hash_password(temp_pw)
    target.must_change_password = 1
    target.failed_attempts = 0
    target.locked_until = None
    target.updated_at = now_str()
    db.query(Session).filter(Session.user_id == target.id).delete()
    log_action(db, user, "password_reset", "user", target.id, {"username": target.username})
    db.commit()
    return {"ok": True, "temp_password": temp_pw}
