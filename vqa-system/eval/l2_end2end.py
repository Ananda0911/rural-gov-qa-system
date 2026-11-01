# -*- coding: utf-8 -*-
"""L2 端到端评估：真实 LLM（qwen-plus + text-embedding-v4）走完整 run_ask 八环节管线
在库副本上执行（会写 qa_records/unanswered_*，不污染生产库）
覆盖：三态正确性、引用合法性、事实一致性关键点、卡片结构、落库校验
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data" / "app.db"
DB = BASE / "eval" / "tmp_eval.db"

os.environ["DB_PATH"] = str(DB)
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8")

if not DB.exists():
    shutil.copy2(SRC, DB)
for side in ("-wal", "-shm"):
    p = Path(str(DB) + side)
    if p.exists():
        p.unlink()

from app import llm, pipeline                     # noqa: E402
from app.db import SessionLocal                   # noqa: E402
from app.models import QaRecord, User             # noqa: E402

# API 调用计数
CALLS = {"chat": 0, "embed": 0}
_chat, _embed = llm.chat, llm.embed


async def chat_counted(*a, **kw):
    CALLS["chat"] += 1
    return await _chat(*a, **kw)


async def embed_counted(*a, **kw):
    CALLS["embed"] += 1
    return await _embed(*a, **kw)


llm.chat, llm.embed = chat_counted, embed_counted
pipeline.llm.chat, pipeline.llm.embed = chat_counted, embed_counted

# ---------------- 端到端问题集 ----------------
# expect: 期望三态；gold: 允许引用的条目；check: 事实一致性关键点（人工核对）
CASES = [
    ("a1",  "申请低保需要准备哪些材料", "answered", {1, 6}, "申请书/户口簿/收入证明"),
    ("a7",  "危房改造每户能补多少钱", "answered", {5}, "D级12000元 / C级6000元"),
    ("a9",  "医保今年要交多少钱", "answered", {7}, "400元/年（L1 已预测 FN，验证端到端表现）"),
    ("a10", "医保异地就医怎么报销", "answered", {7}, "备案 / 未备案降20个百分点"),
    ("b1",  "明年居民养老金涨多少", "missed", None, "卡片：related+referral"),
    ("b3",  "农村养老保险怎么缴费", "missed", None, "近域探针：库有医保无养老"),
    ("c1",  "邻居欠我钱不还想起诉他", "out_of_scope", None, "涉法涉诉"),
    ("c2",  "老人咳嗽半个月了该吃什么药", "out_of_scope", None, "医疗诊断"),
    ("c3",  "村干部贪污低保金我要举报他", "out_of_scope", None, "信访举报（含'低保'仍应先拦截）"),
    ("c4",  "亲戚让我做担保该不该做", "out_of_scope", None, "涉资金个人决策"),
    ("d1",  "低保和危房改造能同时申请吗", "answered", {5, 6}, "跨条目组合问答"),
    ("d2",  "粮食直补一亩地能补多少钱", "missed", None, "检索会命中条目2但无金额→sufficient=false"),
]


async def main():
    db = SessionLocal()
    user = db.query(User).filter(User.username == "admin").first()
    recs_before = db.query(QaRecord).count()

    results = []
    for qid, q, expect, gold, note in CASES:
        r = await pipeline.run_ask(db, user, q)
        status = r.get("status")
        row = {"qid": qid, "question": q, "expect": expect, "actual": status, "ok": status == expect}
        if status == "answered":
            cited = [c["id"] for c in r.get("citations", [])]
            row["cited"] = cited
            row["cite_legal"] = (not gold) or all(c in gold for c in cited)
            row["has_disclaimer"] = bool(r.get("disclaimer"))
            row["snippet_len"] = [len(c.get("snippet", "")) for c in r.get("citations", [])]
            row["answer"] = r["answer"]
        elif status == "missed":
            row["related"] = [c["title"] for c in r.get("related", [])]
            row["referral"] = r.get("referral", {}).get("org")
            row["degraded"] = r.get("degraded")
            row["message"] = r.get("message", "")[:40]
        else:
            row["category"] = r.get("category")
            row["has_advice"] = bool(r.get("advice"))
        row["note"] = note
        results.append(row)
        flag = "OK " if row["ok"] else "✗✗ "
        print(f"{flag}{qid:<4}{q:<18} 期望={expect:<12} 实际={status:<12} {note}")

    # 落库校验：12 问应产生 12 条 qa_records，missed 应有 unanswered_items
    recs_after = db.query(QaRecord).count()
    from app.models import UnansweredItem
    ua_items = db.query(UnansweredItem).count()
    print(f"\n落库: qa_records {recs_before}→{recs_after}（新增 {recs_after - recs_before}，应 12）"
          f"  unanswered_items 总数 {ua_items}")
    per_status = {}
    for r in db.query(QaRecord).filter(QaRecord.id > 0).all():
        per_status[r.status] = per_status.get(r.status, 0) + 1
    print(f"qa_records 全量状态分布: {per_status}")
    db.close()

    n = len(results)
    acc = sum(r["ok"] for r in results)
    print(f"\n=== 三态一致率: {acc}/{n} ===")
    for r in results:
        if not r["ok"]:
            print(f"  不一致: {r['qid']} {r['question']} 期望 {r['expect']} 实际 {r['actual']}")
    answered = [r for r in results if r["actual"] == "answered"]
    print(f"answered 引用合法性: {sum(r.get('cite_legal', True) for r in answered)}/{len(answered)}")
    print(f"API 调用: chat={CALLS['chat']} embed={CALLS['embed']}")

    out = BASE / "eval" / "l2_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细已存 {out}")


if __name__ == "__main__":
    asyncio.run(main())
