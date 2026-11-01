"""认证与会话：bcrypt + 服务端 Session 表 + HttpOnly Cookie（7 天滑动）+ CSRF 令牌
每次请求校验账号状态：停用/锁定即时生效；首次登录未改密时只放行改密与登出。
"""
import secrets
import string
from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, Request

from .config import settings
from .db import get_db
from .models import Session, User, now_str

SESSION_COOKIE = "vqa_session"
# must_change_password=1 时允许访问的 API
MCP_FREE_API = {"/api/auth/change-password", "/api/auth/logout"}


# ---------- 密码 ----------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode())
    except Exception:
        return False


def generate_password(length: int = 12) -> str:
    """初始/临时密码：大小写字母 + 数字，仅在创建响应中出现一次"""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)):
            return pw


# ---------- 时间 ----------
def now() -> datetime:
    return datetime.now()


def add_days(dt: datetime, days: int) -> str:
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def add_minutes(dt: datetime, minutes: int) -> str:
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---------- 会话 ----------
def create_session(db, user_id: int) -> Session:
    sess = Session(
        session_id=secrets.token_hex(32),
        user_id=user_id,
        csrf_token=secrets.token_hex(32),
        expires_at=add_days(now(), settings.session_days),
    )
    db.add(sess)
    return sess


def resolve_session(db, request: Request):
    """Cookie → (Session, User)；校验过期/停用/锁定，滑动续期。失败返回 None。"""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    sess = db.get(Session, sid)
    if sess is None:
        return None
    # 滑动续期：剩余不足 6 天时刷新
    if parse_dt(sess.expires_at) < now() + timedelta(days=6):
        sess.expires_at = add_days(now(), settings.session_days)
    user = db.get(User, sess.user_id)
    if user is None or user.status != "active":
        return None
    if user.locked_until and parse_dt(user.locked_until) > now():
        return None
    return sess, user


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ---------- API 守卫 ----------
def api_user(request: Request, db=Depends(get_db)) -> User:
    resolved = resolve_session(db, request)
    if not resolved:
        raise _err(401, "unauthorized", "未登录或会话已过期，请重新登录")
    sess, user = resolved
    if user.must_change_password and request.url.path not in MCP_FREE_API:
        raise _err(403, "must_change_password", "首次登录请先修改初始密码")
    # CSRF：写操作校验一次性令牌（登录接口除外）
    if request.method in {"POST", "PUT", "DELETE"} and request.url.path != "/api/auth/login":
        if request.headers.get("X-CSRF-Token") != sess.csrf_token:
            raise _err(403, "csrf", "页面令牌校验失败，请刷新页面后重试")
    request.state.session = sess
    request.state.db = db
    return user


def api_admin(user: User = Depends(api_user)) -> User:
    if user.role != "admin":
        raise _err(403, "forbidden", "需要管理员权限")
    return user
