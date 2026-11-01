"""操作留痕：append-only 写入（表上触发器禁 UPDATE/DELETE 兜底）"""
import json

from .models import OperationLog, now_str


def log_action(db, operator, action: str, object_type: str | None = None,
               object_id: int | None = None, detail: dict | None = None) -> None:
    """operator：User 或 None（系统定时任务，operator_name 记为「系统」）"""
    db.add(OperationLog(
        operator_id=operator.id if operator else None,
        operator_name=operator.display_name if operator else "系统",
        action=action,
        object_type=object_type,
        object_id=object_id,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
        created_at=now_str(),
    ))
