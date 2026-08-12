"""Run the smallest auditable TickFlow backtest workflow.

The command deliberately exposes one frozen strategy and one deterministic universe
selection rule.  It validates canonical enriched data, reuses the production Matrix
backtest engine, and writes stable JSON plus a small offline HTML report.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from research.common.universe import stable_symbol_sample, symbols_sha256, universe_manifest
from research.paths import CURRENT_ARTIFACTS_DIR, DATA_DIR, ensure_artifact_dirs

PROTOCOL_VERSION = 1
DEFAULT_STRATEGY = "trend_breakout"
DEFAULT_START = date(2024, 9, 24)
DEFAULT_SEED = 20260723
DEFAULT_UNIVERSE_SIZE: int | None = None
DEFAULT_JSON = CURRENT_ARTIFACTS_DIR / "mvp_backtest.json"
DEFAULT_HTML = CURRENT_ARTIFACTS_DIR / "mvp_backtest.html"

MVP_STRATEGIES = {
    DEFAULT_STRATEGY: {
        "name": "趋势突破",
        "evidence_status": "historical_replay_failed",
        "warning": "仅用于验证回测闭环，不代表策略已经通过样本外晋级。",
    },
}

BACKTEST_KWARGS: dict[str, Any] = {
    "matching": "open_t+1",
    "fees_pct": 0.0002,
    "slippage_bps": 5.0,
    "max_positions": 10,
    "max_exposure_pct": 1.0,
    "initial_capital": 1_000_000.0,
    "position_sizing": "equal",
    "mode": "position",
    "holding_days": 5,
    "asset_type": "stock",
    "minute_fill": False,
}

STABLE_METRICS = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "profit_factor",
    "n_trades",
    "n_candidates",
    "n_days",
    "benchmark_return",
    "excess",
)

BacktestRunner = Callable[[StrategyBacktestConfig, Path], Mapping[str, Any]]


def _enriched_files(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "kline_daily_enriched").glob("**/*.parquet"))


def collect_data_status(data_dir: Path) -> dict[str, Any]:
    """Return an explicit quality summary for canonical enriched daily data."""
    files = _enriched_files(data_dir)
    if not files:
        raise FileNotFoundError(
            f"没有找到 {data_dir / 'kline_daily_enriched'} 下的 parquet 日线"
        )

    source = str(data_dir / "kline_daily_enriched" / "**" / "*.parquet")
    frame = pl.scan_parquet(source, hive_partitioning=True)
    columns = set(frame.collect_schema().names())
    required = {"date", "symbol", "open", "high", "low", "close", "volume"}
    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"enriched 日线缺少必需字段: {', '.join(missing)}")

    summary = frame.select(
        pl.len().alias("rows"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("date").n_unique().alias("trading_days"),
        pl.col("date").min().alias("min_date"),
        pl.col("date").max().alias("max_date"),
        sum(pl.col(column).null_count() for column in required).alias("required_nulls"),
        sum(
            (~pl.col(column).cast(pl.Float64, strict=False).is_finite()).sum()
            for column in ("open", "high", "low", "close", "volume")
        ).alias("non_finite_ohlcv"),
        sum(
            (pl.col(column).cast(pl.Float64, strict=False) <= 0).sum()
            for column in ("open", "high", "low", "close")
        ).alias("non_positive_prices"),
    ).collect().row(0, named=True)

    duplicates = (
        frame.group_by("date", "symbol")
        .len()
        .filter(pl.col("len") > 1)
        .select((pl.col("len") - 1).sum().fill_null(0).alias("duplicates"))
        .collect()
        .item()
    )
    status = {
        "source": "data/kline_daily_enriched/**/*.parquet",
        "file_count": len(files),
        "row_count": int(summary["rows"]),
        "symbol_count": int(summary["symbols"]),
        "trading_day_count": int(summary["trading_days"]),
        "min_date": str(summary["min_date"]),
        "max_date": str(summary["max_date"]),
        "duplicate_date_symbol_rows": int(duplicates or 0),
        "required_null_values": int(summary["required_nulls"] or 0),
        "non_finite_ohlcv_values": int(summary["non_finite_ohlcv"] or 0),
        "non_positive_prices": int(summary["non_positive_prices"] or 0),
    }
    status["valid"] = all(
        status[key] == 0
        for key in (
            "duplicate_date_symbol_rows",
            "required_null_values",
            "non_finite_ohlcv_values",
            "non_positive_prices",
        )
    )
    return status


def resolve_end(value: str, status: Mapping[str, Any]) -> date:
    if value.lower() == "latest":
        return date.fromisoformat(str(status["max_date"]))
    return date.fromisoformat(value)


def select_universe(
    data_dir: Path,
    *,
    start: date,
    end: date,
    size: int | None,
    seed: int | None,
) -> list[str]:
    if size is not None and size <= 0:
        raise ValueError("universe-size 必须大于 0")
    source = str(data_dir / "kline_daily_enriched" / "**" / "*.parquet")
    frame = pl.scan_parquet(source, hive_partitioning=True)
    if size is None:
        latest = (
            frame.filter(pl.col("date") <= end)
            .select(pl.col("date").max())
            .collect()
            .item()
        )
        if latest is None or latest < start:
            raise ValueError(f"{start} 至 {end} 没有可回测交易日")
        symbols = (
            frame.filter(pl.col("date") == latest)
            .select("symbol")
            .unique()
            .collect()
        )
        basic_path = data_dir / "tushare_stock_basic" / "all.parquet"
        if not basic_path.is_file():
            raise FileNotFoundError(f"全量非 ST 股票池需要 Tushare stock_basic: {basic_path}")
        basic = pl.read_parquet(basic_path).select(
            pl.col("ts_code").alias("symbol"),
            pl.col("name").fill_null(""),
            pl.col("list_status").fill_null(""),
        )
        selected = (
            symbols.join(basic, on="symbol", how="left")
            .filter(
                (pl.col("list_status") == "L")
                & ~pl.col("name").str.contains(r"(?i)ST|\*ST|退")
            )
            .select("symbol")
            .sort("symbol")
            .get_column("symbol")
            .to_list()
        )
    else:
        if seed is None:
            raise ValueError("sampled universe requires seed")
        symbols = (
            frame.filter(pl.col("date").is_between(start, end, closed="both"))
            .select("symbol")
            .unique()
            .collect()["symbol"]
            .to_list()
        )
        selected = stable_symbol_sample(symbols, size, seed)
    if not selected:
        raise ValueError(f"{start} 至 {end} 没有可回测股票")
    return selected


def build_config(strategy: str, symbols: list[str], start: date, end: date) -> StrategyBacktestConfig:
    if strategy not in MVP_STRATEGIES:
        raise ValueError(f"MVP 只开放策略: {', '.join(MVP_STRATEGIES)}")
    if start > end:
        raise ValueError("start 不能晚于 end")
    return StrategyBacktestConfig(
        strategy_id=strategy,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=None,
        **BACKTEST_KWARGS,
    )


def default_backtest_runner(config: StrategyBacktestConfig, data_dir: Path) -> Mapping[str, Any]:
    from app.backtest.worker import make_worker_task, run_worker_task

    os.environ.setdefault("TICKFLOW_BACKTEST_MODE", "inprocess")
    return run_worker_task(make_worker_task("backtest", data_dir, config))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, Path)):
        return str(value)
    return value


def stable_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Remove process/cache/timing fields that would make identical runs differ."""
    stats = raw.get("stats") if isinstance(raw.get("stats"), Mapping) else {}
    metrics = {key: _json_safe(stats.get(key)) for key in STABLE_METRICS if key in stats}
    benchmark_curve = raw.get("benchmark_curve")
    if "benchmark_return" not in metrics and isinstance(benchmark_curve, list):
        closes = [
            float(point["close"])
            for point in benchmark_curve
            if isinstance(point, Mapping) and point.get("close") is not None
        ]
        if len(closes) >= 2 and closes[0] > 0:
            metrics["benchmark_return"] = round(closes[-1] / closes[0] - 1.0, 4)
            if metrics.get("total_return") is not None:
                metrics["excess"] = round(
                    float(metrics["total_return"]) - metrics["benchmark_return"],
                    4,
                )
    trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []
    error = str(raw["error"]) if raw.get("error") else None
    if error and "未产生买入信号" in error:
        status = "no_signal"
    elif error:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "metrics": metrics,
        "trade_count": int(metrics.get("n_trades") or len(trades)),
        "error": error,
    }


def build_payload(
    *,
    config: StrategyBacktestConfig,
    data_status: Mapping[str, Any],
    seed: int | None,
    requested_universe_size: int | None,
    raw_result: Mapping[str, Any] | None,
    validate_only: bool,
) -> dict[str, Any]:
    if requested_universe_size is None:
        selected = config.symbols or []
        manifest = {
            "selection": (
                "end-date enriched constituents + Tushare listed stocks - names matching "
                "ST/*ST/退; lexicographic symbol sort; no sampling"
            ),
            "seed": None,
            "requested_size": "all_non_st",
            "actual_size": len(selected),
            "start": str(config.start),
            "end": str(config.end),
            "source": (
                "data/kline_daily_enriched/**/*.parquet + "
                "data/tushare_stock_basic/all.parquet"
            ),
            "symbols_sha256": symbols_sha256(selected),
            "symbols": selected,
        }
    else:
        if seed is None:
            raise ValueError("sampled universe requires seed")
        manifest = universe_manifest(
            config.symbols or [],
            seed=seed,
            requested_size=requested_universe_size,
            start=config.start,
            end=config.end,
        )
    config_payload = asdict(config)
    config_payload.pop("symbols", None)
    config_payload = _json_safe(config_payload)
    protocol = {
        "version": PROTOCOL_VERSION,
        "strategy": config.strategy_id,
        "config": config_payload,
        "universe_sha256": manifest["symbols_sha256"],
        "seed": seed,
        "data_min_date": data_status["min_date"],
        "data_max_date": data_status["max_date"],
    }
    protocol_hash = hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    result = None if validate_only else stable_result(raw_result or {})
    failures = []
    if result and result["error"]:
        failures.append({"stage": "backtest", "reason": result["error"]})
    return {
        "schema": "tickflow.mvp_backtest.v1",
        "protocol_hash": protocol_hash,
        "evidence_status": "historical_backtest_only_not_live_validated",
        "warning": "历史回测不代表未来收益，当前策略未通过新鲜样本外晋级。",
        "strategy": {"id": config.strategy_id, **MVP_STRATEGIES[config.strategy_id]},
        "config": config_payload,
        "seed": seed,
        "data_status": dict(data_status),
        "universe": manifest,
        "result": result,
        "failures": failures,
    }


def render_html(payload: Mapping[str, Any]) -> str:
    result = payload.get("result") or {}
    metrics = result.get("metrics") or {}
    rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in metrics.items()
    ) or "<tr><td colspan='2'>仅完成数据校验，未运行回测</td></tr>"
    strategy = payload["strategy"]
    data_status = payload["data_status"]
    universe = payload["universe"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>TickFlow MVP 回测</title><style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#17202a}}
h1{{margin-bottom:8px}} .warning{{background:#fff3cd;padding:12px;border-radius:6px}}
table{{border-collapse:collapse;width:100%;margin:18px 0}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{width:38%;background:#f6f8fa}}code{{word-break:break-all}}
</style></head><body><h1>TickFlow 最小回测报告</h1>
<p class="warning">{html.escape(str(payload['warning']))}</p>
<p>策略：{html.escape(str(strategy['name']))}（<code>{html.escape(str(strategy['id']))}</code>）</p>
<p>区间：{html.escape(str(payload['config']['start']))} 至 {html.escape(str(payload['config']['end']))}</p>
<p>数据：{data_status['row_count']} 行，{data_status['symbol_count']} 只股票，最新 {data_status['max_date']}</p>
<p>股票池：{universe['actual_size']} 只，seed={payload['seed']}，SHA-256=<code>{universe['symbols_sha256']}</code></p>
<h2>结果</h2><table>{rows}</table>
<p>状态：<strong>{html.escape(str(result.get('status', 'validated_only')))}</strong></p>
<p>协议哈希：<code>{html.escape(str(payload['protocol_hash']))}</code></p>
</body></html>"""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def execute(args: argparse.Namespace, runner: BacktestRunner | None = None) -> dict[str, Any]:
    data_dir = Path(args.data_dir).resolve()
    status = collect_data_status(data_dir)
    if not status["valid"]:
        raise ValueError(f"数据质量检查失败: {json.dumps(status, ensure_ascii=False)}")
    start = date.fromisoformat(args.start)
    end = resolve_end(args.end, status)
    symbols = select_universe(
        data_dir,
        start=start,
        end=end,
        size=args.universe_size,
        seed=None if args.universe_size is None else args.seed,
    )
    config = build_config(args.strategy, symbols, start, end)
    raw_result = None if args.validate_only else (runner or default_backtest_runner)(config, data_dir)
    payload = build_payload(
        config=config,
        data_status=status,
        seed=None if args.universe_size is None else args.seed,
        requested_universe_size=args.universe_size,
        raw_result=raw_result,
        validate_only=args.validate_only,
    )
    ensure_artifact_dirs()
    json_path = Path(args.output_json).resolve()
    html_path = Path(args.output_html).resolve()
    _atomic_write(
        json_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(html_path, render_html(payload))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TickFlow 无前端最小回测闭环")
    parser.add_argument("--strategy", choices=tuple(MVP_STRATEGIES), default=DEFAULT_STRATEGY)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default="latest", help="YYYY-MM-DD 或 latest")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--universe-size",
        type=int,
        default=DEFAULT_UNIVERSE_SIZE,
        help="可选的研究抽样数量；默认使用结束日全部上市非 ST 股票",
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-html", default=str(DEFAULT_HTML))
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = execute(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"MVP 运行失败: {exc}")
        return 2
    result = payload.get("result") or {}
    print(f"数据检查: {'通过' if payload['data_status']['valid'] else '失败'}")
    print(f"股票池: {payload['universe']['actual_size']} 只")
    print(f"回测状态: {result.get('status', 'validated_only')}")
    print(f"JSON: {Path(args.output_json).resolve()}")
    print(f"HTML: {Path(args.output_html).resolve()}")
    return 0 if result.get("status") not in {"failed", "no_signal"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
