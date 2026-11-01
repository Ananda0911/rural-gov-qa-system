"""定时任务（APScheduler，与服务同进程同数据库）
- 每小时 :00 条目到期下线；02:00 会话清理；02:40 上传暂存清理；02:30 记录保留检查（P0 只提示）
- 每日 03:00 备份：SQLite 在线备份 + uploads 原件打包（均滚动保留 backup_keep 份）
"""
import logging
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import SessionLocal
from .docing import clean_tmp_files
from .kb_ops import expire_entries, year_ago_str
from .models import QaRecord, Session

log = logging.getLogger("vqa.scheduler")
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


def job_expire_entries() -> None:
    with SessionLocal() as db:
        n = expire_entries(db)
        if n:
            log.info("到期下线 %s 条", n)


def job_backup() -> None:
    """sqlite3 backup API 在线备份（WAL 一致性），滚动保留 30 份"""
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{datetime.now().strftime('%Y-%m-%d')}.db"
    src = sqlite3.connect(settings.db_path)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    # 滚动清理
    files = sorted(backup_dir.glob("*.db"))
    for f in files[:-settings.backup_keep]:
        f.unlink(missing_ok=True)
    log.info("备份完成: %s", target)
    _backup_upload_files()


def _backup_upload_files() -> None:
    """uploads 正式区原件打包备份（与库备份同日），滚动保留 backup_keep 份"""
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    up = Path(settings.upload_dir)
    files = sorted(f for f in up.iterdir() if f.is_file()) if up.exists() else []
    if files:
        target = backup_dir / f"files-{datetime.now().strftime('%Y-%m-%d')}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f.name)
        log.info("归档原件备份完成: %s（%s 个文件）", target, len(files))
    zips = sorted(backup_dir.glob("files-*.zip"))
    for f in zips[:-settings.backup_keep]:
        f.unlink(missing_ok=True)


def job_clean_sessions() -> None:
    cutoff = (datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    with SessionLocal() as db:
        n = db.query(Session).filter(Session.expires_at < cutoff).delete()
        db.commit()
    if n:
        log.info("清理过期会话 %s 条", n)


def job_clean_uploads_tmp() -> None:
    """清理超期未确认的上传暂存（24 小时，防 data/uploads/tmp 无限增长）"""
    n = clean_tmp_files(24)
    if n:
        log.info("清理超期上传暂存 %s 个", n)


def job_retention_check() -> None:
    """超 1 年记录：P0 只提示不删除（处置细则实施期定）"""
    with SessionLocal() as db:
        n = db.query(QaRecord).filter(QaRecord.created_at < year_ago_str()).count()
    if n:
        log.warning("存在 %s 条超过 1 年的问答记录，可导出归档（管理端留痕页可见提示）", n)


def start() -> None:
    scheduler.add_job(job_expire_entries, CronTrigger(minute=0), id="expire_entries",
                      replace_existing=True)
    scheduler.add_job(job_clean_sessions, CronTrigger(hour=2, minute=0), id="clean_sessions",
                      replace_existing=True)
    scheduler.add_job(job_retention_check, CronTrigger(hour=2, minute=30), id="retention_check",
                      replace_existing=True)
    scheduler.add_job(job_clean_uploads_tmp, CronTrigger(hour=2, minute=40), id="clean_uploads_tmp",
                      replace_existing=True)
    scheduler.add_job(job_backup, CronTrigger(hour=3, minute=0), id="backup",
                      replace_existing=True)
    scheduler.start()
    log.info("定时任务已启动（到期下线/会话清理/暂存清理/保留检查/每日备份）")


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
