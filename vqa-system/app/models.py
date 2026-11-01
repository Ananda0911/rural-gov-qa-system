"""数据模型（技术方案 v1.0 第 4 节）：8 张业务表 + 2 组内置数据表
kb_fts 为 FTS5 虚拟表，由 manage.py init-db 以原生 SQL 创建，不在 ORM 中。
"""
import json
from datetime import datetime

from sqlalchemy import Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------- 账号（FR-05） ----------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    org_label: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(10), default="staff")  # staff / admin
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    must_change_password: Mapped[int] = mapped_column(Integer, default=1)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[str | None] = mapped_column(String(19), default=None)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active / disabled
    created_by: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)
    updated_at: Mapped[str] = mapped_column(String(19), default=now_str)


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(19), nullable=False)
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)


# ---------- 知识库（FR-03） ----------
CATEGORIES = ("办事流程", "惠民政策", "村务公开", "生活服务")
KB_STATUS = ("draft", "published", "offline", "expired")


class KbEntry(Base):
    __tablename__ = "kb_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    basis_file: Mapped[str] = mapped_column(String(200), nullable=False)  # 依据文件名（无依据不入库）
    publish_org: Mapped[str] = mapped_column(String(100), nullable=False)  # 发布单位
    expire_date: Mapped[str | None] = mapped_column(String(10), default=None)
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    status: Mapped[str] = mapped_column(String(12), default="draft")
    source_group_id: Mapped[int | None] = mapped_column(Integer, default=None)  # 由哪个未答组补充而来
    last_verified_at: Mapped[str | None] = mapped_column(String(19), default=None)
    offline_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)
    updated_by: Mapped[int | None] = mapped_column(Integer, default=None)
    updated_at: Mapped[str] = mapped_column(String(19), default=now_str)

    def tags_list(self) -> list:
        try:
            return json.loads(self.tags or "[]")
        except Exception:
            return []


class KbVector(Base):
    __tablename__ = "kb_vectors"

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String(19), default=now_str)


class KbChunk(Base):
    """文档切片（方案 B）：正文切分后逐片向量化，向量检索先召回切片、再按条目聚合最高分"""
    __tablename__ = "kb_chunks"
    __table_args__ = (Index("ix_chunks_entry", "entry_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String(19), default=now_str)


class KbFile(Base):
    """原始文件归档（文档导入）：文件存 data/uploads/ 文件夹，本表记元数据与所属条目"""
    __tablename__ = "kb_files"
    __table_args__ = (Index("ix_files_entry", "entry_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    orig_filename: Mapped[str] = mapped_column(String(200), nullable=False)  # 原始文件名（展示/下载用）
    stored_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)  # 落盘文件名（系统生成）
    ext: Mapped[str] = mapped_column(String(10), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    uploaded_by: Mapped[int | None] = mapped_column(Integer, default=None)
    uploaded_at: Mapped[str] = mapped_column(String(19), default=now_str)


# ---------- 问答记录（FR-01 / FR-02） ----------
class QaRecord(Base):
    __tablename__ = "qa_records"
    __table_args__ = (Index("ix_qa_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(15), nullable=False)  # answered/missed/out_of_scope
    cited_entry_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组（服务端校验后写入）
    sensitive_category: Mapped[str | None] = mapped_column(String(50), default=None)
    degraded: Mapped[int] = mapped_column(Integer, default=0)
    feedback: Mapped[str] = mapped_column(String(10), default="none")  # none/useful/useless
    feedback_at: Mapped[str | None] = mapped_column(String(19), default=None)
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)

    def cited_ids(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.cited_entry_ids or "[]")]
        except Exception:
            return []


# ---------- 未答问题（FR-04）：组为管理单元，组内挂实例 ----------
class UnansweredGroup(Base):
    __tablename__ = "unanswered_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    representative_question: Mapped[str] = mapped_column(Text, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(15), default="pending")  # pending/supplemented/closed
    related_entry_id: Mapped[int | None] = mapped_column(Integer, default=None)
    close_reason: Mapped[str | None] = mapped_column(Text, default=None)
    first_asked_at: Mapped[str] = mapped_column(String(19), default=now_str)
    last_asked_at: Mapped[str] = mapped_column(String(19), default=now_str)
    handled_by: Mapped[int | None] = mapped_column(Integer, default=None)
    handled_at: Mapped[str | None] = mapped_column(String(19), default=None)


class UnansweredItem(Base):
    __tablename__ = "unanswered_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    qa_record_id: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)


# ---------- 操作留痕（append-only，触发器禁改删兜底） ----------
class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int | None] = mapped_column(Integer, default=None)  # 系统任务为 NULL
    operator_name: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(30), default=None)
    object_id: Mapped[int | None] = mapped_column(Integer, default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)  # JSON
    created_at: Mapped[str] = mapped_column(String(19), default=now_str)


# ---------- 内置数据表 ----------
class SensitiveRule(Base):
    __tablename__ = "sensitive_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    patterns: Mapped[str] = mapped_column(Text, nullable=False)  # 正则 JSON 数组
    advice: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1)


class ReferralMap(Base):
    __tablename__ = "referral_map"

    category: Mapped[str] = mapped_column(String(20), primary_key=True)
    org: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
