"""程序注入事实的不可变字段校验。"""
from __future__ import annotations

from typing import Any

from app.services.ai_structured.models import AIValidationIssue

_MISSING = object()


def _get_path(value: Any, path: str) -> Any:
    cur = value
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        else:
            cur = getattr(cur, part, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


def validate_immutable(
    data: dict[str, Any], expected: dict[str, object] | None,
) -> list[AIValidationIssue]:
    """校验点路径/简单字段期望；缺失或漂移均拒绝输出，不覆盖程序事实。"""
    if not expected:
        return []
    issues: list[AIValidationIssue] = []
    for path, want in expected.items():
        got = _get_path(data, path)
        if got is _MISSING:
            issues.append(AIValidationIssue(category="invalid", path=path, message="不可变程序事实缺失", detail={"expected": want}))
        elif got != want:
            issues.append(AIValidationIssue(category="invalid", path=path, message="不可变程序事实被修改", detail={"expected": want, "actual": got}))
    return issues


# Public aliases used by callers/tests.
check_immutable = validate_immutable
check_immutable_fields = validate_immutable
