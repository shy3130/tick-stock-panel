"""结构化 AI 审计：仅记录脱敏元数据，不保存完整 prompt。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.ai_structured.models import StructuredAIResult


def redact_messages(messages: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """返回消息数量、角色及稳定 hash；绝不返回消息内容。"""
    normalized = [{"role": str(m.get("role", "")), "length": len(str(m.get("content", "")))} for m in messages]
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return {"count": len(normalized), "roles": [m["role"] for m in normalized], "content_hash": digest}


def audit_metadata(result: StructuredAIResult, *, messages: Sequence[Mapping[str, object]] | None = None) -> dict[str, Any]:
    """构建可持久化的脱敏 envelope。完整 raw output 也不写入审计记录。"""
    record: dict[str, Any] = {
        "request_id": result.request_id,
        "attempt_id": result.attempt_id,
        "purpose": result.purpose,
        "status": result.status,
        "provider": result.provider,
        "profile_id": result.profile_id,
        "model": result.model,
        "attempts": len(result.attempts),
        "usage": result.usage.model_dump(),
        "elapsed_ms": result.elapsed_ms,
        "warnings": list(result.warnings),
        "error_category": result.error.category if result.error else None,
        "issue_count": sum(len(a.issues) for a in result.attempts),
    }
    if messages is not None:
        record["messages"] = redact_messages(messages)
    return record


# Common names for integration.
build_audit_record = audit_metadata
record_audit_metadata = audit_metadata
