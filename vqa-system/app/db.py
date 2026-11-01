"""数据库层：SQLite(WAL) + SQLAlchemy 2.x + sqlite-vec 扩展"""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

try:
    import sqlite_vec  # 向量扩展（不可用时自动降级为 Python 余弦计算）
except Exception:  # pragma: no cover
    sqlite_vec = None

Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)

_VEC_OK = False


@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _record):
    global _VEC_OK
    if sqlite_vec is not None:
        try:
            sqlite_vec.load(dbapi_conn)
            _VEC_OK = True
        except Exception:
            _VEC_OK = False
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def vec_available() -> bool:
    return _VEC_OK


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 依赖：每请求一个会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
