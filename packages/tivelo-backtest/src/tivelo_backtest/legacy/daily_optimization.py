"""Run the legacy robust optimizer over actual daily bars."""

import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import date
from functools import lru_cache, partial
from threading import Event
from typing import Literal, Protocol

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..research.matrix import (
    MarketDataMatrix,
    MarketMatrix,
    SignalMatrix,
    build_market_matrix_from_encoding,
    build_market_matrix_from_signals,
    encode_panel_matrix,
    slice_market_data_matrix,
    slice_signal_matrix,
)
from ..research.matrix_selection import MatrixPipelineConfig, select_matrix_signals
from ..research.scoring import scoring_warmup_bars
from .composite_strategy import LegacyCompositeStrategy
from .optimizer import default_direction, expand_param_grid, validate_primary_objective
from .panel_strategy import LegacyPolarsStrategy
from .robust_optimizer import run_robust_search, split_validation_windows
from .simulator import LegacySimulator, MatcherConfig, SimulationOptions


class DailyMatrixStrategy(Protocol):
    def required_warmup_bars(self, params: dict) -> int: ...

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix: ...


def optimize_daily_matrix(
    market: MarketDataMatrix,
    strategy: DailyMatrixStrategy,
    matcher: MatcherConfig,
    *,
    start: date,
    end: date,
    params_meta: list[dict],
    param_grid: dict,
    base_params: dict | None = None,
    selection: MatrixPipelineConfig | None = None,
    objective: str = "sortino",
    direction: Literal["max", "min"] | None = None,
    k_segments: int = 4,
    min_trades_total: int = 30,
    stage2_top_n: int = 20,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: Event | None = None,
) -> dict:
    """Reuse a prepared panel and the TSP ranking and result format without a host."""
    dates = tuple(date.fromisoformat(label[:10]) for label in market.timestamp_labels)
    compute_signals = strategy.compute_signals
    if isinstance(strategy, LegacyCompositeStrategy):
        strategy.validate_selection(selection)
        compute_signals = partial(
            strategy.compute_signals, selection=selection, max_hold=max(matcher.max_hold_days or 250, 1)
        )
        selection = None

    @lru_cache(maxsize=16)
    def view(first: int, stop: int) -> MarketDataMatrix:
        return slice_market_data_matrix(market, first, stop)

    def match(history, first, stop, params):
        warmed_market = view(history, stop)
        signals = compute_signals(warmed_market, params)
        if selection is not None:
            signals = select_matrix_signals(warmed_market, signals, selection)
        sliced_signals = slice_signal_matrix(signals, first - history, stop - history)
        if not sliced_signals.entry.any():
            return None
        return build_market_matrix_from_signals(
            view(first, stop),
            sliced_signals,
            entry_delay_bars=int(matcher.entry_fill == "open_t+1"),
            exit_delay_bars=int(matcher.exit_fill == "open_t+1"),
        )

    return _optimize_daily(
        dates, strategy.required_warmup_bars, match, matcher,
        start=start, end=end, params_meta=params_meta, param_grid=param_grid,
        base_params=base_params, selection=selection, objective=objective, direction=direction,
        k_segments=k_segments, min_trades_total=min_trades_total, stage2_top_n=stage2_top_n,
        progress_cb=progress_cb, cancel_event=cancel_event,
    )


def optimize_daily_panel(
    panel: pl.DataFrame,
    strategy: LegacyPolarsStrategy,
    matcher: MatcherConfig,
    *,
    selection: MatrixPipelineConfig | None = None,
    **search,
) -> dict:
    """Keep Float64 panel decisions and use the same optimizer and matcher as TSP."""
    if "symbol" not in panel.columns or panel.schema.get("date") != pl.Date:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Polars daily panel needs symbol and date")
    config = strategy.validate_panel(panel, selection)
    panel = panel.sort("symbol", "date")
    keys = panel.select("symbol", "date")
    if keys.null_count().sum_horizontal().item() or keys.n_unique() != panel.height:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Polars daily panel has invalid or duplicate keys")
    dates = tuple(panel["date"].unique().sort())

    @lru_cache(maxsize=16)
    def view(history: int, first: int, stop: int):
        window = panel.filter(pl.col("date").is_between(dates[history], dates[stop - 1]))
        active = window["date"] >= dates[first]
        encoding = encode_panel_matrix(
            window.filter(active), entry_signal_ids=strategy.entry_signals,
            exit_signal_ids=strategy.exit_signals,
        )
        return window, active, encoding

    def match(history, first, stop, params):
        window, active, encoding = view(history, first, stop)
        scored, _, entry, exits = strategy.evaluate(window, params, config)
        entries = entry.filter(active)
        if not entries.any():
            return None
        return build_market_matrix_from_encoding(
            encoding, score=scored["score"].filter(active), entries=entries, exits=exits.filter(active),
            entry_delay_bars=int(matcher.entry_fill == "open_t+1"),
            exit_delay_bars=int(matcher.exit_fill == "open_t+1"),
        )

    return _optimize_daily(dates, strategy.required_warmup_bars, match, matcher, selection=config, **search)


def _optimize_daily(
    dates: tuple[date, ...],
    lookback: Callable[[dict], int],
    match: Callable[[int, int, int, dict], MarketMatrix | None],
    matcher: MatcherConfig,
    *,
    start: date,
    end: date,
    params_meta: list[dict],
    param_grid: dict,
    base_params: dict | None = None,
    selection: MatrixPipelineConfig | None = None,
    objective: str = "sortino",
    direction: Literal["max", "min"] | None = None,
    k_segments: int = 4,
    min_trades_total: int = 30,
    stage2_top_n: int = 20,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: Event | None = None,
) -> dict:
    started_at = time.perf_counter()
    validate_primary_objective(objective)
    if matcher.minute_fill or matcher.exit_fill == "signal_next_minute":
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Daily optimization needs daily fills")
    if not dates or any(left >= right for left, right in zip(dates, dates[1:], strict=False)):
        raise ValueError("Daily optimization requires ordered, unique dates")
    combos = expand_param_grid(params_meta, param_grid)
    windows = split_validation_windows(start, end, k_segments)
    fixed_params = dict(base_params or {})
    selection_warmup = 0
    if selection is not None:
        selection_warmup = scoring_warmup_bars(selection.scoring)
        if selection.order_by and selection.order_by != "score":
            selection_warmup = max(
                selection_warmup, scoring_warmup_bars({selection.order_by: 1.0})
            )
    simulator = LegacySimulator()
    options = SimulationOptions(
        include_monte_carlo=objective.startswith("mc_maxdd_"),
        include_curves=False,
        include_trades=False,
        include_per_symbol_stats=False,
        include_return_distribution=False,
    )

    def evaluate(combo: dict, first_day: date, last_day: date) -> tuple[dict, str | None]:
        first = bisect_left(dates, first_day)
        stop = bisect_right(dates, last_day)
        if first == stop:
            return {}, "正式回测区间内无数据"
        params = {**fixed_params, **combo}
        warmup = lookback(params)
        if warmup < 0:
            raise ValueError("Strategy warmup must be nonnegative")
        history = max(0, first - max(warmup, selection_warmup))
        matched = match(history, first, stop, params)
        if matched is None:
            return {}, "在指定区间内未产生买入信号"
        result = simulator.simulate_market_matrix(
            matched, matcher, cancel_event=cancel_event, options=options
        )
        stats = {name: result.stats.get(name) for name in (objective, "n_trades")}
        return stats, result.stats.get("error")

    return run_robust_search(
        combos,
        windows,
        evaluate,
        objective=objective,
        direction=direction or default_direction(objective),
        min_trades_total=min_trades_total,
        stage2_top_n=stage2_top_n,
        started_at=started_at,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
