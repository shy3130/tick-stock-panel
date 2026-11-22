"""Run a legacy minute filter without a workbench or provider registry."""

import json
from dataclasses import dataclass, replace
from datetime import date
from inspect import signature
from math import isfinite
from typing import Literal

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..research.matrix import LEGACY_PRICE_SCALE
from ..research.panel_selection import (
    DEFAULT_BASIC_FILTER,
    join_minute_daily_fields,
    materialize_strategy_frames,
    panel_basic_filter,
    score_strategy_rows,
    sort_minute_strategy_rows,
    strategy_result_limit,
)
from ..research.scoring import effective_scoring, effective_scoring_directions
from .minute_replay import (
    CN_TIMEZONE,
    MinuteReplayResult,
    prepare_minute_match,
    replay_minute_signals,
)
from .panel_strategy import legacy_parameter_defaults
from .simulator import LegacySimulator, MatcherConfig, SimResult


class LegacyMinuteStrategy:
    def __init__(self, source, parameters_json: str):
        meta = source.META
        self.meta = meta
        self.filter_history = source.filter_minute_history
        if not callable(self.filter_history) or list(meta.get("timeframes", [])) != ["1m"]:
            raise ValueError("Expected a single minute filter")
        self.daily_bars = int(meta.get("daily_history_bars") or 0)
        if not 0 <= self.daily_bars <= 250:
            raise ValueError("Minute daily history must be within [0, 250] bars")
        if self.daily_bars and "daily" not in signature(self.filter_history).parameters:
            raise ValueError("Minute filters with daily history need a daily keyword")
        self.defaults = {**legacy_parameter_defaults(meta), **json.loads(parameters_json)}
        self.required_intraday_signals = frozenset(
            name for name in (*(meta.get("required_features") or ()),
                              *(getattr(source, "REQUIRED_FEATURES", None) or ()))
            if name.startswith("csgi_")
        )
        self.basic_filter = {
            **DEFAULT_BASIC_FILTER,
            **(getattr(source, "BASIC_FILTER", None) or {}),
            **(meta.get("basic_filter") or {}),
        }

    def evaluate(self, history, current, daily_history, params, *, overrides=None, pool=None):
        overrides = overrides or {}
        if overrides.get("overlay_filter"):
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Minute filters do not support daily overlays")
        missing = self.required_intraday_signals - set(history.columns)
        if missing:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Missing intraday signals: " + ", ".join(sorted(missing)),
            )
        weights = effective_scoring(self.meta.get("scoring"), overrides)
        directions = effective_scoring_directions(overrides)
        current, history = materialize_strategy_frames(current, history, weights)
        params = {**self.defaults, **(overrides.get("params") or {}), **params}
        kwargs = {"daily": daily_history} if self.daily_bars else {}
        frame = self.filter_history(history, params, **kwargs)
        if not isinstance(frame, pl.DataFrame):
            raise TiveloError(ErrorCode.STRATEGY_FAILED, "Minute filter must return a Polars DataFrame")
        if frame.is_empty():
            return [], {}
        if not {"symbol", "close", "last_datetime"} <= set(frame.columns):
            raise TiveloError(ErrorCode.STRATEGY_FAILED, "Minute hits require symbol, close and last_datetime")
        if current is not None and not current.is_empty():
            frame = join_minute_daily_fields(frame, current)
        basic = {**self.basic_filter, **(overrides.get("basic_filter") or {})}
        if basic.get("enabled", True) and (mask := panel_basic_filter(frame, basic)) is not None:
            frame = frame.filter(mask)
        if pool:
            frame = frame.filter(pl.col("symbol").is_in(pool))
        frame = score_strategy_rows(frame, weights, directions)
        frame = sort_minute_strategy_rows(
            frame, overrides.get("order_by", self.meta.get("order_by")),
            overrides.get("descending", self.meta.get("descending", True)),
        )
        if (limit := strategy_result_limit(self.meta, overrides)) is not None:
            frame = frame.head(limit)
        rows = frame.to_dicts()
        for row in rows:
            for name, value in row.items():
                if isinstance(value, float) and not isfinite(value):
                    row[name] = None
        scores = {row["symbol"]: float(row.get("score") or 0) for row in rows} if "score" in frame.columns else {}
        return rows, scores


@dataclass(frozen=True)
class LegacyMinuteResult:
    simulation: SimResult
    replay: MinuteReplayResult
    trigger_times: dict[tuple[str, date], str]
    dropped_axis_hits: int


def run_legacy_minute(
    panel: pl.DataFrame,
    strategy: LegacyMinuteStrategy,
    matcher: MatcherConfig,
    *,
    start: date,
    end: date,
    minute_days,
    load_day,
    limit_for,
    params=None,
    overrides=None,
    pool=None,
    mode: Literal["position", "full"] = "position",
    simulation_end: date | None = None,
    market_timezone=CN_TIMEZONE,
    progress_cb=None,
    cancel_event=None,
    options=None,
) -> LegacyMinuteResult:
    """Use raw minute frames for signals and a prepared daily panel for exits."""
    if matcher.asset_type != "stock" or matcher.exit_fill == "signal_next_minute":
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Legacy minute strategy needs stock daily exits")
    sim_end = simulation_end or end
    if start > end or sim_end < end or mode not in {"position", "full"}:
        raise ValueError("Invalid minute replay range or mode")
    if "raw_close" in panel.columns and LEGACY_PRICE_SCALE not in panel.columns:
        factors = panel.select(
            pl.when((pl.col("raw_close") > 0) & (pl.col("close") > 0))
            .then(pl.col("close") / pl.col("raw_close"))
            .otherwise(1.0).alias(LEGACY_PRICE_SCALE)
        ).to_series()
        if (factors != 1.0).any():
            panel = panel.with_columns(factors)

    def evaluate_day(_day, history, current, daily_history):
        return strategy.evaluate(history, current, daily_history, params or {}, overrides=overrides, pool=pool)

    replay = replay_minute_signals(
        panel, start=start, end=end, daily_bars=strategy.daily_bars,
        minute_days=sorted({day for day in minute_days if start <= day <= end}),
        load_day=load_day, evaluate_day=evaluate_day, limit_for=limit_for,
        market_timezone=market_timezone, progress_cb=progress_cb, cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise TiveloError(ErrorCode.CANCELLED, "Minute replay was cancelled")
    sim_panel = panel.filter(pl.col("date").is_between(start, sim_end))
    if sim_panel.is_empty():
        raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "No daily bars in the minute simulation range")
    matched = prepare_minute_match(sim_panel, replay.hits, next_open_exit=matcher.exit_fill == "open_t+1")
    matcher = replace(matcher, entry_fill="close_t", minute_fill=False)
    simulator = LegacySimulator()
    simulate_options = dict(progress_cb=progress_cb, cancel_event=cancel_event, options=options)
    if mode == "full":
        simulation = simulator.simulate_independent_market_matrix(
            matched.market, matched.candidate_count, matcher, **simulate_options,
        )
    else:
        simulation = simulator.simulate_market_matrix(matched.market, matcher, **simulate_options)
    if cancel_event is not None and cancel_event.is_set():
        raise TiveloError(ErrorCode.CANCELLED, "Minute simulation was cancelled")
    execution = simulation.stats.get("execution") or {}
    execution["buy_limit_up"] = int(execution.get("buy_limit_up", 0)) + replay.buy_limit_up
    simulation.stats["execution"] = execution
    return LegacyMinuteResult(simulation, replay, matched.trigger_times, matched.dropped_axis_hits)
