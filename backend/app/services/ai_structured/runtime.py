"""结构化 AI runtime：解析、schema/immutable 校验、有限重试、取消与超时。"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.services.ai_structured.immutable import validate_immutable
from app.services.ai_structured.models import (
    AIErrorDetails, AIUsage, AIValidationIssue, AttemptRecord, CancellationToken,
    DEFAULT_RETRY_POLICY, GenerateResponse, Invariant, RetryPolicy, StructuredAIResult,
    new_attempt_id, new_request_id,
)
from app.services.ai_structured.parser import parse_ai_output
from app.services.ai_structured.retry import build_corrective_prompt, should_retry

EventCallback = Callable[[str, dict[str, Any]], Any]
GenerateCallable = Callable[..., Awaitable[str | GenerateResponse] | str]


def _issues_for_validation(value: object, model: type[BaseModel]) -> tuple[BaseModel | None, list[AIValidationIssue]]:
    try:
        parsed = model.model_validate(value)
        return parsed, []
    except ValidationError as exc:
        issues: list[AIValidationIssue] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or None
            category = "missing" if err.get("type") == "missing" else "invalid"
            issues.append(AIValidationIssue(category=category, path=loc, message=str(err.get("msg", "validation failed")), detail={"type": err.get("type")}))
        return None, issues


def _invariant_issues(data: dict[str, Any], invariants: Sequence[Invariant]) -> list[AIValidationIssue]:
    out: list[AIValidationIssue] = []
    for invariant in invariants:
        try:
            value = invariant(data)
        except Exception as exc:  # invariant itself is a validation failure, not provider error
            out.append(AIValidationIssue(category="invalid", message=f"不变量校验异常: {exc}"))
            continue
        if value is None:
            continue
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def _category_from_exception(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "quota" in name or "ratelimit" in name or "rate limit" in text or "quota" in text:
        return "quota"
    if "auth" in name or "permission" in name or "unauthor" in text or "api key" in text:
        return "provider"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in name or "timeout" in text:
        return "provider"
    return "provider"


async def _call_generate(generate: GenerateCallable, messages: list[dict[str, str]], *, profile_id: str | None, temperature: float, max_tokens: int, timeout: float, allow_fallback: bool = True) -> str | GenerateResponse:
    """允许完整 keyword 签名，也兼容旧式 generate(messages)。P3: 透传 allow_fallback 控制是否执行 profile fallback。"""
    try:
        value = generate(messages, profile_id=profile_id, temperature=temperature, max_tokens=max_tokens, timeout=timeout, allow_fallback=allow_fallback)
    except TypeError as exc:
        # 仅在签名不接受 kwargs 时回退，不吞 generate 内部业务 TypeError。
        # 特别兼容 allow_fallback 新增参数（自定义 generate 可能未知）
        msg = str(exc)
        if "allow_fallback" in msg:
            value = generate(messages, profile_id=profile_id, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        elif "unexpected keyword" not in msg and "positional argument" not in msg:
            raise
        else:
            value = generate(messages)
    return await value if inspect.isawaitable(value) else value


def _unpack(value: str | GenerateResponse) -> GenerateResponse:
    if isinstance(value, GenerateResponse):
        return value
    if isinstance(value, str):
        return GenerateResponse(text=value, primary_profile_id=None, fallback_used=False, fallback_reason=None)
    # 兼容 provider 的 duck-typed metadata response + P3 新字段
    return GenerateResponse(
        text=str(getattr(value, "text", "")), usage=getattr(value, "usage", AIUsage()),
        provider=str(getattr(value, "provider", "")), profile_id=getattr(value, "profile_id", None), model=str(getattr(value, "model", "")),
        primary_profile_id=getattr(value, "primary_profile_id", None),
        fallback_used=getattr(value, "fallback_used", False),
        fallback_reason=getattr(value, "fallback_reason", None),
    )


async def _default_generate(messages: list[dict[str, str]], **kwargs: Any) -> GenerateResponse:
    from app.services.ai_provider import generate_ai_text_with_meta
    return await generate_ai_text_with_meta(messages, **kwargs)


async def _emit(callback: EventCallback | None, event_type: str, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    value = callback(event_type, payload)
    if inspect.isawaitable(value):
        await value


async def run_structured_ai(
    *, messages: list[dict[str, str]], output_model: type[BaseModel], purpose: str,
    profile_id: str | None = None, invariants: tuple[Invariant, ...] = (),
    immutable_context: dict[str, object] | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    cancel_token: CancellationToken | None = None,
    on_event: EventCallback | None = None, generate: GenerateCallable | None = None,
    temperature: float = 0.3, max_tokens: int = 3000, timeout: float = 180.0,
) -> StructuredAIResult:
    request_id, attempt_id = new_request_id(), new_attempt_id()
    started = time.monotonic()
    token = cancel_token or CancellationToken()
    total_usage = AIUsage()
    records: list[AttemptRecord] = []
    warnings: list[str] = []
    current = [dict(m) for m in messages]
    format_retries = semantic_retries = 0
    provider = model_name = ""
    last_raw = ""
    last_error: AIErrorDetails | None = None

    # P3: primary always the requested; actual may differ after fallback inside generate (only on first provider attempt)
    # validation/schema retries pin to the actual profile (allow_fallback=False) so corrective stays on same profile
    primary_profile_id: str | None = profile_id
    actual_profile_id: str | None = profile_id
    fb_used: bool = False
    fb_reason: str | None = None

    async def result(status: str, *, data: Any = None, parsed: BaseModel | None = None, error: AIErrorDetails | None = None) -> StructuredAIResult:
        return StructuredAIResult(
            request_id=request_id,
            attempt_id=attempt_id,
            status=status,
            purpose=purpose,
            provider=provider,
            profile_id=actual_profile_id,
            model=model_name,
            primary_profile_id=primary_profile_id,
            fallback_used=fb_used,
            fallback_reason=fb_reason,
            data=data,
            parsed_model=parsed,
            raw_text=last_raw,
            attempts=records,
            usage=total_usage,
            error=error,
            warnings=warnings,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def cancelled_result() -> StructuredAIResult:
        await _emit(
            on_event,
            "attempt_cancelled",
            {"request_id": request_id, "attempt_id": attempt_id},
        )
        return await result(
            "cancelled",
            error=AIErrorDetails(category="cancelled", message="structured AI request cancelled"),
        )

    try:
        token.raise_if_cancelled()
        await _emit(on_event, "preflight_completed", {"request_id": request_id, "attempt_id": attempt_id})
        gen = generate or _default_generate
        index = 0
        while True:
            token.raise_if_cancelled()
            await _emit(on_event, "attempt_started", {"request_id": request_id, "attempt_id": attempt_id, "attempt_index": index})
            await _emit(on_event, "stage_started", {"request_id": request_id, "attempt_id": attempt_id, "attempt_index": index, "stage": "provider"})
            attempt_started = time.monotonic()
            try:
                # P3: first attempt allows fallback (generate will resolve chain); corrective retries pin to actual and disable fb
                use_allow_fb = (index == 0)
                pid_to_use = profile_id if index == 0 else actual_profile_id
                response = await asyncio.wait_for(
                    _call_generate(gen, current, profile_id=pid_to_use, temperature=temperature, max_tokens=max_tokens, timeout=timeout, allow_fallback=use_allow_fb),
                    timeout=timeout,
                )
                token.raise_if_cancelled()
            except asyncio.CancelledError:
                return await cancelled_result()
            except BaseException as exc:
                category = _category_from_exception(exc)
                detail = AIErrorDetails(category=category, message=str(exc) or type(exc).__name__)
                records.append(AttemptRecord(index=index, raw_text="", usage=AIUsage(), error_category=category, elapsed_ms=int((time.monotonic() - attempt_started) * 1000)))
                last_error = detail
                await _emit(
                    on_event,
                    "attempt_failed",
                    {
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "category": category,
                        "attempt_index": index,
                    },
                )
                return await result("failed", error=detail)
            unpacked = _unpack(response)
            last_raw, provider, model_name = unpacked.text, unpacked.provider, unpacked.model
            if index == 0:
                # capture from the (possibly fallback) response
                primary_profile_id = getattr(unpacked, "primary_profile_id", None) or profile_id
                actual_profile_id = unpacked.profile_id or profile_id
                fb_used = getattr(unpacked, "fallback_used", False)
                fb_reason = getattr(unpacked, "fallback_reason", None)
            total_usage = total_usage.add(unpacked.usage)
            await _emit(on_event, "token_usage_updated", {"request_id": request_id, "attempt_id": attempt_id, "attempt_index": index, "usage": total_usage.model_dump()})
            value, issues = parse_ai_output(last_raw)
            parsed_model: BaseModel | None = None
            if value is not None:
                parsed_model, model_issues = _issues_for_validation(value, output_model)
                issues.extend(model_issues)
                if parsed_model is not None and isinstance(value, dict):
                    issues.extend(validate_immutable(value, immutable_context))
                    issues.extend(_invariant_issues(value, invariants))
            category = str(issues[0].category) if issues else ""
            record = AttemptRecord(index=index, raw_text=last_raw, usage=unpacked.usage, issues=issues, error_category=category or None, elapsed_ms=int((time.monotonic() - attempt_started) * 1000))
            records.append(record)
            if not issues and parsed_model is not None:
                data = parsed_model.model_dump(mode="json")
                await _emit(
                    on_event,
                    "stage_completed",
                    {
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "attempt_index": index,
                        "stage": "provider",
                    },
                )
                await _emit(
                    on_event,
                    "attempt_completed",
                    {
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "attempt_index": index,
                    },
                )
                return await result("ok", data=data, parsed=parsed_model)
            await _emit(on_event, "validation_failed", {"request_id": request_id, "attempt_id": attempt_id, "attempt_index": index, "issues": [i.model_dump() for i in issues]})
            if not should_retry(category, format_retries=format_retries, semantic_retries=semantic_retries, policy=retry_policy):
                last_error = AIErrorDetails(
                    category=category or "invalid",
                    message="structured output validation failed",
                    issues=issues,
                )
                await _emit(
                    on_event,
                    "attempt_failed",
                    {
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "category": category or "invalid",
                        "attempt_index": index,
                    },
                )
                return await result("failed", error=last_error)
            if category in {"syntax", "plaintext", "missing"}:
                format_retries += 1
            else:
                semantic_retries += 1
            token.raise_if_cancelled()
            await _emit(on_event, "retry_started", {"request_id": request_id, "attempt_id": attempt_id, "attempt_index": index + 1, "reason": category})
            if retry_policy.backoff_seconds > 0:
                await asyncio.sleep(retry_policy.backoff_seconds)
            current = current + [{"role": "user", "content": build_corrective_prompt(issues)}]
            index += 1
    except asyncio.CancelledError:
        return await cancelled_result()
