"""文档导入回归：文本提取（编码/格式校验/扫描件拦截）、规则切片、暂存归档流转"""
import io
import os
import time
from pathlib import Path

import pytest

from app import docing
from app.config import settings

LONG = "乡村政务智能问答系统的回归测试文本段落，用于验证解析与切片逻辑。" * 5   # >50 字


# ---------- 文本提取 ----------
def test_extract_text_utf8_and_gbk():
    assert docing.extract_text(LONG.encode("utf-8"), ".txt") == LONG
    assert docing.extract_text(LONG.encode("gbk"), ".txt") == LONG


def test_extract_text_rejects_unsupported_ext():
    with pytest.raises(docing.DocParseError):
        docing.extract_text(b"x" * 100, ".exe")


def test_extract_text_rejects_scan_like_short_text():
    with pytest.raises(docing.DocParseError, match="扫描件"):
        docing.extract_text("太短".encode("utf-8"), ".txt")


def test_extract_text_rejects_unknown_encoding():
    with pytest.raises(docing.DocParseError):
        docing.extract_text(b"\xff\xfe\x00\x01" * 50, ".txt")


def test_extract_text_docx_roundtrip():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    for _ in range(3):
        d.add_paragraph(LONG)
    buf = io.BytesIO()
    d.save(buf)
    text = docing.extract_text(buf.getvalue(), ".docx")
    assert LONG in text


# ---------- 规则切片 ----------
def test_split_chunks_short_text_single():
    assert docing.split_chunks("A" * 500) == ["A" * 500]


def test_split_chunks_aggregates_paragraphs_under_limit():
    text = "段一内容" * 80 + "\n\n" + "段二内容" * 80      # 两段共约 640 字
    chunks = docing.split_chunks(text)
    assert len(chunks) == 1                                 # 聚合后不超 800 → 单片
    assert "段一内容" in chunks[0] and "段二内容" in chunks[0]


def test_split_chunks_hard_cuts_overlong_paragraph():
    text = "字" * 2000
    chunks = docing.split_chunks(text)
    assert all(len(c) <= docing.CHUNK_HARD_MAX for c in chunks)
    assert sum(len(c) for c in chunks) == 2000
    assert "".join(chunks) == text                          # 无丢失无重叠


def test_split_chunks_no_blank_lines_falls_back_to_lines():
    text = "\n".join(f"第{i}行内容" for i in range(100))
    chunks = docing.split_chunks(text)
    assert chunks and all(c.strip() for c in chunks)


# ---------- 暂存归档流转 ----------
def test_tmp_file_save_read_promote_lifecycle():
    data = LONG.encode("utf-8")
    meta = docing.save_tmp_file(data, "通知原文.txt", ".txt")
    assert meta["size"] == len(data) and len(meta["sha256"]) == 64
    assert docing.read_tmp_meta(meta["token"])["orig_filename"] == "通知原文.txt"
    # 非法 token 防路径注入
    assert docing.read_tmp_meta("../evil") is None
    assert docing.read_tmp_meta("") is None

    promoted = docing.promote_tmp_file(meta["token"])
    assert promoted["orig_filename"] == "通知原文.txt"
    assert (Path(settings.upload_dir) / promoted["stored_name"]).exists()
    assert docing.read_tmp_meta(meta["token"]) is None          # meta 随确认而清理
    with pytest.raises(FileNotFoundError):                      # 二次确认失败
        docing.promote_tmp_file(meta["token"])


def test_clean_tmp_files_by_age():
    data = LONG.encode("utf-8")
    meta = docing.save_tmp_file(data, "过期暂存.txt", ".txt")
    f = Path(settings.upload_dir) / "tmp" / f"{meta['token']}.txt"
    old = time.time() - 48 * 3600
    os.utime(f, (old, old))
    n = docing.clean_tmp_files(max_age_hours=24)
    assert not f.exists() and n >= 1
