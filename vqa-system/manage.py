"""管理脚本：python manage.py init-db | reindex | backup | serve"""
import argparse
import asyncio
import sys

from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.models import KbEntry, KbVector, UnansweredItem, User
from app.security import generate_password, hash_password


def init_db(admin_username: str = "admin", admin_password: str | None = None) -> None:
    """建表 + FTS5 虚拟表 + 留痕触发器 + 内置数据 + 首个管理员
    admin_password 仅供开发/测试指定；生产环境留空（随机生成并打印一次）"""
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
        from app.seed import seed_all
        seed_all(db)
        if db.query(User).count() == 0:
            pw = admin_password or generate_password()
            db.add(User(username=admin_username, display_name="系统管理员", org_label="村委会",
                        role="admin", password_hash=hash_password(pw),
                        must_change_password=1))
            db.commit()
            print("=" * 52)
            print(f"  初始管理员账号：{admin_username}")
            print(f"  初始密码：{pw}")
            print("  首次登录将强制修改密码，请抄录后妥善保存")
            print("=" * 52)
        else:
            print("数据库已初始化（管理员已存在，跳过创建）")
        db.commit()
    print("init-db 完成")


def reindex() -> None:
    """全量重建向量索引（条目全文 + 文档切片 + 未答实例；换 embedding 服务或维度变更后运行）"""
    from app import docing, llm, search
    from app.config import settings
    from app.models import KbChunk, now_str

    with SessionLocal() as db:
        entries = db.query(KbEntry).filter(KbEntry.status == "published").all()
        items = db.query(UnansweredItem).all()
    chunk_rows = []  # (entry_id, chunk_index, text)
    for e in entries:
        for idx, c in enumerate(docing.split_chunks(e.content)):
            chunk_rows.append((e.id, idx, c))
    texts = ([f"{e.title}\n{e.content}" for e in entries]
             + [r[2] for r in chunk_rows] + [i.question for i in items])
    if not texts:
        print("无已发布条目与未答实例，跳过")
        return

    print(f"待重建向量：条目 {len(entries)} + 切片 {len(chunk_rows)} + 未答实例 {len(items)}")
    vectors = []
    for i in range(0, len(texts), 32):
        vectors.extend(asyncio.run(llm.embed(texts[i:i + 32])))
        print(f"  进度 {min(i + 32, len(texts))}/{len(texts)}")

    with SessionLocal() as db:
        vec_iter = iter(vectors)
        for e in entries:
            v = next(vec_iter)
            kv = db.get(KbVector, e.id)
            if kv is None:
                kv = KbVector(entry_id=e.id)
                db.add(kv)
            kv.embedding, kv.dim = search.pack_vec(v), len(v)
            kv.model = settings.llm_embed_model
        db.query(KbChunk).delete()
        for eid, idx, ct in chunk_rows:
            v = next(vec_iter)
            db.add(KbChunk(entry_id=eid, chunk_index=idx, chunk_text=ct,
                           embedding=search.pack_vec(v), dim=len(v), updated_at=now_str()))
        for it in items:
            v = next(vec_iter)
            it.embedding = search.pack_vec(v)
        db.commit()
        from app import vector_store
        vector_store.rebuild(db)
        print(f"向量重建完成（FAISS: {vector_store.stats()}）")


def backup() -> None:
    from app.scheduler import job_backup
    job_backup()


def serve() -> None:
    import uvicorn
    from app.config import settings
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description="乡村智能问答系统管理脚本")
    parser.add_argument("command", choices=["init-db", "reindex", "backup", "serve"])
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument("--admin-password", default=None,
                        help="仅开发/测试用；生产留空则随机生成并打印一次")
    args = parser.parse_args()
    if args.command == "init-db":
        init_db(args.admin_username, args.admin_password)
    else:
        {"reindex": reindex, "backup": backup, "serve": serve}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
