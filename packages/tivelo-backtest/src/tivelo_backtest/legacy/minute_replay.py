"""Replay existing minute strategy hits and build the legacy daily match grid."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone

import numpy as np
import polars as pl

from ..research.matrix import (
    LEGACY_PRICE_SCALE,
    MarketMatrix,
    SignalMatrix,
    build_market_data_matrix,
    build_market_matrix_from_signals,
)

CN_TIMEZONE = timezone(timedelta(hours=8))


@dataclass
class MinuteReplayHit:
    trade_date: date
    symbol: str
    entry_price: float
    trigger_time: str
    score: float = 0.0


@dataclass
class MinuteReplayResult:
    hits: list[MinuteReplayHit] = field(default_factory=list)
    skipped_days: list[date] = field(default_factory=list)
    replayed_days: int = 0
    strategy_matches: int = 0
    buy_limit_up: int = 0
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class MinuteMatchInput:
    market: MarketMatrix
    candidate_count: int
    trigger_times: dict[tuple[str, date], str]
    dropped_axis_hits: int


def _trigger_hhmm(value, market_timezone) -> str:
    if hasattr(value, "astimezone"):
        local = value if value.tzinfo is None else value.astimezone(market_timezone)
        return local.strftime("%H:%M")
    text = str(value or "")
    if len(text) >= 16 and text[13] == ":":
        return text[11:16]
    return text[-5:] if text else ""


def _scalar_limit_up_price(prev_close: float, limit_pct: float) -> float:
    cents = int(prev_close * 100 + 0.5)
    numerator = round((1 + limit_pct) * 100)
    return ((cents * numerator + 50) // 100) / 100


def _prior_quotes(prior_snapshot: pl.DataFrame) -> dict[str, tuple[float, str, float]]:
    if "raw_close" not in prior_snapshot.columns:
        prior_snapshot = prior_snapshot.with_columns(pl.col("close").alias("raw_close"))
    if "name" not in prior_snapshot.columns:
        prior_snapshot = prior_snapshot.with_columns(pl.lit("").alias("name"))
    quotes = {}
    for symbol, raw, name, close in prior_snapshot.select("symbol", "raw_close", "name", "close").iter_rows():
        factor = float(close) / float(raw) if close and raw and raw > 0 else 0.0
        quotes[str(symbol)] = (float(raw or 0), str(name or ""), factor)
    return quotes


def entry_price_factors(day_rows: pl.DataFrame) -> dict[str, float]:
    """Convert an exchange price to the basis used by the daily match grid."""
    if LEGACY_PRICE_SCALE in day_rows.columns:
        return dict(day_rows.select("symbol", LEGACY_PRICE_SCALE).iter_rows())
    if day_rows.is_empty() or "raw_close" not in day_rows.columns:
        return {}
    return {
        str(symbol): float(close) / float(raw)
        for symbol, close, raw in day_rows.select("symbol", "close", "raw_close").iter_rows()
        if close and raw and raw > 0
    }


def replay_minute_signals(
    panel: pl.DataFrame,
    *,
    start: date,
    end: date,
    daily_bars: int,
    minute_days,
    load_day,
    evaluate_day,
    limit_for,
    minute_prices_are_raw: bool = True,
    factors_for=entry_price_factors,
    project_day=None,
    market_timezone=CN_TIMEZONE,
    progress_cb=None,
    cancel_event=None,
) -> MinuteReplayResult:
    """Read observed dates only. Each daily window ends before the minute signal date."""
    started = time.perf_counter()
    result = MinuteReplayResult()
    if panel.is_empty():
        return result
    dates = panel["date"].unique().sort().to_list()
    positions = {day: index for index, day in enumerate(dates)}
    observed = tuple(minute_days)
    minute_dates = set(observed)
    result.skipped_days = [day for day in dates if start <= day <= end and day not in minute_dates]
    for step, day in enumerate(observed, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb is not None:
            progress_cb({"day": step, "total": len(observed), "date": str(day)})
        index = positions.get(day)
        if index is None:
            result.skipped_days.append(day)
            continue
        projected = panel if project_day is None else project_day(day)
        history = load_day(day)
        if history.is_empty():
            result.skipped_days.append(day)
            continue

        daily_history = current = None
        quotes = {}
        factors = factors_for(panel.filter(pl.col("date") == day))
        if index:
            last_prior = dates[index - 1]
            current = projected.filter(pl.col("date") == last_prior)
            if daily_bars:
                first_prior = dates[max(0, index - 1 - daily_bars)]
                window = projected.filter(pl.col("date").is_between(first_prior, last_prior))
                daily_history = None if window.is_empty() else window
            prior_rows = panel.filter(pl.col("date") == last_prior) if project_day is not None else current
            if not prior_rows.is_empty():
                quotes = _prior_quotes(prior_rows)
            if current.is_empty():
                current = None

        evaluated = evaluate_day(day, history, current, daily_history)
        if evaluated is None:
            result.skipped_days.append(day)
            continue
        rows, scores = evaluated
        result.replayed_days += 1
        result.strategy_matches += len(rows)
        for row in rows:
            symbol, close = row.get("symbol"), row.get("close")
            if not symbol or close is None or float(close) <= 0:
                continue
            symbol = str(symbol)
            prev, name, prev_adjustment = quotes.get(symbol, (0.0, "", 0.0))
            factor = factors.get(symbol, 1.0)
            minute_close = float(close)
            raw = minute_close if minute_prices_are_raw else minute_close / factor
            if prev > 0:
                reference = (
                    prev * prev_adjustment / factor
                    if prev_adjustment > 0 and factor > 0 and abs(factor - prev_adjustment) > 1e-9
                    else prev
                )
                limit = limit_for(symbol, day, name)
                if limit is not None and raw >= _scalar_limit_up_price(reference, limit) - 1e-9:
                    result.buy_limit_up += 1
                    continue
            result.hits.append(MinuteReplayHit(
                trade_date=day,
                symbol=symbol,
                entry_price=raw * factor if minute_prices_are_raw else minute_close,
                trigger_time=_trigger_hhmm(row.get("last_datetime"), market_timezone),
                score=float(scores.get(symbol, 0.0) or 0.0),
            ))
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return result


def prepare_minute_match(
    panel: pl.DataFrame, hits: list[MinuteReplayHit], *, next_open_exit: bool,
) -> MinuteMatchInput:
    """Enter at the trigger close and use the existing daily matrix for exits."""
    market = build_market_data_matrix(panel, field_columns={LEGACY_PRICE_SCALE} & set(panel.columns))
    time_index = {date.fromisoformat(label[:10]): index for index, label in enumerate(market.timestamp_labels)}
    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    shape = market.shape
    entry = np.zeros(shape, dtype=np.uint8)
    score = np.zeros(shape, dtype=np.float32)
    prices = np.full(shape, np.nan, dtype=np.float32)
    trigger_times = {}
    dropped = 0
    for hit in hits:
        time_id, asset_id = time_index.get(hit.trade_date), asset_index.get(hit.symbol)
        if time_id is None or asset_id is None:
            dropped += 1
            continue
        entry[time_id, asset_id] = 1
        score[time_id, asset_id] = hit.score
        prices[time_id, asset_id] = hit.entry_price
        trigger_times[hit.symbol, hit.trade_date] = hit.trigger_time
    candidate_count = int(entry.sum())
    exit_mask = np.zeros(shape, dtype=np.uint8)
    codes = np.zeros(shape, dtype=np.int16)
    for array in (entry, score, prices, exit_mask, codes):
        array.setflags(write=False)
    signals = SignalMatrix(entry, exit_mask, score, codes, codes, (), ())
    matched = build_market_matrix_from_signals(
        market, signals, entry_delay_bars=0, exit_delay_bars=int(next_open_exit), entry_price_override=prices,
    )
    return MinuteMatchInput(matched, candidate_count, trigger_times, dropped)
