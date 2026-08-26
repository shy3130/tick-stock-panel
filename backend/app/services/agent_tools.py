from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from typing import Any

import polars as pl

from app.log_redaction import redact_text

_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|INDEX|ETF)$")
_PATH_IN_ERROR_RE = re.compile(r"[/\\][^\s\"']*")

_SCREEN_STOCK_POOL_SCHEMA = {
    "type": "object",
    "properties": {
        "preset_id": {"type": "string", "enum": ["short_momentum_quality_v1"]},
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "op": {"type": "string"},
                    "value": {},
                },
                "required": ["field", "op", "value"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 20,
        },
        "as_of": {"type": "string", "description": "YYYY-MM-DD；省略时使用最新可信交易日"},
        "order_by": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "direction": {"type": "string", "enum": ["asc", "desc"]},
            },
            "required": ["field"],
            "additionalProperties": False,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "legacy 分支允许 1..500；preset 分支由服务端收紧为 5..12，默认 8",
        },
    },
    "additionalProperties": False,
}


TOOLS = [
    {
        "name": "get_capabilities",
        "description": "返回当前数据源/能力标签。可用本工具确认系统具备哪些数据能力。",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "list_strategies",
        "description": "列出所有可选股策略（内置预设 + 自定义），返回 id/名称/标签。配合 start_pool_backtest 使用。",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "get_kline",
        "description": "查询单只标的最近 N 个交易日的日 K 线（开高低收/成交量）。数据来自本地 DuckDB。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "标的代码，如 600519.SH / 000001.SZ / 00700.HK",
                },
                "limit": {"type": "integer", "description": "返回最近多少天，默认 60，上限 200"},
            },
        },
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "标的代码，如 600519.SH / 000001.SZ / 00700.HK",
                },
                "limit": {"type": "integer", "description": "返回最近多少天，默认 60，上限 200"},
            },
        },
        "read_only": True,
    },
    {
        "name": "list_screener_fields",
        "description": (
            "列出强类型条件选股可用字段、类型、运算符和分组。"
            "在不确定 field/op/value 时先调用本工具；不得自行构造 SQL。"
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "read_only": True,
    },
    {
        "name": "screen_stock_pool",
        "description": (
            "强类型条件选股并保存服务端股票池；或传 preset_id=short_momentum_quality_v1 "
            "运行固定确定性短线观察池策略（AI 短线池）。"
            "preset 分支只在请求内返回，不保存 short_pool artifact；"
            "策略完全固定、确定性筛选：条件与排序服务端锁定，"
            "只允许再传 limit(5..12，默认 8)，不接受 conditions/as_of/order_by；"
            "返回逐股结构化证据，AI 只解释证据，不得增删重排候选。"
            "返回还携带 market_state（市场抱团/拥挤度状态，严格 T-1）与 t_research"
            "（固定做T研究协议草案）：AI 不得改动其状态/阈值/协议内容，"
            "t_research.protocol_id 只是研究协议标识而非既有策略；"
            "仅当 market_state.state=dispersed 时可向用户展示"
            "「确认创建研究假设」入口，且必须由用户显式确认；"
            "任何情况下不得自动运行回测，不得给出买卖方向、价格或仓位。"
            "legacy 分支 conditions 每项必须是 {field,op,value}，field/op 应来自 "
            "list_screener_fields；返回 pool_id、日期、数量和少量预览，"
            "完整股票列表不会进入模型上下文。"
        ),
        "input_schema": _SCREEN_STOCK_POOL_SCHEMA,
        "parameters": _SCREEN_STOCK_POOL_SCHEMA,
        "read_only": True,
    },
    {
        "name": "start_pool_backtest",
        "description": (
            "仅对 screen_stock_pool 的 legacy conditions 分支保存的普通股票池启动策略或因子回测；"
            "不接受 short_momentum_quality_v1 返回的 pool_id。"
            "会创建回测计算任务与研究 artifact，不改交易事实、不下单。"
            "回测开始日不得早于股票池 as_of，最长 186 天；返回 job_id，不返回大结果。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pool_id": {"type": "string"},
                "target": {"type": "string", "enum": ["strategy", "factor"]},
                "strategy_id": {"type": "string"},
                "factor_name": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "matching": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "entry_fill": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "exit_fill": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "fees_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                "slippage_bps": {"type": "number", "minimum": 0, "maximum": 1000},
                "max_positions": {"type": "integer", "minimum": 1, "maximum": 500},
                "n_groups": {"type": "integer", "minimum": 2, "maximum": 20},
                "rebalance": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "weight": {"type": "string", "enum": ["equal", "factor_weight"]},
            },
            "required": ["pool_id", "target", "start", "end"],
            "additionalProperties": False,
        },
        "parameters": {
            "type": "object",
            "properties": {
                "pool_id": {"type": "string"},
                "target": {"type": "string", "enum": ["strategy", "factor"]},
                "strategy_id": {"type": "string"},
                "factor_name": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "matching": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "entry_fill": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "exit_fill": {"type": "string", "enum": ["close_t", "open_t+1"]},
                "fees_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                "slippage_bps": {"type": "number", "minimum": 0, "maximum": 1000},
                "max_positions": {"type": "integer", "minimum": 1, "maximum": 500},
                "n_groups": {"type": "integer", "minimum": 2, "maximum": 20},
                "rebalance": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "weight": {"type": "string", "enum": ["equal", "factor_weight"]},
            },
            "required": ["pool_id", "target", "start", "end"],
            "additionalProperties": False,
        },
        "read_only": True,
        "resource_kind": "job",
    },
    {
        "name": "get_pool_backtest",
        "description": (
            "查询 start_pool_backtest 的任务状态。"
            "可等待最多 30 秒；完成后只返回研究摘要、run_id 和 RunCard 引用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "read_only": True,
    },
    {
        "name": "get_market_overview",
        "description": "返回市场概览（主要指数/涨跌分布/板块热度），数据来自本地 DuckDB。",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "list_ext_data",
        "description": "列出已配置的扩展数据集（概念/行业/自定义）。",
        "input_schema": {"type": "object", "properties": {}},
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "optimize_portfolio",
        "description": "为一组标的计算优化权重（等权/风险平价/均值方差/最大分散等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array"},
                "method": {"type": "string"},
                "lookback_days": {"type": "integer"},
            },
        },
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "method": {
                    "type": "string",
                    "description": "equal / equal_vol / risk_parity / mean_variance / max_diversification / score_weight",
                },
                "lookback_days": {"type": "integer", "description": "回看天数，默认 120"},
            },
            "required": ["symbols"],
        },
        "read_only": True,
    },
    {
        "name": "analyze_factor",
        "description": "对单因子运行 IC/IR 分析和分层回测。默认直接使用本地 DuckDB 全市场股票池，并以 enriched 最新交易日为截止日回溯半年；仅在用户明确指定标的或日期时传 symbols/start/end，symbols 最多 50 只。无需也不得调用 quote_pool 获取全市场列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "factor_name": {"type": "string"},
                "symbols": {"type": "array"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "n_groups": {"type": "integer"},
                "rebalance": {"type": "string"},
                "weight": {"type": "string"},
            },
            "required": ["factor_name"],
        },
        "parameters": {
            "type": "object",
            "properties": {
                "factor_name": {"type": "string", "description": "因子 ID,例如 momentum_20d"},
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选。省略时直接分析本地 DuckDB 全市场;指定时最多 50 只",
                },
                "start": {
                    "type": "string",
                    "description": "可选，YYYY-MM-DD；仅在用户明确指定起始日时传入",
                },
                "end": {
                    "type": "string",
                    "description": "可选，YYYY-MM-DD；省略时使用本地 enriched 最新交易日",
                },
                "n_groups": {"type": "integer", "description": "分组数,默认 5"},
                "rebalance": {"type": "string", "description": "daily/weekly/monthly"},
                "weight": {"type": "string", "description": "equal/factor_weight"},
            },
            "required": ["factor_name"],
        },
        "read_only": True,
    },
    {
        "name": "compare_factors",
        "description": "对比多个 Alpha Zoo 因子的 IC/IR（factor_ids 必须在 Alpha Zoo 中存在）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "factor_ids": {"type": "array"},
                "symbols": {"type": "array"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
        },
        "parameters": {
            "type": "object",
            "properties": {
                "factor_ids": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["factor_ids", "symbols"],
        },
        "read_only": True,
    },
    {
        "name": "compose_factor_score",
        "description": "按 IC 权重合成多因子打分，返回标的地打分排名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "factor_ids": {"type": "array"},
                "pool": {"type": "array"},
                "as_of": {"type": "string"},
                "lookback_days": {"type": "integer"},
                "top_n": {"type": "integer"},
            },
        },
        "parameters": {
            "type": "object",
            "properties": {
                "factor_ids": {"type": "array", "items": {"type": "string"}},
                "pool": {"type": "array", "items": {"type": "string"}},
                "as_of": {"type": "string"},
                "lookback_days": {"type": "integer"},
                "top_n": {"type": "integer"},
            },
        },
        "read_only": True,
    },
]


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """把内部 TOOLS 注册表转为 OpenAI function-calling tools 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t.get("parameters")
                or t.get("input_schema")
                or {"type": "object", "properties": {}},
            },
        }
        for t in tools
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
                rows.append(
                    {
                        "id": strategy_id,
                        "name": item.get("name") or strategy_id,
                        "source": item.get("source") or "unknown",
                        "tags": item.get("tags") or [],
                    }
                )
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
    if name == "list_screener_fields":
        from app.services.screener_query import field_metadata

        fields = field_metadata()
        return {
            "status": "success",
            "summary": f"可用条件字段 {len(fields)} 个",
            "fields": fields,
            "next_actions": ["screen_stock_pool"],
            "artifacts": [],
        }
    if name == "screen_stock_pool":
        from app.services.agent_research_tools import screen_stock_pool

        return screen_stock_pool(app_state, args)
    if name == "start_pool_backtest":
        from app.services.agent_research_tools import start_pool_backtest

        return start_pool_backtest(app_state, args)
    if name == "get_pool_backtest":
        from app.services.agent_research_tools import get_pool_backtest

        return get_pool_backtest(app_state, args)
    if name == "get_market_overview":
        repo = _require(app_state, "repo")
        from app.services.market_overview_builder import build_market_overview

        return _truncate(
            build_market_overview(
                repo=repo,
                quote_service=getattr(app_state, "quote_service", None),
                depth_service=getattr(app_state, "depth_service", None),
            )
        )
    if name == "list_ext_data":
        repo = _require(app_state, "repo")
        from app.services.ext_data import ExtConfigStore

        configs = ExtConfigStore(repo.store.data_dir).load_all()
        return {
            "datasets": [
                {"id": c.id, "label": c.label, "mode": c.mode, "fields": [f.name for f in c.fields]}
                for c in configs[:200]
            ]
        }
    if name == "optimize_portfolio":
        repo = _require(app_state, "repo")
        import numpy as np

        from app.backtest.optimizers import portfolio_weights
        from app.backtest.portfolio import (
            load_price_matrix,
            momentum_from_prices,
            returns_from_prices,
        )

        symbols = _require_list(args, "symbols", 50)
        method = str(args.get("method") or "risk_parity")
        if method not in {
            "equal",
            "equal_vol",
            "risk_parity",
            "mean_variance",
            "max_diversification",
            "score_weight",
        }:
            raise ValueError(f"unknown optimize method: {method}")
        lookback_days = max(20, min(1000, int(args.get("lookback_days") or 120)))
        end = date.today()
        start = end - timedelta(days=lookback_days)

        prices, kept = load_price_matrix(repo, symbols, start, end)
        if len(kept) < 2:
            raise ValueError("有效标的不足 2 只（数据缺失、港股或标的过少）")
        if prices.shape[0] < 2:
            raise ValueError("标的间共同交易日不足，无法估计收益/协方差")

        rets = returns_from_prices(prices)
        scores = momentum_from_prices(prices) if method == "score_weight" else None
        weights_arr = np.asarray(portfolio_weights(rets, method, scores), dtype=float)
        dropped = [s for s in symbols if s not in kept]
        weights = [
            {"symbol": s, "weight": round(float(weights_arr[i]), 6)} for i, s in enumerate(kept)
        ]
        return _truncate(
            {
                "weights": weights,
                "method": method,
                "lookback_days": lookback_days,
                "meta": {"kept": kept, "dropped": dropped},
            }
        )
    if name == "analyze_factor":
        repo = _require(app_state, "repo")
        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FactorBacktestService, FactorConfig

        symbols = None if args.get("symbols") is None else _require_list(args, "symbols", 50)
        factor_name = str(args.get("factor_name") or "").strip()
        if not factor_name:
            raise ValueError("factor_name required")
        start, end = _resolve_date_range(args, 180, 186, default_end=repo.enriched_latest_date())

        svc = FactorBacktestService(BacktestEngine(repo))
        result = svc.run(
            FactorConfig(
                factor_name=factor_name,
                symbols=symbols,
                start=start,
                end=end,
                n_groups=int(args.get("n_groups") or 5),
                rebalance=args.get("rebalance") or "monthly",
                weight=args.get("weight") or "equal",
            )
        )
        return _truncate(_plain(result))
    if name == "compare_factors":
        repo = _require(app_state, "repo")
        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FactorBacktestService, FactorConfig
        from app.backtest.factor_zoo import ALPHAS

        symbols = _require_list(args, "symbols", 50)
        factor_ids = _require_list(args, "factor_ids", 20)
        unknown = [x for x in factor_ids if x not in ALPHAS]
        if unknown:
            raise ValueError(f"unknown factor: {unknown[0]}")

        start, end = _resolve_date_range(args, 180, 186, default_end=repo.enriched_latest_date())
        svc = FactorBacktestService(BacktestEngine(repo))
        out = []
        for factor_id in factor_ids:
            result = svc.run(
                FactorConfig(factor_name=factor_id, symbols=symbols, start=start, end=end)
            )
            out.append(
                {
                    "factor_id": factor_id,
                    "coverage": result.n_symbols,
                    "n_dates": result.n_dates,
                    "ic_mean": result.ic_mean,
                    "ic_ir": result.ir,
                    "error": result.error,
                }
            )
        return _truncate({"factors": out})
    if name == "compose_factor_score":
        repo = _require(app_state, "repo")
        import numpy as np

        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import (
            FACTOR_COLUMNS,
            FactorBacktestService,
            FactorConfig,
            _rank_average,
        )
        from app.backtest.factor_zoo import ALPHAS

        pool = _require_list(args, "pool", 300)
        factor_ids = _require_list(args, "factor_ids", 20)
        known_ids = {c["id"] for c in FACTOR_COLUMNS} | set(ALPHAS)
        unknown = [x for x in factor_ids if x not in known_ids]
        if unknown:
            raise ValueError(f"unknown factor: {unknown[0]}")

        as_of = date.fromisoformat(args["as_of"]) if args.get("as_of") else date.today()
        lookback_days = max(20, min(500, int(args.get("lookback_days") or 120)))
        start = as_of - timedelta(days=lookback_days)
        top_n = max(1, min(int(args.get("top_n") or 50), len(pool)))

        engine = BacktestEngine(repo)
        svc = FactorBacktestService(engine)
        candidates: list[dict] = []
        excluded: list[dict] = []
        for factor_id in factor_ids:
            ic = svc.compute_ic_only(
                FactorConfig(
                    factor_name=factor_id, symbols=pool, start=start, end=as_of, rebalance="daily"
                )
            )
            if ic["error"] is not None or ic["ic_mean"] is None or not ic["ic_std"]:
                excluded.append({"factor_id": factor_id, "reason": ic["error"] or "IC 不可用"})
                continue
            ir = ic["ic_mean"] / ic["ic_std"]
            candidates.append(
                {
                    "factor_id": factor_id,
                    "ic_mean": ic["ic_mean"],
                    "ir": ir,
                    "sign": 1 if ic["ic_mean"] >= 0 else -1,
                }
            )

        if not candidates:
            return {"error": "所有因子均无法计算，无法合成", "meta": {"excluded_factors": excluded}}

        panel_columns = [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover_rate",
        ]
        for candidate in candidates:
            if candidate["factor_id"] not in panel_columns:
                panel_columns.append(candidate["factor_id"])
        panel = engine.load_panel(pool, start, as_of, columns=panel_columns)
        if panel.is_empty():
            return {"error": "所选股票池在该日期范围内无可用行情数据"}

        available_dates = [d for d in panel["date"].unique().to_list() if d <= as_of]
        if not available_dates:
            return {"error": "所选股票池在该日期范围内无可用行情数据"}
        scored_date = max(available_dates)

        factor_day_values: dict[str, dict[str, float]] = {}
        survivors: list[dict] = []
        for candidate in candidates:
            factor_id = candidate["factor_id"]
            source = (
                panel
                if factor_id in panel.columns
                else FactorBacktestService._compute_missing_factor(panel, factor_id)
            )
            if factor_id not in source.columns:
                excluded.append({"factor_id": factor_id, "reason": "因子列不可用"})
                continue
            day_slice = (
                source.filter(pl.col("date") == scored_date)
                .select(["symbol", factor_id])
                .filter(pl.col(factor_id).is_not_null() & pl.col(factor_id).is_finite())
            )
            if day_slice.is_empty():
                excluded.append({"factor_id": factor_id, "reason": "打分日无该因子有效值"})
                continue
            factor_day_values[factor_id] = dict(
                zip(day_slice["symbol"].to_list(), day_slice[factor_id].cast(pl.Float64).to_list())
            )
            survivors.append(candidate)

        if not survivors:
            return {"error": "所有因子均无法计算，无法合成", "meta": {"excluded_factors": excluded}}

        common_symbols = set(pool)
        for candidate in survivors:
            common_symbols &= set(factor_day_values[candidate["factor_id"]].keys())
        uncovered = [s for s in pool if s not in common_symbols]
        if not common_symbols:
            return {
                "error": "所选股票池在打分日没有任何标的同时覆盖所有可用因子",
                "meta": {"excluded_factors": excluded, "uncovered_symbols": uncovered},
            }

        raw_weight_sum = sum(abs(candidate["ir"]) for candidate in survivors)
        note = None
        if raw_weight_sum == 0:
            for candidate in survivors:
                candidate["weight"] = 1.0 / len(survivors)
            note = "IC全为0，已退化为等权"
        else:
            for candidate in survivors:
                candidate["weight"] = abs(candidate["ir"]) / raw_weight_sum

        symbols_sorted = sorted(common_symbols)
        composite: dict[str, float] = {s: 0.0 for s in symbols_sorted}
        per_factor: dict[str, dict[str, dict]] = {s: {} for s in symbols_sorted}
        n = len(symbols_sorted)
        for candidate in survivors:
            factor_id = candidate["factor_id"]
            values_map = factor_day_values[factor_id]
            values = np.array([values_map[s] for s in symbols_sorted]) * candidate["sign"]
            normalized = np.array([0.5]) if n == 1 else (_rank_average(values) - 1) / (n - 1)
            for sym, norm_v in zip(symbols_sorted, normalized):
                composite[sym] += candidate["weight"] * float(norm_v)
                per_factor[sym][factor_id] = {
                    "weight": round(candidate["weight"], 4),
                    "ic_mean": candidate["ic_mean"],
                    "rank_normalized_value": round(float(norm_v), 4),
                }

        ranked = sorted(
            (
                {"symbol": sym, "composite_score": round(score, 6), "per_factor": per_factor[sym]}
                for sym, score in composite.items()
            ),
            key=lambda row: row["composite_score"],
            reverse=True,
        )[:top_n]
        meta = {
            "used_factors": [candidate["factor_id"] for candidate in survivors],
            "excluded_factors": excluded,
            "pool_size": len(pool),
            "as_of": str(as_of),
            "scored_date": str(scored_date),
            "uncovered_symbols": uncovered,
        }
        if note:
            meta["note"] = note
        return _truncate({"ranked": ranked, "meta": meta})
    raise ValueError(f"unknown agent tool: {name}")


def sanitize_tool_error(exc: BaseException) -> str:
    """Redact secrets and filesystem paths before an error enters model/UI context."""
    message = redact_text(str(exc) or type(exc).__name__)
    return _PATH_IN_ERROR_RE.sub("<path>", message)


def _require(app_state: Any, attr: str):
    value = getattr(app_state, attr, None)
    if value is None:
        raise ValueError(f"tool requires app_state.{attr}")
    return value


def _require_list(args: dict, key: str, max_len: int) -> list:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list")
    if len(value) > max_len:
        raise ValueError(f"{key} supports at most {max_len} items")
    return value


def _resolve_date_range(
    args: dict,
    default_days: int,
    max_days: int,
    *,
    default_end: date | None = None,
) -> tuple[date, date]:
    end = date.fromisoformat(args["end"]) if args.get("end") else (default_end or date.today())
    start = (
        date.fromisoformat(args["start"])
        if args.get("start")
        else end - timedelta(days=default_days)
    )
    if (end - start).days > max_days:
        raise ValueError(f"date range too wide (max {max_days} days)")
    return start, end


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
