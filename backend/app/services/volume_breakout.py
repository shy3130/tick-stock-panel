"""Auditable volume-divergence/convergence breakout research factor."""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean
from typing import Any, Literal, Protocol

import polars as pl
from pydantic import BaseModel, ConfigDict
from app.services.universe_scd import UniverseScdIntegrityError

FACTOR_ID = "volume_breakout_v1"
FACTOR_VERSION = 1
FACTOR_NAME = "量价序列突破（研究）"
FACTOR_DESCRIPTION = (
    "放量（raw volume 与 amount 双 P90）后 3-15 市场日箱体整理冻结，"
    "raw_close 越过冻结箱体上/下沿确认的只读事件研究因子"
)
REACHABILITY = "daily_price_only"
REFERENCE_WINDOW = 20
VOLUME_PERCENTILE = 0.90
CONSOLIDATION_MIN_DAYS = 3
CONSOLIDATION_MAX_DAYS = 15
BOX_WIDTH_MAX = 0.12
FORWARD_HORIZONS = (1, 5, 10, 20)
VARIANT_UP_BREAKOUT = "up_breakout"
VARIANT_DOWN_BREAKOUT = "down_breakout"
VARIANTS = (VARIANT_UP_BREAKOUT, VARIANT_DOWN_BREAKOUT)
REQUIRED_RAW_COLUMNS = ("raw_high", "raw_low", "raw_close", "volume", "amount")
DEFAULT_OOS_START = date(2025, 7, 1)
DEFAULT_COST_BPS = 10.0
_BANNED_TRADING_TOKENS = (
    "buy", "sell", "target", "stop", "action", "entry", "exit",
    "position", "order", "long", "short", "hold", "trade",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class VolumeBreakoutFactor(_StrictModel):
    factor_id: str
    version: int
    name: str
    description: str
    reachability: Literal["daily_price_only"]


class VolumeBreakoutRequest(_StrictModel):
    start: date
    end: date
    symbols: list[str] | None = None
    oos_start: date = DEFAULT_OOS_START
    cost_bps: float = DEFAULT_COST_BPS


class VolumeBreakoutCapabilities(_StrictModel):
    generation_pinned_reader: bool = False
    pit_eligible_universe: bool = False
    versioned_exchange_calendar: bool = False


class VolumeBreakoutResponse(_StrictModel):
    factor: VolumeBreakoutFactor
    status: Literal["ok", "unavailable"]
    unavailable_reasons: list[str]
    request: VolumeBreakoutRequest
    capabilities: VolumeBreakoutCapabilities
    parameters: dict[str, Any]
    provenance: dict[str, Any]
    coverage: dict[str, Any] | None
    events: list[Any]
    clusters: list[Any]
    censored: list[Any]
    research: dict[str, Any] | None = None
    note: str


class GenerationPinnedDailyReader(Protocol):
    def generation(self) -> str: ...
    def manifest_sha256(self) -> str: ...
    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...


class PitEligibleUniverse(Protocol):
    def source_manifest(self) -> dict[str, Any]: ...
    def snapshot_identity(self, event_date: date) -> dict[str, Any]: ...
    def eligible_symbols(self, event_date: date) -> list[str]: ...
    def prefetch_event_days(self, event_days: list[date]) -> dict[date, tuple[dict[str, Any], list[str]]]: ...


class VersionedExchangeCalendar(Protocol):
    def version(self) -> str: ...
    def market_days(self, start: date, end: date) -> list[date]: ...


PINNED_READER_ATTR = "generation_pinned_daily_reader"
PIT_UNIVERSE_ATTR = "pit_eligible_universe"
CALENDAR_ATTR = "versioned_exchange_calendar"


def _resolve_capability(repo: Any, attr: str, required: tuple[str, ...]) -> Any | None:
    capability = getattr(repo, attr, None)
    if capability is None:
        return None
    return capability if all(callable(getattr(capability, name, None)) for name in required) else None


def resolve_pinned_reader(repo: Any) -> GenerationPinnedDailyReader | None:
    return _resolve_capability(repo, PINNED_READER_ATTR, ("generation", "manifest_sha256", "daily_bars"))


def resolve_pit_universe(repo: Any) -> PitEligibleUniverse | None:
    return _resolve_capability(repo, PIT_UNIVERSE_ATTR, ("source_manifest", "snapshot_identity", "eligible_symbols", "prefetch_event_days"))


def resolve_versioned_calendar(repo: Any) -> VersionedExchangeCalendar | None:
    return _resolve_capability(repo, CALENDAR_ATTR, ("version", "market_days"))


def assert_no_trading_tokens(name: str) -> None:
    lowered = name.lower()
    for token in _BANNED_TRADING_TOKENS:
        if token in lowered:
            raise ValueError(f"trading semantics token {token!r} forbidden in field {name!r}")


def _validate_keys_no_trading_tokens(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_trading_tokens(str(key))
            _validate_keys_no_trading_tokens(value)
    elif isinstance(payload, list):
        for item in payload:
            _validate_keys_no_trading_tokens(item)


def _factor_meta() -> dict[str, Any]:
    return {
        "factor_id": FACTOR_ID,
        "version": FACTOR_VERSION,
        "name": FACTOR_NAME,
        "description": FACTOR_DESCRIPTION,
        "reachability": REACHABILITY,
    }


def _locked_parameters() -> dict[str, Any]:
    return {
        "reference_window_market_days": REFERENCE_WINDOW,
        "volume_percentile": VOLUME_PERCENTILE,
        "consolidation_window_days": [CONSOLIDATION_MIN_DAYS, CONSOLIDATION_MAX_DAYS],
        "box_width_max": BOX_WIDTH_MAX,
        "forward_horizons_market_days": list(FORWARD_HORIZONS),
        "variants": list(VARIANTS),
        "required_raw_columns": list(REQUIRED_RAW_COLUMNS),
        "price_scale": "raw",
    }


def unavailable_envelope(
    *, start: date, end: date, reasons: list[str], symbols: list[str] | None = None,
    oos_start: date = DEFAULT_OOS_START, cost_bps: float = DEFAULT_COST_BPS,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    payload = {
        "factor": _factor_meta(),
        "status": "unavailable",
        "unavailable_reasons": list(reasons),
        "request": {"start": start, "end": end, "symbols": symbols, "oos_start": oos_start, "cost_bps": cost_bps},
        "capabilities": {
            "generation_pinned_reader": False,
            "pit_eligible_universe": False,
            "versioned_exchange_calendar": False,
            **dict(capabilities or {}),
        },
        "parameters": _locked_parameters(),
        "provenance": {},
        "coverage": None,
        "events": [],
        "clusters": [],
        "censored": [],
        "research": None,
        "note": "缺少可证明的 sealed/PIT 能力时不产生事件；输出仅用于研究诊断，不构成投资建议",
    }
    _validate_keys_no_trading_tokens(payload)
    return VolumeBreakoutResponse.model_validate(payload).model_dump(mode="json")


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _prepare_bars(frame: pl.DataFrame, symbol: str) -> tuple[dict[date, dict[str, Any]], dict[str, Any] | None]:
    if frame is None or frame.is_empty():
        return {}, {"symbol": symbol, "code": "no_data"}
    missing = [column for column in ("date", *REQUIRED_RAW_COLUMNS) if column not in frame.columns]
    if missing:
        return {}, {"symbol": symbol, "code": "raw_field_missing", "fields": missing}
    result: dict[date, dict[str, Any]] = {}
    for row in frame.sort("date").to_dicts():
        values = [row.get(column) for column in REQUIRED_RAW_COLUMNS]
        if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
            return {}, {"symbol": symbol, "code": "raw_field_invalid", "date": str(row.get("date"))}
        result[row["date"]] = row
    return result, None


def _forward(event: dict[str, Any], bars: dict[date, dict[str, Any]], calendar: list[date], cost_bps: float) -> dict[int, Any]:
    index = {day: i for i, day in enumerate(calendar)}
    trigger = event["confirm_date"]
    base = float(bars[trigger]["raw_close"])
    trigger_index = index[trigger]
    output: dict[int, Any] = {}
    for horizon in FORWARD_HORIZONS:
        target_index = trigger_index + horizon
        later = bars.get(calendar[target_index]) if target_index < len(calendar) else None
        if later is None:
            output[horizon] = None
        else:
            gross = float(later["raw_close"]) / base - 1.0
            output[horizon] = {
                "gross_return": gross,
                "post_cost_return": gross - cost_bps / 10000.0,
                "cost_bps": cost_bps,
            }
    return output


def _scan_symbol(
    symbol: str,
    bars: dict[date, dict[str, Any]],
    calendar: list[date],
    request_days: frozenset[date],
    prefetched: dict[date, tuple[dict[str, Any], list[str]]],
    eligible_sets: dict[date, frozenset[str]],
    cost_bps: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    for event_index in range(REFERENCE_WINDOW, len(calendar) - CONSOLIDATION_MIN_DAYS):
        event_day = calendar[event_index]
        if event_day not in request_days:
            continue
        event_bar = bars.get(event_day)
        if event_bar is None:
            continue
        prior_days = calendar[event_index - REFERENCE_WINDOW:event_index]
        prior = [bars.get(day) for day in prior_days]
        if any(row is None for row in prior):
            continue
        prior_rows = [row for row in prior if row is not None]
        if float(event_bar["volume"]) < _percentile([float(row["volume"]) for row in prior_rows], VOLUME_PERCENTILE):
            continue
        if float(event_bar["amount"]) < _percentile([float(row["amount"]) for row in prior_rows], VOLUME_PERCENTILE):
            continue
        identity = prefetched[event_day][0]
        if symbol not in eligible_sets[event_day]:
            censored.append({"symbol": symbol, "event_date": event_day, "code": "pit_universe_ineligible"})
            continue

        consolidation: list[dict[str, Any]] = []
        freeze: dict[str, Any] | None = None
        failed = False
        for offset in range(1, CONSOLIDATION_MAX_DAYS + 1):
            if event_index + offset >= len(calendar):
                break
            day = calendar[event_index + offset]
            row = bars.get(day)
            if row is None:
                censored.append({"symbol": symbol, "event_date": event_day, "code": "market_day_bar_missing", "date": day})
                failed = True
                break
            if freeze is not None:
                close = float(row["raw_close"])
                variant = VARIANT_UP_BREAKOUT if close > freeze["box_high"] else VARIANT_DOWN_BREAKOUT if close < freeze["box_low"] else None
                if variant is not None:
                    event = {
                        "symbol": symbol,
                        "variant": variant,
                        "event_date": event_day,
                        "freeze_date": freeze["freeze_date"],
                        "confirm_date": day,
                        "box_high": freeze["box_high"],
                        "box_low": freeze["box_low"],
                        "box_width": freeze["box_width"],
                        "event_low": min(float(item["raw_low"]) for item in consolidation),
                        "universe_hash": identity["content_hash"],
                    }
                    event["forward"] = _forward(event, bars, calendar, cost_bps)
                    events.append(event)
                    failed = True
                    break
                continue

            if consolidation:
                previous_high = max(float(item["raw_high"]) for item in consolidation)
                previous_low = min(float(item["raw_low"]) for item in consolidation)
                close = float(row["raw_close"])
                if close > previous_high or close < previous_low:
                    censored.append({"symbol": symbol, "event_date": event_day, "code": "consolidation_broke_before_freeze", "date": day})
                    failed = True
                    break
            consolidation.append(row)
            highs = [float(item["raw_high"]) for item in consolidation]
            lows = [float(item["raw_low"]) for item in consolidation]
            box_high, box_low = max(highs), min(lows)
            width = (box_high - box_low) / float(event_bar["raw_close"])
            per_bar_ok = all((float(item["raw_high"]) - float(item["raw_low"])) / float(item["raw_close"]) <= BOX_WIDTH_MAX for item in consolidation)
            if len(consolidation) >= CONSOLIDATION_MIN_DAYS and width <= BOX_WIDTH_MAX and per_bar_ok:
                freeze = {"freeze_date": day, "box_high": box_high, "box_low": box_low, "box_width": width}
        if freeze is None and not failed:
            censored.append({"symbol": symbol, "event_date": event_day, "code": "consolidation_timeout"})
    return events, censored


def _clusters(events: list[dict[str, Any]], calendar: list[date]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions = {day: index for index, day in enumerate(calendar)}
    retained: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for symbol in sorted({event["symbol"] for event in events}):
        rows = sorted((event for event in events if event["symbol"] == symbol), key=lambda event: event["confirm_date"])
        current: list[dict[str, Any]] = []
        end_index = -1
        for event in rows:
            start_index = positions[event["confirm_date"]] + 1
            event_end = positions[event["confirm_date"]] + max(FORWARD_HORIZONS)
            if current and start_index > end_index:
                retained.append(current[0])
                clusters.append({"symbol": symbol, "members": len(current), "retained_confirm_date": current[0]["confirm_date"]})
                current = []
            current.append(event)
            end_index = max(end_index, event_end)
        if current:
            retained.append(current[0])
            clusters.append({"symbol": symbol, "members": len(current), "retained_confirm_date": current[0]["confirm_date"]})
    return retained, clusters


def _research(events: list[dict[str, Any]], oos_start: date) -> dict[str, Any]:
    segments: dict[str, Any] = {}
    for segment in ("is", "oos"):
        rows = [event for event in events if (event["confirm_date"] >= oos_start) == (segment == "oos")]
        by_horizon: dict[int, Any] = {}
        for horizon in FORWARD_HORIZONS:
            values = [event["forward"][horizon]["post_cost_return"] for event in rows if event["forward"][horizon] is not None]
            by_horizon[horizon] = {"n": len(values), "mean_post_cost_return": mean(values) if values else None, "censored": len(rows) - len(values)}
        segments[segment] = {"events": len(rows), "by_horizon": by_horizon}
    oos_h1 = segments["oos"]["by_horizon"][1]
    verdict = "accepted" if oos_h1["n"] >= 30 and (oos_h1["mean_post_cost_return"] or 0) > 0 else "rejected"
    return {"oos_start": oos_start, "segments": segments, "verdict": verdict}


def evaluate_volume_breakout(
    *, start: date, end: date, symbols: list[str] | None,
    pinned_reader: GenerationPinnedDailyReader | None,
    pit_universe: PitEligibleUniverse | None,
    calendar: VersionedExchangeCalendar | None,
    oos_start: date = DEFAULT_OOS_START,
    cost_bps: float = DEFAULT_COST_BPS,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must be <= end")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and >= 0")
    reasons: list[str] = []
    if pinned_reader is None:
        reasons.append("generation_pinned_reader_missing")
    if pit_universe is None:
        reasons.append("pit_eligible_universe_missing")
    if calendar is None:
        reasons.append("versioned_exchange_calendar_missing")
    capabilities = {
        "generation_pinned_reader": pinned_reader is not None,
        "pit_eligible_universe": pit_universe is not None,
        "versioned_exchange_calendar": calendar is not None,
    }
    if reasons:
        return unavailable_envelope(start=start, end=end, reasons=reasons, symbols=symbols, oos_start=oos_start, cost_bps=cost_bps, capabilities=capabilities)
    if not start <= oos_start <= end:
        raise ValueError("oos_start must be within [start, end]")
    assert pinned_reader is not None and pit_universe is not None and calendar is not None
    market_days = sorted(
        set(calendar.market_days(start - timedelta(days=60), end + timedelta(days=40)))
    )
    request_market_days = [day for day in market_days if start <= day <= end]
    if not request_market_days:
        return unavailable_envelope(
            start=start,
            end=end,
            reasons=["versioned_exchange_calendar_empty"],
            symbols=symbols,
            oos_start=oos_start,
            cost_bps=cost_bps,
            capabilities=capabilities,
        )
    try:
        prefetched = pit_universe.prefetch_event_days(request_market_days)
    except UniverseScdIntegrityError:
        return unavailable_envelope(
            start=start,
            end=end,
            reasons=["pit_eligible_universe_unavailable"],
            symbols=symbols,
            oos_start=oos_start,
            cost_bps=cost_bps,
            capabilities=capabilities,
        )
    request_days = frozenset(request_market_days)
    eligible_sets = {day: frozenset(eligible) for day, (_, eligible) in prefetched.items()}
    if symbols is None:
        symbols = sorted(set().union(*(eligible_sets[day] for day in request_market_days)))
    all_events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    for symbol in sorted(set(symbols)):
        bars, error = _prepare_bars(pinned_reader.daily_bars(symbol, market_days[0], market_days[-1]), symbol)
        if error:
            censored.append(error)
            continue
        events, symbol_censored = _scan_symbol(symbol, bars, market_days, request_days, prefetched, eligible_sets, cost_bps)
        all_events.extend(events)
        censored.extend(symbol_censored)
    events, clusters = _clusters(all_events, market_days)
    payload = {
        "factor": _factor_meta(),
        "status": "ok",
        "unavailable_reasons": [],
        "request": {"start": start, "end": end, "symbols": symbols, "oos_start": oos_start, "cost_bps": cost_bps},
        "capabilities": capabilities,
        "parameters": _locked_parameters(),
        "provenance": {
            "generation": pinned_reader.generation(),
            "manifest_sha256": pinned_reader.manifest_sha256(),
            "calendar_version": calendar.version(),
            "universe_source": pit_universe.source_manifest(),
            "universe_intervals": _group_universe_intervals(request_market_days, prefetched),
        },
        "coverage": {"symbols": len(symbols), "market_days": len(market_days), "events_before_overlap_control": len(all_events), "events": len(events), "censored": len(censored)},
        "events": events,
        "clusters": clusters,
        "censored": censored,
        "research": _research(events, oos_start),
        "note": "日线价格可达性与成本仅为研究诊断，不构成投资建议",
    }
    _validate_keys_no_trading_tokens(payload)
    return VolumeBreakoutResponse.model_validate(payload).model_dump(mode="json")


def _group_universe_intervals(
    request_market_days: list[date],
    prefetched: dict[date, tuple[dict[str, Any], list[str]]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for event_day in request_market_days:
        identity = prefetched[event_day][0]
        key = (identity["content_hash"], identity["effective_from"], identity["effective_to"])
        group = groups.get(key)
        if group is None:
            group = {
                "content_hash": identity["content_hash"],
                "effective_from": identity["effective_from"],
                "effective_to": identity["effective_to"],
                "available_at": identity["available_at"],
                "event_days": 0,
            }
            groups[key] = group
        group["event_days"] += 1
    return sorted(groups.values(), key=lambda item: item["effective_from"])
__all__ = [
    "FACTOR_ID", "FACTOR_VERSION", "FACTOR_NAME", "REACHABILITY",
    "REFERENCE_WINDOW", "VOLUME_PERCENTILE", "CONSOLIDATION_MIN_DAYS",
    "CONSOLIDATION_MAX_DAYS", "BOX_WIDTH_MAX", "FORWARD_HORIZONS",
    "VARIANT_UP_BREAKOUT", "VARIANT_DOWN_BREAKOUT", "VARIANTS",
    "REQUIRED_RAW_COLUMNS", "DEFAULT_OOS_START", "DEFAULT_COST_BPS",
    "VolumeBreakoutRequest", "VolumeBreakoutCapabilities", "VolumeBreakoutResponse",
    "GenerationPinnedDailyReader", "PitEligibleUniverse", "VersionedExchangeCalendar",
    "PINNED_READER_ATTR", "PIT_UNIVERSE_ATTR", "CALENDAR_ATTR",
    "resolve_pinned_reader", "resolve_pit_universe", "resolve_versioned_calendar",
    "assert_no_trading_tokens", "unavailable_envelope", "evaluate_volume_breakout",
]
