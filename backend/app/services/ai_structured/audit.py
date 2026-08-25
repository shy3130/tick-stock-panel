"""结构化 AI 审计：仅记录脱敏元数据，不保存完整 prompt。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.ai_structured.models import AIUsage, StructuredAIResult


def redact_messages(messages: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """返回消息数量、角色及稳定 hash；绝不返回消息内容。"""
    normalized = [{"role": str(m.get("role", "")), "length": len(str(m.get("content", "")))} for m in messages]
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return {"count": len(normalized), "roles": [m["role"] for m in normalized], "content_hash": digest}


def build_ai_meta(result: StructuredAIResult) -> dict[str, Any]:
    """对外统一 ``ai_meta`` 对象 (P3 契约)。

    从结构化结果投影出实际使用 profile / fallback / provider / model / usage，
    不含 prompt 正文或凭据。``profile_id`` 为实际命中 profile，``primary_profile_id``
    为请求 profile (默认 profile 解析后即实际，除非发生 fallback)。
    usage 为 provider 原生返回的累计值；不支持 token 计数的 provider 全 0，前端按需展示。
    """
    usage = result.usage or AIUsage()
    return {
        "primary_profile_id": result.primary_profile_id,
        "profile_id": result.profile_id,
        "fallback_used": bool(result.fallback_used),
        "fallback_reason": result.fallback_reason,
        "provider": result.provider,
        "model": result.model,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        },
    }


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
