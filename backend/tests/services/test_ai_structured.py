from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel

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
