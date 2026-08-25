"""自定义信号 NL → 结构化草稿。

复用 run_structured_ai / ai_budgets / ai_usage_snapshot / generate_ai_text_with_meta 模式（来自 nl_screener）。
模型只返回字面量结构；本地 custom_signals.validate 作为结构不变量强校验（不信任 pydantic）。
绝不持久化、绝不执行表达式、绝不自动启用监控。
服务允许注入 generate 便于 deterministic 测试。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.ai_structured import build_ai_meta, run_structured_ai
from app.services.ai_budgets import resolve_budget
from app.services.ai_usage_snapshot import record_structured_usage
from app.services.ai_provider import generate_ai_text_with_meta
from app.strategy import custom_signals


async def generate_ai_text(messages, **kwargs):  # noqa: A001
    """默认包装，走带 meta 的路径，便于 fallback/usage。"""
    return await generate_ai_text_with_meta(messages, **kwargs)


class CustomSignalDraftError(RuntimeError):
    """provider/quota/auth 等不可用时抛出，API 层映射为 503。"""

    pass


class _Condition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    left: str
    op: str
    right: Any


class _Draft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    name: str = ""
    kind: str = "both"
    conditions: list[_Condition] = Field(default_factory=list)
    rationale: str | None = None


class CustomSignalDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    profile_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_text(self) -> CustomSignalDraftRequest:
        self.text = self.text.strip()
        if not 1 <= len(self.text) <= 500:
            raise ValueError("text must contain 1..500 non-whitespace characters")
        if self.profile_id is not None:
            self.profile_id = self.profile_id.strip() or None
        return self


def _prompt(text: str) -> list[dict[str, str]]:
    fields = sorted(custom_signals.ALLOWED_FIELDS)
    ops = sorted(custom_signals.OPS)
    instruction = (
        "Extract a custom signal definition from the user's Chinese natural language request. "
        "Return ONLY a JSON object with keys: id, name, kind ('entry'|'exit'|'both'), "
        "conditions (array of 1..8 objects each {left, op, right}), optional rationale. "
        "id: lowercase letters/digits/underscore, 1-40 chars. name: short descriptive. "
        f"left MUST be one of: {', '.join(fields)}. "
        f"op MUST be one of: {', '.join(ops)}. "
        "right: number literal OR 'field:XXX' where XXX is also an allowed field. "
        "NEVER output Python code, SQL, formulas, comments, or executable text. "
        "Only pure JSON literal structure. "
        f"User request: {text}"
    )
    return [
        {
            "role": "system",
            "content": "You are a strict extractor that outputs only the requested JSON literal structure. No prose.",
        },
        {"role": "user", "content": instruction},
    ]


def _structured_error_reason(result: Any) -> str:
    category = getattr(getattr(result, "error", None), "category", "malformed")
    if category in {"provider", "quota", "auth"}:
        return "provider_unavailable"
    if category in {"cancelled"}:
        raise asyncio.CancelledError
    return "malformed"


async def generate_custom_signal_draft(
    text: str,
    profile_id: str | None = None,
    *,
    generate: Any | None = None,
) -> dict[str, Any]:
    """自然语言 → 结构化草稿。

    - 注入 generate= 便于测试 deterministic 返回。
    - 预算 temperature=0, max_tokens<=1800, timeout<=60s。
    - 结构化后用 custom_signals.validate 做最终不变量校验（拒绝未知 field/op/非法 right / 超8条）。
    - 只返回草稿，不写盘，不执行。
    - AI 建议的 id 若与现有冲突，由调用方（API）负责后缀化返回的 draft。
    """
    req = CustomSignalDraftRequest(text=text, profile_id=profile_id)
    messages = _prompt(req.text)
    budget = resolve_budget("custom_signal_draft")
    result = await run_structured_ai(
        messages=messages,
        output_model=_Draft,
        purpose="custom_signal_draft",
        profile_id=req.profile_id,
        generate=generate or generate_ai_text,
        temperature=budget.temperature,
        max_tokens=budget.max_tokens,
        timeout=budget.timeout,
    )
    record_structured_usage("custom_signal_draft", result)
    ai_meta = build_ai_meta(result)

    if getattr(result, "status", None) != "ok" or not getattr(result, "data", None):
        reason = _structured_error_reason(result)
        if reason == "provider_unavailable":
            raise CustomSignalDraftError(reason)
        raise CustomSignalDraftError(reason)

    raw = result.data
    if isinstance(raw, dict):
        d = raw
    else:
        d = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else dict(raw)

    # 白名单投影但保留非法值给最终 validate 拒绝；超 8 条不得截断后悄悄接受
    raw_conds = d.get("conditions")
    if not isinstance(raw_conds, list):
        raw_conds = []
    if len(raw_conds) > 8:
        raise CustomSignalDraftError("invalid_structure: conditions 最多 8 条")
    conditions: list[dict] = []
    for c in raw_conds:
        if isinstance(c, dict):
            right = c.get("right")
            conditions.append(
                {
                    "left": c.get("left"),
                    "op": c.get("op"),
                    "right": str(right) if isinstance(right, (int, float)) else right,
                }
            )

    draft = {
        "id": d.get("id", ""),
        "name": str(d.get("name", "")).strip()[:80],
        "kind": d.get("kind"),
        "conditions": conditions,
    }

    # 强不变量校验（本地，绝不信任模型输出结构）
    try:
        custom_signals.validate(draft)
    except ValueError as exc:
        raise CustomSignalDraftError(f"invalid_structure: {exc}") from exc

    rationale = d.get("rationale")
    if isinstance(rationale, str):
        rationale = rationale.strip() or None
    else:
        rationale = None

    return {"draft": draft, "rationale": rationale, "ai_meta": ai_meta}


def _ensure_unique_draft_id(draft: dict, existing: set[str]) -> dict:
    """返回不覆盖既有信号的合法 id；长 id 也必须在 40 字符内终止。"""
    result = dict(draft)
    original = str(result.get("id", "") or "").strip()
    if original and custom_signals.ID_RE.match(original) and original not in existing:
        return result

    base = original if custom_signals.ID_RE.match(original) else "ai_sig"
    index = 1
    while True:
        suffix = f"_{index}"
        stem = base[: 40 - len(suffix)].rstrip("_") or "ai"
        candidate = f"{stem}{suffix}"
        if candidate not in existing:
            result["id"] = candidate
            return result
        index += 1


__all__ = [
    "CustomSignalDraftRequest",
    "CustomSignalDraftError",
    "generate_custom_signal_draft",
    "_ensure_unique_draft_id",
]
