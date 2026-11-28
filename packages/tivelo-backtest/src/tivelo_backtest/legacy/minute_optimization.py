"""Optimize a sealed minute strategy over the bars in a locked snapshot."""

import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..research.intraday import build_feature_frame, cn_session_expr
from ..research.signals import build_intraday_expressions, inject_intraday_columns
from .minute_replay import CN_TIMEZONE
from .minute_strategy import run_legacy_minute
from .optimizer import default_direction, expand_param_grid
from .robust_optimizer import run_robust_search, split_validation_windows
from .simulator import SimulationOptions
from .snapshot_minutes import read_legacy_minute_frame

_FRAME_CACHE_BYTES = 64 * 1024 * 1024


def _minute_loader(reader, query, symbols, minute_label, *, signal_fields=None, signal_exprs=None):
    cache = OrderedDict()
    total_bytes = 0

    def load(day):
        nonlocal total_bytes
        if day in cache:
            cache.move_to_end(day)
            return cache[day][0].clone()
        midnight = datetime.combine(day, datetime.min.time(), CN_TIMEZONE).astimezone(UTC)
        start = max(query.start, midnight)
        end = min(query.end, midnight + timedelta(days=1))
        frame = (
            read_legacy_minute_frame(
                reader, query.model_copy(update={"start": start, "end": end}),
                symbols=symbols, minute_label=minute_label, signal_fields=signal_fields,
            ) if start < end else pl.DataFrame()
        )
        if signal_exprs and not frame.is_empty():
            features = build_feature_frame(frame, timezone=CN_TIMEZONE, session_expr=cn_session_expr())
            frame = inject_intraday_columns(frame, features, signal_exprs)
        size = frame.estimated_size()
        if size <= _FRAME_CACHE_BYTES:
            while total_bytes + size > _FRAME_CACHE_BYTES:
                _, (_, removed) = cache.popitem(last=False)
                total_bytes -= removed
            cache[day] = frame, size
            total_bytes += size
            return frame.clone()
        return frame

    return load


def _trade_record(trade, triggers):
    row = asdict(trade)
    day = date.fromisoformat(str(trade.entry_date)[:10])
    trigger = triggers.get((trade.symbol, day))
    row["entry_date"] = f"{day} {trigger}" if trigger else day.isoformat()
    for field in ("exit_date", "entry_signal_date", "exit_signal_date"):
        if row[field] is not None:
            row[field] = str(row[field])
    return row


def _backtest_report(result):
    simulation = result.simulation
    return {
        "stats": {key: value for key, value in simulation.stats.items() if key != "statistics_ms"},
        "trades": [_trade_record(trade, result.trigger_times) for trade in simulation.trades],
        "equity_curve": simulation.equity_curve,
        "drawdown_curve": simulation.drawdown_curve,
        "per_symbol_stats": simulation.per_symbol_stats,
        "minute_replay": {
            "replayed_days": result.replay.replayed_days,
            "skipped_days": [day.isoformat() for day in result.replay.skipped_days],
            "strategy_matches": result.replay.strategy_matches,
            "buy_limit_up": result.replay.buy_limit_up,
            "dropped_axis_hits": result.dropped_axis_hits,
        },
    }


def optimize_legacy_minute(panel, strategy, matcher, *, request, reader, limit_for, grid_order, scoring_order):
    started = time.perf_counter()
    settings = request.minute
    signal_exprs = build_intraday_expressions(list(settings.signal_definitions))
    missing = strategy.required_intraday_signals - (settings.signal_fields.keys() | signal_exprs.keys())
    if missing:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
            "Minute snapshot lacks intraday signal inputs: " + ", ".join(sorted(missing)),
        )
    load_day = _minute_loader(
        reader, settings.query, request.symbols, settings.minute_label,
        signal_fields=settings.signal_fields, signal_exprs=signal_exprs,
    )
    days = panel.filter(pl.col("date").is_between(request.start, request.end))["date"].unique().sort().to_list()
    has_minutes = False
    for day in days:
        if not load_day(day).is_empty():
            has_minutes = True
            if not settings.signal_fields:
                break
    if not has_minutes:
        raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Snapshot has no matching minute bars")
    daily_signals = {"signal_limit_up", "signal_limit_down", "signal_broken_limit_up"} & set(panel.columns)
    if daily_signals:
        panel = panel.with_columns(pl.col(sorted(daily_signals)).cast(pl.Boolean))
    overrides = {}
    if (selection := request.selection) is not None:
        overrides = dict(
            basic_filter=selection.basic_filter.model_dump(mode="json"),
            scoring={name: selection.scoring[name] for name in scoring_order},
            scoring_replace=True,
            scoring_directions=selection.scoring_directions,
            order_by=selection.order_by,
            descending=selection.descending,
        )
    monte_carlo = request.objective.startswith("mc_maxdd_")
    trial_options = SimulationOptions(
        include_monte_carlo=monte_carlo, include_curves=False, include_trades=False,
        include_per_symbol_stats=False, include_return_distribution=False,
    )
    last_observed = panel["date"].max()

    def run(combo, first, last, options):
        sim_end = last
        if settings.mode == "full" and last_observed > last:
            horizon = matcher.max_hold_days or settings.holding_days
            tail_days = min(max(0, (horizon + 5) * 2), (last_observed - last).days)
            sim_end = last + timedelta(days=tail_days)
        return run_legacy_minute(
            panel, strategy, matcher, start=first, end=last, minute_days=days,
            load_day=load_day, limit_for=limit_for, params={**request.base_params, **combo},
            overrides=overrides, mode=settings.mode, simulation_end=sim_end, options=options,
        )

    def evaluate(combo, first, last):
        if not any(first <= day <= last for day in days):
            return {}, "正式回测区间内无数据"
        result = run(combo, first, last, trial_options)
        if not result.replay.hits:
            detail = (
                f" (区间内 {len(result.replay.skipped_days)} 个交易日缺分钟K分区被跳过)"
                if result.replay.skipped_days else ""
            )
            return {}, "在指定区间内未产生买入信号" + detail
        stats = result.simulation.stats
        metrics = {name: stats.get(name) for name in (request.objective, "n_trades")}
        return metrics, stats.get("error")

    combos = expand_param_grid(list(request.params_meta), {key: request.param_grid[key] for key in grid_order})
    report = run_robust_search(
        combos,
        split_validation_windows(request.start, request.end, request.k_segments),
        evaluate,
        objective=request.objective,
        direction=request.direction or default_direction(request.objective),
        min_trades_total=request.min_trades_total,
        stage2_top_n=request.stage2_top_n,
        started_at=started,
    )
    report["best_backtest"] = None
    if (best := report["best_params"]) is not None:
        detailed = run(best, request.start, request.end, SimulationOptions(include_monte_carlo=monte_carlo))
        report["best_backtest"] = _backtest_report(detailed)
    return report
