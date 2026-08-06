"""结构化输出重试分类与 corrective prompt。"""
from __future__ import annotations

from collections.abc import Iterable

from app.services.ai_structured.models import AIErrorCategory, AIValidationIssue, RetryPolicy

_FORMAT = {"syntax", "plaintext", "missing"}
_SEMANTIC = {"invalid"}
_NO_CONTENT_RETRY = {"quota", "cancelled", "provider"}


def retry_kind(category: str) -> str | None:
    if category in _FORMAT:
        return "format"
    if category in _SEMANTIC:
        return "semantic"
    return None


def should_retry(
    category: str,
    *,
    format_retries: int = 0,
    semantic_retries: int = 0,
    policy: RetryPolicy,
) -> bool:
    """按策略判断是否允许下一次内容尝试；provider/quota/cancel 永不内容重试。"""
    if category in _NO_CONTENT_RETRY:
        return False
    if category in _FORMAT:
        return format_retries < policy.max_format_retries
    if category in _SEMANTIC:
        return semantic_retries < policy.max_semantic_retries
    return False


def build_corrective_prompt(issues: Iterable[AIValidationIssue]) -> str:
    details = []
    for issue in issues:
        location = f"字段 {issue.path}" if issue.path else "输出"
        details.append(f"- {location}: {issue.message}")
    return (
        "上一次输出未通过结构化校验。请仅返回符合既定 schema 的 JSON，不要解释或 markdown fence；"
        "不要修改程序注入的不可变事实。具体问题：\n" + "\n".join(details)
    )


# Compatibility alias.
corrective_prompt = build_corrective_prompt
