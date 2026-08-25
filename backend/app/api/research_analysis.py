"""研究分析 API — 单标的日收益的风险/绩效/ADF/GARCH（只读 GET）。

契约见 ``local://research-analysis-contract.md``。
仅接受 canonical A 股代码，区间最大 5 年；503 on unavailable，200 on
insufficient，422 on invalid params。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.json_safe import json_safe
from app.services.research_analysis import (
    MAX_RANGE_YEARS,
    _DataUnavailable,
    analyze_symbol_returns,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/research/analysis", tags=["research-analysis"])

#: canonical A 股代码：6 位 ASCII 数字 + SH/SZ/BJ。
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")

_MAX_RANGE_DAYS = MAX_RANGE_YEARS * 365 + 2  # 含最多 2 个闰日（5 年内）


def _validate_symbol(symbol: str) -> None:
    """Reject non-canonical symbols with 422."""
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(
            status_code=422,
            detail="symbol must match ^[0-9]{6}\\.(SH|SZ|BJ)$",
        )


def _resolve_range(
    symbol: str,
    start: date | None,
    end: date | None,
) -> tuple[date, date]:
    """Resolve default window and validate range ordering / max span.

    - ``end`` defaults to today; ``start`` defaults to ``end - 365 days``.
    - ``start > end`` → 422.
    - span > 5 years → 422.
    """
    today = date.today()
    end = today if end is None else end
    start = (end - timedelta(days=365)) if start is None else start

    if start > end:
        raise HTTPException(
            status_code=422,
            detail=f"start ({start}) must be <= end ({end})",
        )
    if (end - start).days > _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"range exceeds {MAX_RANGE_YEARS} years (start={start}, end={end})",
        )
    return start, end


@router.get("/symbol/{symbol}")
def get_symbol_analysis(
    request: Request,
    symbol: str,
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> JSONResponse:
    """单标的日收益的风险 / 绩效 / ADF / GARCH 分析（研究计算，不构成交易建议）。

    - 符号非法 / 日期顺序错误 / 超 5 年 → 422。
    - canonical/repository 不可读 → 503 + 完整 envelope（``available=false``）。
    - 合法但有效收益不足 → 200 + ``available=true``，各段 ``insufficient_data``。
    """
    _validate_symbol(symbol)
    start, end = _resolve_range(symbol, start, end)

    repo = request.app.state.repo
    try:
        raw = analyze_symbol_returns(repo, symbol, start, end)
    except _DataUnavailable as exc:
        envelope = {
            "available": False,
            "source": None,
            "symbol": symbol,
            "start": None,
            "end": None,
            "data_as_of": None,
            "observations": 0,
            "result": None,
            "warnings": [],
            "reason": str(exc),
        }
        return JSONResponse(status_code=503, content=json_safe(envelope))

    payload = json_safe({
        "available": True,
        **raw,
        "reason": None,
    })
    return JSONResponse(status_code=200, content=payload)
