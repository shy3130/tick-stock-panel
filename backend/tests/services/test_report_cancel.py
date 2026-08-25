"""Report 流在 CancellationToken 置位后必须立刻停，不得进 LLM。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.ai_structured import CancellationToken
from app.services.financial_analyzer import analyze_financials_stream
from app.services.market_recap import recap_market_stream


@pytest.mark.asyncio
async def test_financials_stream_stops_when_cancelled(tmp_path: Path):
    token = CancellationToken()
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        async for _ in analyze_financials_stream(tmp_path, "000001.SZ", cancel_token=token):
            pass


@pytest.mark.asyncio
async def test_recap_stream_stops_when_cancelled():
    token = CancellationToken()
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        async for _ in recap_market_stream(repo=None, cancel_token=token):
            pass
