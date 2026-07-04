from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from typing import Any

import polars as pl

_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|INDEX|ETF)$")

TOOLS = [
    {
        "name": "get_capabilities",
        "description": "Return current data/provider capability labels.",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "list_strategies",
        "description": "Return available screener strategy ids and names.",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "get_kline",
        "description": "Return recent daily kline rows for one symbol.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}}},
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}}},
        "read_only": True,
    },
    {
        "name": "run_screener",
        "description": "Run the local screener with supplied conditions.",
        "input_schema": {"type": "object", "properties": {"conditions": {"type": "array"}, "limit": {"type": "integer"}}},
        "parameters": {"type": "object", "properties": {"conditions": {"type": "array"}, "limit": {"type": "integer"}}},
        "read_only": True,
    },
    {
        "name": "run_backtest",
        "description": "Run a bounded strategy backtest without saving strategy definitions.",
        "input_schema": {"type": "object", "properties": {"strategy_id": {"type": "string"}, "symbols": {"type": "array"}}},
        "parameters": {"type": "object", "properties": {"strategy_id": {"type": "string"}, "symbols": {"type": "array"}}},
        "read_only": True,
    },
    {
        "name": "get_market_overview",
        "description": "Return compact market overview.",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "list_ext_data",
        "description": "Return configured extension datasets.",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
]


def call_tool(name: str, app_state: Any, args: dict | None = None) -> dict:
    args = args or {}
    if name == "get_capabilities":
        capset = getattr(app_state, "capabilities", None)
        return {"capabilities": sorted(capset.all()) if capset else []}
    if name == "list_strategies":
        engine = getattr(app_state, "strategy_engine", None)
        rows = []
        if engine is not None:
            for item in engine.list_strategies():
                strategy_id = str(item.get("id") or "")
                rows.append({
                    "id": strategy_id,
                    "name": item.get("name") or strategy_id,
                    "source": item.get("source") or "unknown",
                    "tags": item.get("tags") or [],
                })
        return {"strategies": rows[:200]}
    if name == "get_kline":
        repo = _require(app_state, "repo")
        symbol = str(args.get("symbol") or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("invalid symbol")
        limit = max(1, min(200, int(args.get("limit") or 60)))
        end = date.today()
        start = end - timedelta(days=limit * 3)
        df = repo.get_daily(symbol, start, end, ["date", "open", "high", "low", "close", "volume"])
        return _truncate({"rows": _df_rows(df.tail(limit))})
    if name == "run_screener":
        repo = _require(app_state, "repo")
        from app.services.screener import ScreenerService

        svc = ScreenerService(repo)
        as_of = args.get("as_of") or svc.latest_date()
        if not as_of:
            raise ValueError("no enriched data available")
        result = svc.run(
            as_of=as_of,
            conditions=list(args.get("conditions") or []),
            order_by=args.get("order_by"),
            limit=max(1, min(200, int(args.get("limit") or 50))),
            pool=args.get("pool"),
        )
        return _truncate(_plain(result))
    if name == "run_backtest":
        repo = _require(app_state, "repo")
        strategy_engine = _require(app_state, "strategy_engine")
        from app.backtest.engine import BacktestEngine
        from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

        strategy_id = str(args.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError("strategy_id required")
        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)
        result = StrategyBacktestService(BacktestEngine(repo), strategy_engine).run(StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=args.get("symbols"),
            start=start,
            end=end,
        ))
        return _truncate(_plain(result))
    if name == "get_market_overview":
        repo = _require(app_state, "repo")
        from app.services.market_overview_builder import build_market_overview

        return _truncate(build_market_overview(repo=repo, quote_service=getattr(app_state, "quote_service", None), depth_service=getattr(app_state, "depth_service", None)))
    if name == "list_ext_data":
        repo = _require(app_state, "repo")
        from app.services.ext_data import ExtConfigStore

        configs = ExtConfigStore(repo.store.data_dir).load_all()
        return {"datasets": [{"id": c.id, "label": c.label, "mode": c.mode, "fields": [f.name for f in c.fields]} for c in configs[:200]]}
    raise ValueError(f"unknown agent tool: {name}")


def _require(app_state: Any, attr: str):
    value = getattr(app_state, attr, None)
    if value is None:
        raise ValueError(f"tool requires app_state.{attr}")
    return value


def _df_rows(df: pl.DataFrame) -> list[dict]:
    return [{k: str(v) if k == "date" else v for k, v in row.items()} for row in df.to_dicts()]


def _truncate(payload: dict, max_chars: int = 20_000) -> dict:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return payload
    return {"truncated": True, "preview": text[:max_chars]}


def _plain(value) -> dict:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return dict(value)
