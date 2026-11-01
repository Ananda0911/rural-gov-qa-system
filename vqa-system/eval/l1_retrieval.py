# -*- coding: utf-8 -*-
"""L1 检索层评估：在库副本上，用真实 embedding API 评估混合检索质量
指标：hit@1/3/5、MRR、T1 三态判定混淆矩阵、单路(FTS/向量) vs RRF 融合对比
只读库副本，不产生任何写操作（hybrid_retrieve 本身不落库）
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

# 在导入 app 之前覆盖 DB 路径（pydantic-settings 环境变量优先于 .env）
os.environ["DB_PATH"] = str(DB)
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8")

if not DB.exists():
    shutil.copy2(SRC, DB)
    # WAL 侧文件一并清理，保证副本干净
    for side in ("-wal", "-shm"):
        p = Path(str(DB) + side)
        if p.exists():
            p.unlink()

from app import search, vector_store          # noqa: E402
from app.config import settings               # noqa: E402
from app.db import SessionLocal               # noqa: E402
from app.models import KbEntry                # noqa: E402

T1 = settings.t1_relevance

# ---------------- 评估问题集 ----------------
# gold: 应命中的条目 id 集合；expect_relevant: 该问检索层应判"相关"（A 类）还是"不相关"（B 类）
A_SET = [  # 应命中（库内有依据条目）
    ("a1",  "申请低保需要准备哪些材料", {1, 6}),
    ("a2",  "低保标准每个月是多少钱", {6}),
    ("a3",  "耕地地力保护补贴怎么申领", {2}),
    ("a4",  "种粮补贴什么时候发放", {2}),
    ("a5",  "村里的财务收支在哪里可以查到", {3}),
    ("a6",  "宅基地申请流程是什么", {4}),
    ("a7",  "危房改造每户能补多少钱", {5}),
    ("a8",  "危房改造申请到批复要多长时间", {5}),
    ("a9",  "医保今年要交多少钱", {7}),
    ("a10", "医保异地就医怎么报销", {7}),
    ("a11", "低保多长时间核查一次", {6}),
    ("a12", "危房改造哪些人可以申请", {5}),
]
B_SET = [  # 应未命中（库内无依据，且不在敏感规则内）
    ("b1", "明年居民养老金涨多少"),
    ("b2", "村里路灯不亮了找谁反映"),
    ("b3", "农村养老保险怎么缴费"),   # 近域探针：库有医保、无养老
    ("b4", "孩子上学学区怎么划分"),
]


async def eval_one(db, qid, question, gold=None, expect_relevant=None):
    tokens = search.jieba_tokens(question)
    fts_ids = search.fts_search(db, tokens)
    qvec = await search.embed_question(question)
    vec_ids = search.vector_search_sync(db, qvec) if qvec is not None else []
    retrieved = await search.hybrid_retrieve(db, question)
    cands = retrieved["candidates"]

    # 复现管线 ④ 的 T1 判定逻辑（Top1 向量相似度 ≥ T1 或强 FTS 匹配）
    t1_hit = False
    top1 = cands[0] if cands else None
    if top1 is not None:
        if top1["sim"] is not None and top1["sim"] >= T1:
            t1_hit = True
        else:
            e = db.get(KbEntry, top1["id"])
            if e and search.strong_fts_match(db, e, tokens):
                t1_hit = True

    rrf_ids = [c["id"] for c in cands]
    rec = {
        "qid": qid, "question": question, "gold": sorted(gold) if gold else [],
        "expect_relevant": expect_relevant,
        "tokens": tokens[:8],
        "fts_top3": fts_ids[:3],
        "vec_top3": [(eid, round(s, 4)) for eid, s in vec_ids[:3]],
        "rrf_top5": rrf_ids,
        "top1_sim": round(top1["sim"], 4) if top1 and top1["sim"] is not None else None,
        "t1_pass": t1_hit,
        "degraded": retrieved["degraded"],
    }
    if gold:
        rec["hit1"] = any(i in gold for i in rrf_ids[:1])
        rec["hit3"] = any(i in gold for i in rrf_ids[:3])
        rec["hit5"] = any(i in gold for i in rrf_ids[:5])
        rec["mrr"] = next((1.0 / (i + 1) for i, eid in enumerate(rrf_ids) if eid in gold), 0.0)
        rec["fts_only_hit1"] = any(i in gold for i in fts_ids[:1])
        rec["vec_only_hit1"] = any(eid in gold for eid, _ in vec_ids[:1])
    return rec


async def main():
    db = SessionLocal()
    ok = vector_store.rebuild(db)
    print(f"FAISS 镜像: {vector_store.stats() if ok else '不可用'} | T1={T1} | mock={settings.llm_mock}\n")

    results = []
    for qid, q, gold in A_SET:
        results.append(await eval_one(db, qid, q, gold=gold, expect_relevant=True))
    for qid, q in B_SET:
        results.append(await eval_one(db, qid, q, expect_relevant=False))
    db.close()

    # ---------------- 明细输出 ----------------
    print(f"{'id':<4}{'问题':<18}{'RRF Top5':<18}{'Top1 sim':<9}{'T1':<4}{'@1':<4}{'@3':<4}{'@5':<4}{'MRR'}")
    for r in results:
        print(f"{r['qid']:<4}{r['question']:<18}{str(r['rrf_top5']):<18}"
              f"{str(r['top1_sim']):<9}{('√' if r['t1_pass'] else '×'):<4}"
              f"{str(r.get('hit1', '-')):<4}{str(r.get('hit3', '-')):<4}"
              f"{str(r.get('hit5', '-')):<4}{r.get('mrr', '-')}")
        print(f"     FTS top3={r['fts_top3']}  向量 top3={r['vec_top3']}")

    # ---------------- 汇总指标 ----------------
    a = [r for r in results if r["expect_relevant"]]
    b = [r for r in results if not r["expect_relevant"]]
    n = len(a)
    hit1 = sum(r["hit1"] for r in a)
    hit3 = sum(r["hit3"] for r in a)
    hit5 = sum(r["hit5"] for r in a)
    mrr = sum(r["mrr"] for r in a) / n
    print("\n=== 检索层汇总（A 类应命中 12 问）===")
    print(f"hit@1={hit1}/{n}  hit@3={hit3}/{n}  hit@5={hit5}/{n}  MRR={mrr:.3f}")
    print(f"仅 FTS 路 hit@1={sum(r['fts_only_hit1'] for r in a)}/{n}  "
          f"仅向量路 hit@1={sum(r['vec_only_hit1'] for r in a)}/{n}  "
          f"RRF 融合 hit@1={hit1}/{n}")

    tp = sum(r["t1_pass"] for r in a)          # A 类判相关（正确放行）
    fn = n - tp                                 # A 类判不相关（漏召回→过度拒答）
    fp = sum(r["t1_pass"] for r in b)           # B 类判相关（误放行→幻觉风险）
    tn = len(b) - fp                            # B 类判不相关（正确拒答）
    print(f"\n=== T1 三态判定混淆矩阵（阈值 {T1}）===")
    print(f"应命中: TP={tp}  FN(漏召回)={fn}   应未命中: TN={tn}  FP(误放行)={fp}")
    if (tp + fp):
        print(f"放行精确率={tp/(tp+fp):.2f}  拒答召回率(TN率)={tn/len(b):.2f}")

    out = BASE / "eval" / "l1_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已存 {out}")


if __name__ == "__main__":
    asyncio.run(main())
