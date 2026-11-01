"""文档导入（方案 B）：解析（docx / pdf 文字版 / txt / md）+ 规则切分 + 原始文件归档
- 原始文件存 data/uploads/ 文件夹（tmp 暂存 → 确认导入后转正式区），元数据入业务库 kb_files
- 扫描件/图片型 PDF 无法提取文字，明确报错（OCR 列为后续增强，不在本期）
"""
import hashlib
import io
import json
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path

MAX_FILE_SIZE = 5 * 1024 * 1024  # 上传上限 5MB
ALLOWED_EXTS = (".docx", ".pdf", ".txt", ".md")
MIN_TEXT_LEN = 50     # 提取文字低于此长度视为疑似扫描件
CHUNK_HARD_MAX = 800  # 单片上限：超长段落按此硬切；聚合出片亦不超过此长度


class DocParseError(Exception):
    """解析失败（格式不支持/文件损坏/疑似扫描件），消息可直接展示给用户"""


def extract_text(filebytes: bytes, ext: str) -> str:
    ext = (ext or "").lower()
    if ext not in ALLOWED_EXTS:
        raise DocParseError(f"仅支持 {' / '.join(ALLOWED_EXTS)} 格式")
    text = ""
    if ext in (".txt", ".md"):
        try:
            text = filebytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = filebytes.decode("gbk")
            except UnicodeDecodeError:
                raise DocParseError("文本编码无法识别（仅支持 UTF-8 / GBK）")
    elif ext == ".docx":
        try:
            from docx import Document
            doc = Document(io.BytesIO(filebytes))
            text = "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise DocParseError(f"Word 文档解析失败：{e}")
    elif ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(filebytes))
            if reader.is_encrypted:
                raise DocParseError("PDF 已加密，无法解析")
            text = "\n\n".join((pg.extract_text() or "").strip() for pg in reader.pages
                               if (pg.extract_text() or "").strip())
        except DocParseError:
            raise
        except Exception as e:
            raise DocParseError(f"PDF 解析失败：{e}")
    text = (text or "").strip()
    if len(text) < MIN_TEXT_LEN:
        raise DocParseError("未能提取到有效文字（疑似扫描件或图片型 PDF），请改用文字版文件")
    return text


def _hard_cut(paragraph: str) -> list[str]:
    """超长段落按 CHUNK_HARD_MAX 硬切（规则切分兜底，不追求语义边界）"""
    if len(paragraph) <= CHUNK_HARD_MAX:
        return [paragraph]
    return [paragraph[i:i + CHUNK_HARD_MAX] for i in range(0, len(paragraph), CHUNK_HARD_MAX)]


def split_chunks(text: str) -> list[str]:
    """规则切分：按空行分段落，段落完整聚合（目标 500-800 字/片）；
    全文不足上限时自然形成 1 片；无空行分段的文本退化为按单行聚合"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in re.split(r"\n{2,}", normalized) if p.strip()]
    if not paras:
        paras = [ln.strip() for ln in normalized.split("\n") if ln.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        for piece in _hard_cut(para):
            if cur and len(cur) + len(piece) + 1 > CHUNK_HARD_MAX:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


# ---------- 原始文件归档：文件夹存原件 + 元数据入业务库 ----------
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")  # 暂存令牌格式（先校验再拼路径，防路径注入）


def upload_dir() -> Path:
    from .config import settings
    p = Path(settings.upload_dir)
    (p / "tmp").mkdir(parents=True, exist_ok=True)
    return p


def save_tmp_file(filebytes: bytes, orig_filename: str, ext: str) -> dict:
    """上传暂存：tmp/{token}{ext} + {token}.meta；确认导入时再转正式归档，未确认的定期自动清理"""
    token = secrets.token_hex(16)
    meta = {"orig_filename": (orig_filename or "")[:200], "ext": ext,
            "size": len(filebytes), "sha256": hashlib.sha256(filebytes).hexdigest()}
    (upload_dir() / "tmp" / f"{token}{ext}").write_bytes(filebytes)
    (upload_dir() / "tmp" / f"{token}.meta").write_text(json.dumps(meta, ensure_ascii=False),
                                                        encoding="utf-8")
    return {"token": token, **meta}


def read_tmp_meta(token: str) -> dict | None:
    if not _TOKEN_RE.match(token or ""):
        return None
    m = upload_dir() / "tmp" / f"{token}.meta"
    if not m.exists():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except Exception:
        return None


def promote_tmp_file(token: str) -> dict:
    """确认导入：tmp → 正式区（落盘名由系统生成，与原始名解耦，防重名），返回归档元数据"""
    meta = read_tmp_meta(token)
    if meta is None:
        raise FileNotFoundError(token)
    tmp_dir = upload_dir() / "tmp"
    src = tmp_dir / f"{token}{meta['ext']}"
    if not src.exists():
        raise FileNotFoundError(token)
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{token[:8]}{meta['ext']}"
    shutil.move(str(src), str(upload_dir() / stored_name))
    (tmp_dir / f"{token}.meta").unlink(missing_ok=True)
    return {"orig_filename": meta["orig_filename"], "stored_name": stored_name,
            "ext": meta["ext"], "size": meta["size"], "sha256": meta["sha256"]}


def clean_tmp_files(max_age_hours: int = 24) -> int:
    """清理超期未确认的上传暂存（定时任务调用），返回清理数量"""
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    n = 0
    for f in (upload_dir() / "tmp").iterdir():
        try:
            if f.stat().st_mtime < cutoff.timestamp():
                f.unlink(missing_ok=True)
                n += 1
        except OSError:
            continue
    return n
