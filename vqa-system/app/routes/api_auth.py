"""认证 API：登录（失败计数/锁定）、注册、登出、改密（旧密码验证）"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..audit import log_action
from ..config import settings
from ..db import get_db
from ..models import User, now_str
from .. import security
from ..security import api_user, create_session, hash_password, verify_password

router = APIRouter(prefix="/api/auth")


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    confirm: str
    display_name: str = ""
    role: str = "staff"          # staff / admin（admin 需邀请码）
    invite_code: str = ""        # 管理员注册邀请码


@router.post("/register")
def register(body: RegisterBody, request: Request, response: Response, db=Depends(get_db)):
    """自助注册：默认 staff；选 admin 须提供邀请码（防止越权自提权）"""
    username = body.username.strip()
    display_name = body.display_name.strip() or username
    role = body.role if body.role in ("staff", "admin") else "staff"

    def err(status: int, code: str, message: str):
        return HTTPException(status_code=status, detail={"code": code, "message": message})

    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise err(400, "invalid_username", "用户名需 3-20 位字母、数字或下划线")
    if body.password != body.confirm:
        raise err(400, "password_mismatch", "两次输入的密码不一致")
    if len(body.password) < 8 or body.password == username:
        raise err(400, "weak_password", "密码至少 8 位，且不得与用户名相同")
    if db.query(User).filter(User.username == username).first():
        raise err(409, "username_taken", "该用户名已被使用")
    if role == "admin" and body.invite_code != settings.admin_invite_code:
        raise err(403, "invalid_invite", "邀请码错误，无法注册管理员账号")

    user = User(username=username, display_name=display_name, org_label="",
                role=role, password_hash=hash_password(body.password),
                status="active", must_change_password=0)
    db.add(user)
    db.flush()
    log_action(db, user, "register", "user", user.id)
    sess = create_session(db, user.id)
    db.commit()
    response.set_cookie(
        security.SESSION_COOKIE, sess.session_id, max_age=settings.session_days * 86400,
        httponly=True, samesite="lax", path="/",
    )
    return {"ok": True, "csrf_token": sess.csrf_token,
            "must_change_password": False, "redirect": "/"}


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db=Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip()).first()
    now = security.now()

    def err(status: int, code: str, message: str):
        return HTTPException(status_code=status, detail={"code": code, "message": message})

    if user is None:
        raise err(401, "invalid_credentials", "用户名或密码错误")
    if user.status != "active":
        raise err(403, "account_disabled", "账号已停用，请联系管理员")
    if user.locked_until and security.parse_dt(user.locked_until) > now:
        left = int((security.parse_dt(user.locked_until) - now).total_seconds() // 60) + 1
        raise err(423, "locked", f"失败次数过多已锁定，请约 {left} 分钟后重试")

    if not verify_password(body.password, user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= settings.login_max_fails:
            user.locked_until = security.add_minutes(now, settings.lock_minutes)
            user.failed_attempts = 0
            db.commit()
            raise err(423, "locked", f"连续失败达 {settings.login_max_fails} 次，账号锁定 {settings.lock_minutes} 分钟")
        db.commit()
        raise err(401, "invalid_credentials", "用户名或密码错误")

    # 成功：清零、建会话、写留痕
    user.failed_attempts = 0
    user.locked_until = None
    user.updated_at = now_str()
    sess = create_session(db, user.id)
    log_action(db, user, "login_success", "user", user.id)
    db.commit()
    response.set_cookie(
        security.SESSION_COOKIE, sess.session_id, max_age=settings.session_days * 86400,
        httponly=True, samesite="lax", path="/",
    )
    return {
        "ok": True,
        "csrf_token": sess.csrf_token,
        "must_change_password": bool(user.must_change_password),
        "redirect": "/change-password" if user.must_change_password else "/",
    }


@router.post("/logout")
def logout(request: Request, response: Response, user=Depends(api_user), db=Depends(get_db)):
    sess = request.state.session
    db.delete(sess)
    db.commit()
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, user=Depends(api_user), db=Depends(get_db)):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(403, {"code": "wrong_password", "message": "原密码不正确"})
    new_pw = body.new_password
    if len(new_pw) < 8 or new_pw == body.old_password or new_pw == user.username:
        raise HTTPException(400, {"code": "weak_password",
                                  "message": "新密码至少 8 位，且不得与原密码或用户名相同"})
    user.password_hash = hash_password(new_pw)
    user.must_change_password = 0
    user.updated_at = now_str()
    log_action(db, user, "password_change", "user", user.id)
    db.commit()
    return {"ok": True, "redirect": "/"}
