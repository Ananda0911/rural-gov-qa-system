# -*- coding: utf-8 -*-
"""检查 L2 评估后的未答聚合落库（只读副本库）"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect(r"file:eval\tmp_eval.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("--- 未答组（含 L2 新增） ---")
for r in con.execute(
    "select g.id, g.representative_question, g.question_count, g.status, "
    "count(i.id) items from unanswered_groups g "
    "left join unanswered_items i on i.group_id=g.id group by g.id"
):
    print(dict(r))

print("\n--- L2 新增未答实例（qa_record_id>13） ---")
for r in con.execute(
    "select i.group_id, i.question, i.qa_record_id from unanswered_items i where i.qa_record_id>13"
):
    print(dict(r))

print("\n--- L2 落库记录（id>13） ---")
for r in con.execute(
    "select id, status, substr(question,1,24) q, cited_entry_ids from qa_records where id>13"
):
    print(dict(r))
