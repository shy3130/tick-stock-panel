"""冻结执行输入；拒绝回测会静默纠正或当前模拟仓无法表达的配置。"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import CodeType

from app import definitions
from app.backtest.strategy import StrategyBacktestConfig
from app.services.resource_bundle import collect_resources
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine
from app.strategy.paper import MAX_POSITION_SYMBOLS
from app.strategy.scoring import effective_scoring, effective_scoring_directions

DIFFERENCES = [
    "日线买卖点在收盘数据确认后评估，委托交给原模拟仓按次日开盘撮合；不回填历史信号。",
    "模拟仓佣金最低 5 元，回测无此下限；同费率不保证同成本、同收益。",
    "仓位按委托时权益与参考价确定，次日跳空、可卖资金、涨跌停和顺延会造成成交及仓位差异。",
    "保护性风控按新鲜实时快照检查，无法重放两次快照之间的价格路径；次日开盘单沿用原模拟仓盘后记账。",
    "回测期末强制平仓不用于持续运行的模拟仓。历史报告未保存代码版本时，只能核对当前策略定义。",
]
RISK_FIELDS = (
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "trailing_take_profit_activate",
    "trailing_take_profit_drawdown",
    "max_hold_days",
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str).encode()
    ).hexdigest()


def number(value, name: str, low: float, high: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} 必须在 {low} 至 {high} 之间")
    return float(value)


def _params(strategy, raw: dict) -> dict:
    definitions = {p["id"]: p for p in strategy.meta.get("params", [])}
    unknown = set(raw) - definitions.keys()
    if unknown:
        raise ValueError("未知策略参数: " + ", ".join(sorted(unknown)))
    result = StrategyEngine.resolve_params(strategy, raw)
    for key, value in result.items():
        spec = definitions[key]
        kind = spec.get("type")
        if kind in {"float", "int"}:
            num = number(value, key, float(spec.get("min", -1e100)), float(spec.get("max", 1e100)))
            if kind == "int" and num != int(num):
                raise ValueError(f"{key} 必须是整数")
            result[key] = int(num) if kind == "int" else num
        elif kind == "bool" and not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        elif kind == "select" and value not in spec.get("options", []):
            raise ValueError(f"{key} 不在可选值中")
        elif kind in {"string", "date"} and not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
    return result


def _code_signature(value):
    # 不使用 marshal 的对象引用标记：同一代码在重启/不同 hash seed 下也必须同指纹。
    if isinstance(value, CodeType):
        return {
            "code": value.co_code.hex(),
            "names": value.co_names,
            "vars": value.co_varnames,
            "free": value.co_freevars,
            "cells": value.co_cellvars,
            "consts": [_code_signature(c) for c in value.co_consts],
        }
    if isinstance(value, (tuple, frozenset)):
        values = [_code_signature(c) for c in value]
        return sorted(values, key=digest) if isinstance(value, frozenset) else values
    if isinstance(value, (str, int, float, bool, type(None))):
        return str(value) if isinstance(value, float) and not math.isfinite(value) else value
    return repr(value)


def definition_revision(strategy) -> str:
    # 同时检查内存定义与磁盘代码；热重载/删除都使旧快照停用。
    return digest(
        {
            "meta": strategy.meta,
            "basic_filter": strategy.basic_filter,
            "signals": [strategy.entry_signals, strategy.exit_signals],
            "risk": [getattr(strategy, k, None) for k in RISK_FIELDS],
            "backend": strategy.execution_backend,
            "functions": [
                digest(_code_signature(fn.__code__))
                for fn in (
                    strategy.filter_fn,
                    strategy.filter_history_fn,
                    getattr(strategy.matrix_strategy, "compute_signals", None),
                )
                if fn is not None and hasattr(fn, "__code__")
            ],
        }
    )


def make_snapshot(data_dir: Path, engine, source: dict, account: dict | None = None) -> dict:
    raw = source.get("config")
    current = raw is None
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("回测配置必须是对象")
    raw = deepcopy(raw or {})
    if current and account:
        raw.update(
            initial_capital=account["initial_cash"],
            **{k: account[k] for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")},
        )
    sid = raw.get("strategy_id") or source.get("strategy_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("请选择策略或有效的策略回测配置")
    s = engine.get(sid)
    if s.meta.get("research_only") or s.execution_backend in {"composite", "minute_filter"}:
        raise ValueError("当前跟单支持正式日线独立策略，暂不支持叠加、研究模板或分钟策略")
    if "stock" not in s.meta.get("asset_types", ["stock"]) or "1d" not in s.meta.get(
        "timeframes", ["1d"]
    ):
        raise ValueError("当前跟单仅支持 A 股日线策略")
    valid = {f.name for f in fields(StrategyBacktestConfig)} | {
        "score_min",
        "score_max",
        "timing_mode",
        "timeframe",
    }
    if set(raw) - valid:
        raise ValueError("不支持的回测配置字段: " + ", ".join(sorted(set(raw) - valid)))
    if raw.get("asset_type", "stock") != "stock" or raw.get("timeframe", "1d") != "1d":
        raise ValueError("当前跟单仅支持 A 股日线")
    if (
        raw.get("mode", "position") != "position"
        or raw.get("minute_fill")
        or raw.get("regime_filter")
    ):
        raise ValueError("暂不支持独立全样本模式、分钟成交或市场环境过滤")
    if raw.get("position_sizing", "equal") not in {"equal", "score_weight"} or raw.get(
        "position_policy"
    ):
        raise ValueError("暂不支持自定义仓位策略")
    matching = raw.get("matching", "open_t+1")
    if any((raw.get(key) or matching) != "open_t+1" for key in ("entry_fill", "exit_fill")):
        raise ValueError("日线确认后无法按已知的当日收盘价成交，请使用买入/卖出次日开盘")
    overrides = deepcopy(
        strategy_config.load_override(data_dir, sid) if current else raw.get("overrides") or {}
    )
    if not isinstance(overrides, dict):
        raise ValueError("策略覆盖参数必须是对象")
    # 回测 run 以 config.params + META 为准；不会读取全局 override.params。
    params = _params(
        s,
        StrategyEngine.resolve_params(s, overrides=overrides)
        if current
        else raw.get("params") or {},
    )
    info = source.get("strategy_info") or {}
    if info.get("id") and info["id"] != sid:
        raise ValueError("报告的策略信息与配置 ID 不一致")
    for key in ("entry_signals", "exit_signals", *RISK_FIELDS):
        value = info[key] if key in info else overrides.get(key, getattr(s, key, None))
        if key.endswith("signals"):
            value = value if isinstance(value, list) else list(getattr(s, key))
            if any(not isinstance(v, str) or not v for v in value):
                raise ValueError("买卖信号必须是名称列表")
        elif value is not None:
            lo, hi = {
                "take_profit": (0.01, 5),
                "trailing_stop": (0.005, 0.5),
                "trailing_take_profit_activate": (0.01, 2),
                "trailing_take_profit_drawdown": (0.005, 0.5),
                "max_hold_days": (1, 10000),
            }.get(key, (-1, 1))
            if key not in {"stop_loss", "max_hold_days"}:
                number(value, key, -hi, hi)
                value = number(abs(value), key, lo, hi)
            else:
                number(value, key, lo, hi)
            if key == "max_hold_days" and int(value) != value:
                raise ValueError("最大持有天数必须是整数")
        overrides[key] = value
    activate = overrides["trailing_take_profit_activate"]
    drawdown = overrides["trailing_take_profit_drawdown"]
    if (activate is None) != (drawdown is None) or (activate and drawdown > activate):
        raise ValueError("移动止盈需同时配置激活与回撤，回撤不能大于激活涨幅")
    overrides["basic_filter"] = {**s.basic_filter, **(overrides.get("basic_filter") or {})}
    overrides["scoring"] = effective_scoring(s.meta.get("scoring"), overrides)
    overrides["scoring_directions"] = effective_scoring_directions(overrides)
    overrides["params"] = params
    overrides["display_limit"] = 0
    for key in ("score_min", "score_max"):
        value = info.get(key, overrides.get(key, raw.get(key)))
        overrides[key] = number(value, key, 0, 100) if value is not None else None
    if (
        overrides["score_min"] is not None
        and overrides["score_max"] is not None
        and overrides["score_min"] > overrides["score_max"]
    ):
        raise ValueError("评分下限不能大于上限")
    max_positions = raw.get("max_positions", 10)
    number(max_positions, "最大持仓数", 1, MAX_POSITION_SYMBOLS)
    if int(max_positions) != max_positions:
        raise ValueError("最大持仓数必须是整数")
    symbols = raw.get("symbols")
    if symbols is not None and (
        not isinstance(symbols, list)
        or not symbols
        or any(not isinstance(v, str) or not v for v in symbols)
    ):
        raise ValueError("股票池需为空值（全市场）或非空代码列表")
    fees = {
        "commission_pct": raw.get("commission_pct"),
        "stamp_tax_pct": raw.get("stamp_tax_pct"),
        "slippage_bps": raw.get("slippage_bps", 5.0),
    }
    fees["commission_pct"] = (
        fees["commission_pct"]
        if fees["commission_pct"] is not None
        else raw.get("fees_pct", 0.0002)
    )
    fees["stamp_tax_pct"] = fees["stamp_tax_pct"] if fees["stamp_tax_pct"] is not None else 0.0
    for key, value in fees.items():
        number(
            value, key, 0, {"slippage_bps": 200, "commission_pct": 0.01, "stamp_tax_pct": 0.05}[key]
        )
    # 复用资源依赖闭包，不另写策略/因子/信号解析器。额外加入冻结覆盖配置引用。
    roots = [{"kind": "strategy", "id": sid}]
    catalog = definitions.catalog(data_dir)
    refs = set(overrides["entry_signals"] + overrides["exit_signals"] + list(overrides["scoring"]))
    for condition in overrides.get("overlay_filter") or []:
        refs.add(str(condition.get("left", "")))
        refs.add(str(condition.get("right", "")).removeprefix("field:"))
    for item in catalog:
        if item["id"] in refs or f"csg_{item['id']}" in refs or f"signal_{item['id']}" in refs:
            if (
                item.get("enabled") is False
                or item.get("timeframe", "daily") != "daily"
                or item.get("error")
            ):
                raise ValueError(f"引用的因子或信号不可用于日线跟单: {item['label']}")
            roots.append({"kind": item["kind"], "id": item["id"]})
    entries, summary = collect_resources(data_dir, engine, roots)
    if summary.warnings:
        raise ValueError("策略依赖尚不可用: " + "; ".join(summary.warnings))
    dependencies = {
        name: hashlib.sha256((data_dir / name).read_bytes()).hexdigest()
        for name, _payload in entries
        if not name.startswith("user_data/strategy_overrides/")
    }
    snapshot = {
        "strategy_id": sid,
        "name": str(s.meta.get("name") or sid),
        "params": params,
        "overrides": overrides,
        "symbols": symbols,
        "asset_type": "stock",
        "timeframe": "1d",
        "entry_fill": "open_t+1",
        "exit_fill": "open_t+1",
        "fees": fees,
        "initial_capital": number(raw.get("initial_capital", 1_000_000), "初始资金", 1, 1e12),
        "max_positions": int(max_positions),
        "max_exposure_pct": number(raw.get("max_exposure_pct", 1.0), "最大敞口", 0, 1),
        "position_sizing": raw.get("position_sizing", "equal"),
        "dependencies": dependencies,
        "definition_revision": definition_revision(s),
        "source": source.get("label") or ("当前策略配置" if current else "回测配置"),
    }
    return {"snapshot": snapshot, "revision": digest(snapshot), "differences": DIFFERENCES}


def check_revision(data_dir: Path, engine, snapshot: dict) -> None:
    s = engine.get(snapshot["strategy_id"])
    if definition_revision(s) != snapshot["definition_revision"]:
        raise ValueError("策略定义已变更，请重新预览并绑定")
    for name, expected in snapshot["dependencies"].items():
        path = (data_dir / name).resolve()
        try:
            unchanged = (
                path.is_relative_to(data_dir.resolve())
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected
            )
        except OSError as exc:
            raise ValueError("策略或因子、信号依赖无法读取，请检查后重新绑定") from exc
        if not unchanged:
            raise ValueError("策略或因子、信号依赖已变更，请重新预览并绑定")
