"""应用入口：FastAPI + 静态资源 + 路由 + APScheduler 生命周期"""
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import scheduler as sched
from . import vector_store
from .config import BASE_DIR, settings
from .db import Base, SessionLocal, engine

(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(BASE_DIR / "data" / "app.log",
                                             maxBytes=5 * 1024 * 1024, backupCount=3,
                                             encoding="utf-8"),
    ],
)
log = logging.getLogger("vqa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # 幂等补表：旧库升级自动创建新增表（如 kb_chunks）
    with SessionLocal() as db:
        vector_store.rebuild(db)      # FAISS 内存镜像启动时从 SQLite 全量重建
    log.info("FAISS 向量索引: %s", vector_store.stats())
    Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)
    if settings.run_scheduler:
        sched.start()
    yield
    sched.shutdown()


app = FastAPI(title="乡村智能问答系统", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

from .routes import api_accounts, api_ask, api_auth, api_import, api_kb, api_unanswered, pages  # noqa: E402

app.include_router(pages.router)
app.include_router(api_auth.router)
app.include_router(api_ask.router)
app.include_router(api_kb.router)
app.include_router(api_import.router)
app.include_router(api_unanswered.router)
app.include_router(api_accounts.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
