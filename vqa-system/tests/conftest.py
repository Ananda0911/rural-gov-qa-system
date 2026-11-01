"""回归测试公共设施。

关键约束：config.settings / db.engine 均在 app 包首次导入时初始化，
因此测试环境变量必须在任何 `from app...` 之前设置（本文件顶部即完成）。
策略：LLM_MOCK=1 本地模拟（不出域）+ 临时目录数据库 + 关闭调度器。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # vqa-system/
TEST_DIR = Path(tempfile.gettempdir()) / f"vqa_test_{int(time.time())}"

# ★ 必须先于 app 导入生效
os.environ["LLM_MOCK"] = "1"            # 本地模拟 LLM，测试不出域
os.environ["RUN_SCHEDULER"] = "0"       # 关闭 APScheduler
os.environ["BCRYPT_ROUNDS"] = "4"       # 降低 bcrypt 轮数加速（仅测试）
os.environ["LOGIN_MAX_FAILS"] = "5"
# 固定为 .env 当前校准阈值（2026-10 按百炼 text-embedding-v4 实测），防 .env 调参导致测试漂移
os.environ["T1_RELEVANCE"] = "0.55"
os.environ["T2_AGGREGATE"] = "0.70"
os.environ["T3_SIMILAR"] = "0.60"
os.environ["DB_PATH"] = str(TEST_DIR / "app.db")
os.environ["UPLOAD_DIR"] = str(TEST_DIR / "uploads")
os.environ["BACKUP_DIR"] = str(TEST_DIR / "backup")

sys.path.insert(0, str(BASE_DIR))

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

TEST_PASSWORD = "Passw0rd123"

# 每个用例开始前清空的业务表（operation_logs 受 append-only 触发器保护，
# 特意不清——触发器行为本身有专项测试；users / sensitive_rules / referral_map 为种子数据保留）
_CLEAN_TABLES = ("qa_records", "unanswered_items", "unanswered_groups", "kb_files",
                 "kb_chunks", "kb_vectors", "kb_entries", "kb_fts", "sessions")


@pytest.fixture(scope="session", autouse=True)
def init_database():
    """一次性建库：表结构 + FTS5 虚拟表 + append-only 触发器 + 内置数据 + 测试账号
    （与 manage.py init-db 的建库逻辑保持一致）"""
    from app.db import Base, SessionLocal, engine
    from app.models import User
    from app.security import hash_password
    from app.seed import seed_all

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, content, tags)"))
        db.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_logs_no_update BEFORE UPDATE ON operation_logs "
            "BEGIN SELECT RAISE(ABORT, 'operation_logs 为 append-only，禁止修改'); END"))
        db.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_logs_no_delete BEFORE DELETE ON operation_logs "
            "BEGIN SELECT RAISE(ABORT, 'operation_logs 为 append-only，禁止删除'); END"))
        seed_all(db)
        if db.query(User).count() == 0:
            db.add(User(username="staff", display_name="张干事", org_label="村委会",
                        role="staff", password_hash=hash_password(TEST_PASSWORD),
                        must_change_password=0))
            db.add(User(username="boss", display_name="李管理员", org_label="乡镇政府",
                        role="admin", password_hash=hash_password(TEST_PASSWORD),
                        must_change_password=0))
            db.add(User(username="rookie", display_name="王新人", org_label="村委会",
                        role="staff", password_hash=hash_password(TEST_PASSWORD),
                        must_change_password=1))
            db.commit()
    yield


@pytest.fixture(autouse=True)
def clean_business_tables(init_database):
    """用例隔离：每个测试开始前清空业务数据（结构、种子与账号保留）"""
    from app.db import SessionLocal

    with SessionLocal() as db:
        for t in _CLEAN_TABLES:
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        # 账号状态复位（锁定计数等不跨用例泄漏）
        db.execute(text("UPDATE users SET locked_until = NULL, failed_attempts = 0"))
        db.commit()
    from app import vector_store
    vector_store.reset()          # FAISS 内存镜像随表清空一并重置（下次 publish 触发重建）
    yield


@pytest.fixture()
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def run():
    """同步驱动协程（免装 pytest-asyncio）"""
    return asyncio.run


@pytest.fixture()
def no_sleep(monkeypatch):
    """压掉管线重试的 2s 退避，加速技术失败用例"""
    async def _instant(_sec):
        return None
    monkeypatch.setattr("asyncio.sleep", _instant)


@pytest.fixture()
def publish(db):
    """发布知识条目：状态 + FTS + 向量/切片索引同事务建立（与线上发布路径一致）"""
    from app import kb_ops
    from app.models import KbEntry

    async def _publish(title, content, category="办事流程", tags=None, expire_date=None,
                       last_verified_at="2026-01-01 00:00:00"):
        e = KbEntry(title=title, category=category, content=content,
                    basis_file="测试依据文件.pdf", publish_org="县农业农村局",
                    tags=json.dumps(tags or [], ensure_ascii=False), status="published",
                    expire_date=expire_date, last_verified_at=last_verified_at)
        db.add(e)
        db.flush()
        kb_ops.fts_upsert(db, e)
        await kb_ops.vector_upsert(db, e)
        db.commit()
        return e

    return _publish


@pytest.fixture()
def staff(db):
    from app.models import User

    return db.query(User).filter(User.username == "staff").one()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def login(client):
    """登录并返回 CSRF 令牌（Cookie 由 TestClient 自动携带）"""
    def _login(username="staff", password=TEST_PASSWORD):
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
        return r.json()["csrf_token"]
    return _login
