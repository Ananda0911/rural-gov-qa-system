"""文档导入 API（仅管理员）：上传 → 解析切分预览 → 人工核对确认 → 存为草稿 + 原件归档
- parse：解析切分不落库；原件暂存 data/uploads/tmp/（令牌引用），未确认的由定时任务清理
- confirm：原件转正式区 data/uploads/ 并登记 kb_files 元数据，再走通用建草稿路径
- 发布环节复用现有 /api/kb/{id}/publish：同事务重建条目全文向量与切片向量（方案 B）"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile

from ..audit import log_action
from ..db import get_db
from ..docing import (ALLOWED_EXTS, MAX_FILE_SIZE, DocParseError, extract_text,
                      promote_tmp_file, save_tmp_file, split_chunks, upload_dir)
from ..kb_ops import check_similar_titles
from ..models import KbEntry, KbFile
from ..security import api_admin
from .api_kb import KbBody, _validate, create_draft

router = APIRouter(prefix="/api/admin/import", dependencies=[Depends(api_admin)])


class ImportBody(KbBody):
    file_token: str | None = None  # parse 返回的上传暂存令牌；确认时据此转正式归档


def _basename(filename: str) -> str:
    return (filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def _ext_of(filename: str) -> str:
    name = _basename(filename)
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _discard_stored(stored_name: str) -> None:
    """回收已转正式区的原件（条目创建失败时）；存储名由系统生成，无路径注入风险"""
    try:
        (upload_dir() / stored_name).unlink(missing_ok=True)
    except OSError:
        pass


async def _precheck(db, body: KbBody) -> None:
    """业务校验前置：让可预期的 400/409 失败发生在消费上传暂存令牌之前（重试无需重新上传）"""
    _validate(body)
    if db.query(KbEntry).filter(KbEntry.title == body.title.strip()).first():
        raise HTTPException(409, {"code": "title_exists", "message": "标题已存在（数据库层唯一兜底）"})
    similar = await check_similar_titles(db, body.title.strip())
    if similar and not body.force:
        raise HTTPException(409, {"code": "similar_titles",
                                  "message": "检测到与已发布条目标题高度相似，是否为原条目的更新版本？",
                                  "similar_to": similar})


@router.post("/parse")
async def parse_doc(file: UploadFile, user=Depends(api_admin)):
    """解析 + 规则切分预览：不入库、不向量化；原件暂存（返回令牌），确认后再转正式归档"""
    ext = _ext_of(file.filename)
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, {"code": "bad_ext",
                                  "message": f"仅支持 {' / '.join(ALLOWED_EXTS)} 格式（扫描件请先转为文字版）"})
    filebytes = await file.read()
    if not filebytes:
        raise HTTPException(400, {"code": "empty_file", "message": "文件为空"})
    if len(filebytes) > MAX_FILE_SIZE:
        raise HTTPException(400, {"code": "too_large", "message": "文件超过 5MB 上限"})
    try:
        text = extract_text(filebytes, ext)
    except DocParseError as e:
        raise HTTPException(422, {"code": "parse_failed", "message": str(e)})
    chunks = split_chunks(text)
    finfo = save_tmp_file(filebytes, _basename(file.filename), ext)  # 全部成功后再暂存，不留垃圾文件
    return {
        "file_token": finfo["token"],
        "orig_filename": finfo["orig_filename"],
        "text": text,
        "suggest_title": _basename(file.filename).rsplit(".", 1)[0].strip()[:200],
        "basis_file": _basename(file.filename)[:200],
        "chunks": [{"index": i, "text": c,
                    "preview": c[:80] + ("…" if len(c) > 80 else "")} for i, c in enumerate(chunks)],
    }


@router.post("/confirm")
async def confirm_doc(body: ImportBody, user=Depends(api_admin), db=Depends(get_db)):
    """人工核对后存为草稿：与手工新建同一条路径（标题唯一 + 相似提示闭环 + 留痕）；
    带 file_token 时原件转正式区归档并登记 kb_files，失败自动回收不留孤儿文件"""
    if not body.file_token:
        return await create_draft(db, user, body, via="doc_import")
    await _precheck(db, body)
    try:
        finfo = promote_tmp_file(body.file_token)  # tmp → 正式区（令牌一次性，天然防重复提交）
    except FileNotFoundError:
        raise HTTPException(410, {"code": "file_expired",
                                  "message": "上传文件已过期或已使用，请重新上传解析"})
    try:
        result = await create_draft(db, user, body, via="doc_import")
    except Exception:
        _discard_stored(finfo["stored_name"])  # 条目创建失败回收原件，不留无主文件
        raise
    db.add(KbFile(entry_id=result["id"], orig_filename=finfo["orig_filename"],
                  stored_name=finfo["stored_name"], ext=finfo["ext"], size=finfo["size"],
                  sha256=finfo["sha256"], uploaded_by=user.id))
    log_action(db, user, "file_attach", "kb_entry", result["id"],
               {"orig_filename": finfo["orig_filename"], "stored_name": finfo["stored_name"],
                "size": finfo["size"], "sha256": finfo["sha256"]})
    db.commit()
    return result
