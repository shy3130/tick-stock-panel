"""AI 助手分时小图必须剔除 nan/inf, 否则 NDJSON 事件无法被前端 JSON.parse。

_intraday_chart_payload 把分钟收盘价 round 后原样放进 charts.points。
停牌/坏源会出现 nan 或 inf 收盘价: Python json.dumps 默认 allow_nan=True,
会写出非法 JSON 词 NaN/Infinity; 前端 JSON.parse 抛 SyntaxError, 整行
tool_result 事件被丢, 分时图不附、后续 delta 也对不上。

日K 小图走 _rows → _clean_value, nan 已变 None; 分时路径直接读分钟分区,
没有同一层清洗。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime

import polars as pl

from app.custom.assistant import tools as assistant_tools

_TRADE_DATE = date(2026, 9, 18)


class _MinuteRepo:
    def __init__(self, closes: list[float], volumes: list[float] | None = None) -> None:
        self.closes = closes
        self.volumes = volumes or [100.0] * len(closes)

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"600519.SH": "贵州茅台"}

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True):
        return pl.DataFrame({"symbol": ["600519.SH"]}), _TRADE_DATE

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        n = len(self.closes)
        return pl.DataFrame({
            "datetime": [
                datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=31 + i)
                for i in range(n)
            ],
            "close": self.closes,
            "volume": self.volumes,
        })


class _QuoteService:
    def get_quotes_compat(self) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["600519.SH"],
            "close": [1500.0],
            "prev_close": [1481.8],
            "change_pct": [0.0123],
        })


def _dump(payload: dict) -> str:
    """与 chat_service._line / tool 回填相同: 默认 json.dumps。再以 allow_nan=False 校验。"""
    text = json.dumps(payload, ensure_ascii=False)
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return text


async def test_intraday_chart_skips_nan_and_inf_close() -> None:
    repo = _MinuteRepo([1490.0, float("nan"), 1500.0, float("inf"), 1501.0])
    ctx = assistant_tools.ToolContext.build(repo=repo, quote_service=_QuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote", {"symbols": ["600519.SH"]}, ctx,
    )

    assert payload["ok"] is True
    _dump(payload)
    points = payload["result"]["charts"][0]["points"]
    closes = [p[1] for p in points]
    assert closes == [1490.0, 1500.0, 1501.0]
    assert all(math.isfinite(v) for v in closes)


async def test_intraday_chart_nonfinite_volume_becomes_zero() -> None:
    repo = _MinuteRepo([1490.0, 1500.0], volumes=[float("nan"), 80.0])
    ctx = assistant_tools.ToolContext.build(repo=repo, quote_service=_QuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote", {"symbols": ["600519.SH"]}, ctx,
    )

    _dump(payload)
    assert payload["result"]["charts"][0]["points"] == [
        ["09:31", 1490.0, 0.0],
        ["09:32", 1500.0, 80.0],
    ]


async def test_intraday_chart_omitted_when_all_closes_nonfinite() -> None:
    repo = _MinuteRepo([float("nan"), float("inf")])
    ctx = assistant_tools.ToolContext.build(repo=repo, quote_service=_QuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote", {"symbols": ["600519.SH"]}, ctx,
    )

    assert payload["ok"] is True
    _dump(payload)
    assert "charts" not in payload["result"]
