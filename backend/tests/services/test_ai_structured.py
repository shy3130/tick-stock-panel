from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.services.ai_structured.immutable import detect_retry_immutable_drift
from app.services.ai_structured import (
    AIUsage,
    AnalysisArtifact,
    CancellationToken,
    GenerateResponse,
    run_structured_ai,
)
from app.services.ai_structured.parser import parse_json


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    score: int = Field(ge=0, le=100)


class _Rows(RootModel[list[_Payload]]):
    pass


def test_parser_extracts_fenced_json_and_repairs_trailing_comma():
    value, issues = parse_json('prefix ```json\n{"symbol":"600519.SH","score":80,}\n``` suffix')
    assert issues == []
    assert value == {"symbol": "600519.SH", "score": 80}


@pytest.mark.asyncio
async def test_runtime_retries_missing_field_and_accumulates_usage():
    responses = iter(
        [
            GenerateResponse(
                text='[{"symbol":"600519.SH"}]',
                usage=AIUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            ),
            GenerateResponse(
                text='[{"symbol":"600519.SH","score":80}]',
                usage=AIUsage(prompt_tokens=11, cached_prompt_tokens=5, completion_tokens=3, total_tokens=14),
            ),
        ]
    )
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(responses)

    result = await run_structured_ai(
        messages=[{"role": "user", "content": "x"}],
        output_model=_Rows,
        purpose="test",
        generate=generate,
    )
    assert result.status == "ok"
    assert calls == 2
    assert result.data == [{"symbol": "600519.SH", "score": 80}]
    assert result.usage.total_tokens == 26
    assert result.usage.cached_prompt_tokens == 5
    assert [attempt.error_category for attempt in result.attempts] == ["missing", None]


@pytest.mark.asyncio
async def test_immutable_violation_is_invalid_and_bounded_to_one_retry():
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return '{"symbol":"000001.SZ","score":80}'

    result = await run_structured_ai(
        messages=[{"role": "user", "content": "x"}],
        output_model=_Payload,
        purpose="test",
        immutable_context={"symbol": "600519.SH"},
        generate=generate,
    )
    assert result.status == "failed"
    assert result.error is not None and result.error.category == "invalid"
    assert calls == 2


@pytest.mark.asyncio
async def test_quota_and_cancel_do_not_retry():
    quota_calls = 0

    async def quota(*args, **kwargs):
        nonlocal quota_calls
        quota_calls += 1
        raise RuntimeError("quota exceeded")

    quota_result = await run_structured_ai(
        messages=[], output_model=_Payload, purpose="test", generate=quota
    )
    assert quota_result.status == "failed"
    assert quota_result.error is not None and quota_result.error.category == "quota"
    assert quota_calls == 1

    token = CancellationToken()
    token.cancel()
    cancelled = await run_structured_ai(
        messages=[], output_model=_Payload, purpose="test", generate=quota, cancel_token=token
    )
    assert cancelled.status == "cancelled"
    assert quota_calls == 1


def test_analysis_artifact_serializes_without_prompt_content():
    artifact = AnalysisArtifact(
        id="artifact-1",
        attempt_id="att-1",
        request_id="req-1",
        purpose="stock_analysis",
        status="ok",
        data_as_of=datetime(2026, 8, 5, tzinfo=timezone.utc),
        symbol="600519.SH",
        market="a_share",
        adjustment="qfq",
        source_refs=["canonical_enriched"],
        result={"summary": "ok"},
    )
    dumped = artifact.model_dump_json()
    assert "artifact-1" in dumped
    assert "\"messages\"" not in dumped
    assert "\"content\"" not in dumped
    assert "secret" not in dumped.lower()


# ── Parser 增强测试（移植自 PA_Agent） ─────────────────────────────────────


def test_parser_smart_quotes_normalized():
    """智能引号 \u201c\u201d\u2018\u2019 和 en/em-dash 被归一化为 ASCII。"""
    v, i = parse_json('{"key": \u201cvalue\u201d, "dash": \u201c\u2013\u201d}')
    assert i == []
    assert v == {"key": "value", "dash": "-"}



def test_parser_unescaped_quotes_in_string_repaired():
    """字符串值内的未转义引号被 peek-ahead 修复。"""
    v, i = parse_json('{"reason": "he said "hi"", "count": 1}')
    assert i == []
    assert v == {"reason": 'he said "hi"', "count": 1}


def test_parser_semicolon_separator_repaired():
    """字段间的分号被替换为逗号。"""
    v, i = parse_json('{"a": 1; "b": 2}')
    assert i == []
    assert v == {"a": 1, "b": 2}


def test_parser_brace_depth_extracts_first_complete_object():
    """花括号深度跟踪提取首个完整对象，忽略后续含 } 的 prose。"""
    v, i = parse_json('{"x": 1} trailing prose } end')
    assert i == []
    assert v == {"x": 1}


def test_parser_embedded_fence_after_prose():
    """prose 后跟随嵌入式 ```json``` 围栏。"""
    v, i = parse_json('Here is the result:\n```json\n{"score": 42}\n```\nDone.')
    assert i == []
    assert v == {"score": 42}


def test_parser_control_char_in_string_escaped():
    """字符串内的原始换行被转义而非删除。"""
    v, i = parse_json('{"text": "line1\nline2"}')
    assert i == []
    assert v == {"text": "line1\nline2"}


def test_parser_array_with_smart_quotes_and_trailing_comma():
    """数组也受益于 Unicode 归一化和尾逗号清理。"""
    v, i = parse_json('[{"name": \u201cABC\u201d}, {"name": \u201cXYZ\u201d},]')
    assert i == []
    assert v == [{"name": "ABC"}, {"name": "XYZ"}]


def test_parser_plain_text_still_returns_plaintaintext_error():
    """非 JSON 自然语言仍正确分类为 plaintext。"""
    v, i = parse_json("I think the stock is overvalued.")
    assert v is None


@pytest.mark.asyncio
async def test_quota_body_detection_short_circuits_without_retry():
    """HTTP 200 body 是配额错误文本时，不重试，直接返回 quota 失败。"""
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return GenerateResponse(
            text="积分不足，请充值后重试",
            usage=AIUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )

    result = await run_structured_ai(
        messages=[{"role": "user", "content": "x"}],
        output_model=_Payload,
        purpose="test",
        generate=generate,
    )
    assert result.status == "failed"
    assert result.error is not None and result.error.category == "quota"
    assert calls == 1  # 不重试


@pytest.mark.asyncio
async def test_quota_body_english_marker_detected():
    """英文 quota marker 也被检测。"""
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return GenerateResponse(
            text="402 Payment Required: insufficient quota",
            usage=AIUsage(),
        )

    result = await run_structured_ai(
        messages=[],
        output_model=_Payload,
        purpose="test",
        generate=generate,
    )
    assert result.status == "failed"
    assert result.error is not None and result.error.category == "quota"
    assert calls == 1


@pytest.mark.asyncio
async def test_valid_json_not_misclassified_as_quota():
    """正常 JSON 不被 quota 检测误伤。"""
    async def generate(*args, **kwargs):
        return GenerateResponse(
            text='{"symbol":"600519.SH","score":80}',
            usage=AIUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    result = await run_structured_ai(
        messages=[{"role": "user", "content": "x"}],
        output_model=_Payload,
        purpose="test",
        generate=generate,
    )
    assert result.status == "ok"
    assert result.data == {"symbol": "600519.SH", "score": 80}


def test_retry_immutable_drift_rejects_silent_change_but_allows_correction():
    """重试期间不可变事实只能回到程序期望值，不能改成第三个值。"""
    before = {"symbol": "600519.SH", "score": 60}
    changed = {"symbol": "000001.SZ", "score": 80}
    corrected = {"symbol": "600519.SH", "score": 80}
    expected = {"symbol": "600519.SH"}

    violations = detect_retry_immutable_drift(before, changed, expected)
    assert len(violations) == 1
    assert violations[0].path == "symbol"
    assert violations[0].detail["reason"] == "retry_cheat"
    assert detect_retry_immutable_drift(before, corrected, expected) == []
