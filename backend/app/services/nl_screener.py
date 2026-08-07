"""Natural-language screener parsing.

The model only proposes literal condition objects.  Registry validation is
always performed locally; this module never executes a query.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from app.services.ai_structured import CancellationToken, build_ai_meta, run_structured_ai
from app.services.ai_budgets import resolve_budget
from app.services.ai_usage_snapshot import record_structured_usage
from app.services.ai_provider import generate_ai_text_with_meta
from app.services.screener_query import (
    FIELD_REGISTRY,
    QueryCondition,
    ScreenerQueryRequest,
    validate_query,
)


# P3: 默认 generate 绑定为 metadata 路径的包装。生产走 meta (fallback+usage);
# 旧测试 monkeypatch ``nl_screener.generate_ai_text`` 注入纯字符串返回时,
# 替换的是这个模块属性, parse_nl 经 ``generate or generate_ai_text`` 命中注入实现。
async def generate_ai_text(messages, **kwargs):  # noqa: A001 - keep public name
    return await generate_ai_text_with_meta(messages, **kwargs)


class NLScreenerError(RuntimeError):
    """Provider or response failure safe to expose at the API boundary."""


class NLParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    profile_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_text(self) -> NLParseRequest:
        self.text = self.text.strip()
        if not 1 <= len(self.text) <= 500:
            raise ValueError("text must contain 1..500 non-whitespace characters")
        if self.profile_id is not None:
            self.profile_id = self.profile_id.strip() or None
        return self


_CLOSED_REASONS = {
    "unknown_field",
    "unavailable_field",
    "unsupported_operator",
    "invalid_value",
    "malformed",
    "excess_conditions",
    "unsortable_field",
    "invalid_order_field",
    "invalid_direction",
}


class _ConditionCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    op: str
    value: Any
    raw: str | None = None


class _ConditionCandidates(RootModel[list[_ConditionCandidate]]):
    """Strict structured output; registry validation remains local."""


def _reason(error: Exception) -> str:
    value = getattr(error, "reason", "malformed")
    return value if value in _CLOSED_REASONS else "malformed"


def _raw_candidate(candidate: dict[str, Any]) -> str:
    raw = candidate.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raw = " ".join(str(candidate.get(k, "")) for k in ("field", "op", "value")).strip()
    return raw.strip()[:200]


def _validate_candidates(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    recognized: list[dict[str, Any]] = []
    unrecognized: list[dict[str, str]] = []
    if len(candidates) > 20:
        return {
            "recognized": [],
            "unrecognized": [
                {"raw": _raw_candidate(c), "reason": "excess_conditions"} for c in candidates[:20]
            ],
        }
    for item in candidates:
        try:
            payload = dict(item)
            payload.pop("raw", None)
            condition = QueryCondition.model_validate(payload)
            req = ScreenerQueryRequest(conditions=[condition])
            applied, _ = validate_query(req)
        except Exception as exc:
            unrecognized.append({"raw": _raw_candidate(item), "reason": _reason(exc)})
            continue
        recognized.extend(applied)
    return {"recognized": recognized, "unrecognized": unrecognized}


def _prompt(text: str) -> list[dict[str, str]]:
    fields = [
        {
            "field": spec.field,
            "label": spec.label,
            "unit": spec.unit,
            "value_type": spec.value_type,
            "ops": list(spec.ops),
            "options": list(spec.options or ()),
        }
        for spec in FIELD_REGISTRY.values()
        if spec.availability == "available"
    ]
    instruction = (
        "Extract only explicit AND conditions from the user's Chinese request. "
        "Return JSON array only. Each item must be {field,op,value,raw}; never invent fields, "
        "SQL, expressions, OR/grouping, sorting, or unavailable data. "
        "Unit rule: change_pct is a decimal fraction, so 5% must be 0.05; other percent fields "
        "use the registry unit as percentage points. "
        f"Registry: {json.dumps(fields, ensure_ascii=False)}\nUser: {text}"
    )
    return [
        {"role": "system", "content": "You are a strict screener condition parser."},
        {"role": "user", "content": instruction},
    ]


def _structured_error_reason(result: Any) -> str:
    category = getattr(getattr(result, "error", None), "category", "malformed")
    if category in {"provider", "quota", "auth"}:
        return "provider_unavailable"
    if category in {"cancelled"}:
        raise asyncio.CancelledError
    return "malformed"


async def parse_nl(
    text: str,
    profile_id: str | None = None,
    *,
    cancel_token: CancellationToken | None = None,
    on_event: Any | None = None,
    generate: Any | None = None,
) -> dict[str, Any]:
    """Parse one request through the shared structured runtime.

    返回值在旧 ``recognized`` / ``unrecognized`` 基础上 additive 追加顶层 ``ai_meta``
    (实际 profile / fallback / usage)，旧消费方忽略该字段即可。
    """
    messages = _prompt(text)
    budget = resolve_budget("nl_screener")
    result = await run_structured_ai(
        messages=messages,
        output_model=_ConditionCandidates,
        purpose="nl_screener",
        profile_id=profile_id,
        cancel_token=cancel_token,
        on_event=on_event,
        generate=generate or generate_ai_text,
        temperature=budget.temperature,
        max_tokens=budget.max_tokens,
        timeout=budget.timeout,
    )
    record_structured_usage("nl_screener", result)
    ai_meta = build_ai_meta(result)
    if getattr(result, "status", None) != "ok" or not getattr(result, "data", None):
        reason = _structured_error_reason(result)
        if reason == "provider_unavailable":
            raise NLScreenerError(reason)
        return {
            "recognized": [],
            "unrecognized": [{"raw": text[:200], "reason": reason}],
            "ai_meta": ai_meta,
        }
    candidates = result.data
    if isinstance(candidates, dict):
        candidates = candidates.get("root", candidates.get("conditions", []))
    if not isinstance(candidates, list):
        return {
            "recognized": [],
            "unrecognized": [{"raw": text[:200], "reason": "malformed"}],
            "ai_meta": ai_meta,
        }
    validated = _validate_candidates(candidates)
    validated["ai_meta"] = ai_meta
    return validated


parse = parse_nl


__all__ = ["NLParseRequest", "NLScreenerError", "parse", "parse_nl"]
