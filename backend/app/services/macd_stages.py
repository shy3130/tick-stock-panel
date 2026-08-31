"""MACD(10/20/7) staged research with strict PIT boundaries."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Protocol

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.services.hold_firm_patterns.statistics import selection_cluster_bootstrap

MACD_PARAMS = {"fast": 10, "slow": 20, "signal": 7}
SCHEMA = "tickflow.research.macd-stages.v1"
WARMUP_BARS = 26
STATE_VALUES = (
    "initial",
    "below_shrink",
    "below_expand",
    "cross_up",
    "above_expand",
    "above_shrink",
    "cross_down",
)
PINNED_READER_ATTR = "generation_pinned_daily_reader"
_READER_METHODS = ("generation", "manifest_sha256", "market_days", "daily_bars")
_SHA256 = frozenset("0123456789abcdef")


class GenerationPinnedDailyReader(Protocol):
    def generation(self) -> str: ...
    def manifest_sha256(self) -> str: ...
    def market_days(self, start: date, end: date) -> list[date]: ...
    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...


def _reader_ok(reader: Any) -> bool:
    return reader is not None and all(
        callable(getattr(reader, name, None)) for name in _READER_METHODS
    )


def resolve_pinned_reader(repo: Any) -> GenerationPinnedDailyReader | None:
    reader = getattr(repo, PINNED_READER_ATTR, None)
    return reader if _reader_ok(reader) else None


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= _SHA256


@dataclass(frozen=True, slots=True)
class MacdStagesAvailability:
    schema: str
    status: str
    params: dict[str, int]
    reasons: tuple[str, ...]
    missing_capabilities: dict[str, bool]
    contract_preview: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def macd_stages_availability(reader: Any | None = None) -> MacdStagesAvailability:
    valid = _reader_ok(reader)
    if valid:
        status, reasons = "available", ()
    elif reader is None:
        status, reasons = "unavailable", ("generation_pinned_reader_missing",)
    else:
        status, reasons = "unavailable", ("generation_pinned_reader_invalid",)
    return MacdStagesAvailability(
        SCHEMA,
        status,
        dict(MACD_PARAMS),
        reasons,
        {"daily_state_machine": False, "oos_evaluation": False, "pit_reader": not valid},
        {
            "required_fields": ["raw", "pit", "generation", "available_from"],
            "state_values": list(STATE_VALUES),
        },
    )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MacdStagesRequest(_StrictModel):
    start: date
    end: date
    symbols: list[str] | None = None
    oos_start: date

    @model_validator(mode="after")
    def validate_bounds(self) -> MacdStagesRequest:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if self.oos_start < self.start:
            raise ValueError("oos_start must be >= start")
        if self.symbols is not None and (
            not self.symbols or any(not isinstance(s, str) or not s.strip() for s in self.symbols)
        ):
            raise ValueError("symbols must contain non-empty strings")
        return self


def classify_stage(
    prev: tuple[float, float, float] | None, cur: tuple[float, float, float]
) -> str | None:
    if prev is None:
        return None
    pdif, pdea, phist = prev
    dif, dea, hist = cur
    if dif == dea:
        return None
    if dif > dea:
        if pdif <= pdea:
            return "cross_up"
        if hist > phist:
            return "above_expand"
        if hist < phist:
            return "above_shrink"
        return None
    if pdif >= pdea:
        return "cross_down"
    if abs(hist) < abs(phist):
        return "below_shrink"
    if abs(hist) > abs(phist):
        return "below_expand"
    return None


def zero_side(dif: float) -> str:
    return "positive" if dif > 0 else "negative" if dif < 0 else "zero"


def _bars(frame: Any, symbol: str) -> tuple[dict[date, dict[str, Any]], dict[str, Any] | None]:
    if frame is None or frame.is_empty():
        return {}, {"symbol": symbol, "code": "no_data", "detail": {}}
    missing = [field for field in ("date", "raw_close") if field not in frame.columns]
    if missing:
        return {}, {"symbol": symbol, "code": "raw_field_missing", "detail": {"fields": missing}}
    output: dict[date, dict[str, Any]] = {}
    for row in frame.sort("date").to_dicts():
        day, value = row.get("date"), row.get("raw_close")
        if day is None or value is None:
            return {}, {
                "symbol": symbol,
                "code": "raw_field_missing",
                "detail": {"fields": ["date" if day is None else "raw_close"]},
            }
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            return {}, {
                "symbol": symbol,
                "code": "raw_field_invalid",
                "detail": {"fields": ["raw_close"], "date": str(day)},
            }
        output[day] = row
    return output, None


def _arm_bars(frame: Any, symbol: str) -> tuple[dict[date, dict[str, Any]], dict[str, Any] | None]:
    """Arms consumption requires canonical adjusted close, not raw close."""
    if frame is None or frame.is_empty():
        return {}, {"symbol": symbol, "code": "no_data", "detail": {}}
    missing = [field for field in ("date", "close") if field not in frame.columns]
    if missing:
        return {}, {"symbol": symbol, "code": "close_field_missing", "detail": {"fields": missing}}
    output: dict[date, dict[str, Any]] = {}
    for row in frame.sort("date").to_dicts():
        day, value = row.get("date"), row.get("close")
        if day is None or value is None:
            return {}, {
                "symbol": symbol,
                "code": "close_field_missing",
                "detail": {"fields": ["date" if day is None else "close"]},
            }
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            return {}, {
                "symbol": symbol,
                "code": "close_field_invalid",
                "detail": {"fields": ["close"], "date": str(day)},
            }
        output[day] = row
    return output, None


def _rows(
    symbol: str,
    bars: dict[date, dict[str, Any]],
    calendar: list[date],
    next_day: dict[date, date],
    request: MacdStagesRequest,
    generation: str,
    manifest: str,
) -> list[dict[str, Any]]:
    af, ass, ag = (2.0 / (MACD_PARAMS[k] + 1.0) for k in ("fast", "slow", "signal"))
    ef = es = dea = None
    previous: tuple[float, float, float] | None = None
    seen = 0
    result: list[dict[str, Any]] = []
    for index, day in enumerate(calendar):
        bar = bars.get(day)
        if bar is None:
            continue
        seen += 1
        close = float(bar["raw_close"])
        ef = close if ef is None else ef + af * (close - ef)
        es = close if es is None else es + ass * (close - es)
        dif = ef - es
        dea = dif if dea is None else dea + ag * (dif - dea)
        hist = dif - dea
        current = (dif, dea, hist)
        if request.start <= day <= request.end and seen >= WARMUP_BARS and day in next_day:
            prev_market_has_bar = index > 0 and calendar[index - 1] in bars
            state = (
                "initial"
                if seen == WARMUP_BARS
                else classify_stage(previous if prev_market_has_bar else None, current)
            )
            result.append(
                {
                    "market_date": day,
                    "symbol": symbol,
                    "state": state,
                    "zero_side": zero_side(dif),
                    "available_from": next_day[day],
                    "raw": {
                        "snapshot_ref": f"sealed:{manifest}:{symbol}:{day.isoformat()}",
                        "raw_close": close,
                        "source_fields": ["raw_close"],
                    },
                    "pit": {"as_of": f"{day.isoformat()}T23:59:59Z", "generation": generation},
                    "generation": generation,
                    "macd": {"ema_fast": ef, "ema_slow": es, "dif": dif, "dea": dea, "hist": hist},
                }
            )
        previous = current
    return result


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    missing = 0
    for row in rows:
        if row["state"] is None:
            missing += 1
        else:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "rows": len(rows),
        "symbols": len({r["symbol"] for r in rows}),
        "first_market_date": rows[0]["market_date"] if rows else None,
        "last_market_date": rows[-1]["market_date"] if rows else None,
        "state_counts": counts,
        "state_missing_rows": missing,
    }


def _unavailable(request: MacdStagesRequest, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "unavailable_reasons": reasons,
        "request": request.model_dump(mode="json"),
        "provenance": {},
        "segments": {"is": {"coverage": None, "rows": []}, "oos": {"coverage": None, "rows": []}},
        "censored": [],
    }


def evaluate_macd_stages(
    reader: Any,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    oos_start: date | None = None,
) -> dict[str, Any]:
    try:
        request = MacdStagesRequest(start=start, end=end, symbols=symbols, oos_start=oos_start)
    except ValidationError as exc:
        raise ValueError(f"invalid macd-stages request: {exc.error_count()} errors") from exc
    if reader is None:
        return _unavailable(request, ["generation_pinned_reader_missing"])
    if not _reader_ok(reader):
        return _unavailable(request, ["generation_pinned_reader_invalid"])
    manifest = reader.manifest_sha256()
    if not _valid_hash(manifest):
        return _unavailable(request, ["reader_manifest_identity_invalid"])
    manifest, generation = manifest.lower(), reader.generation()
    lookup_start = request.start - timedelta(days=150)
    calendar = sorted(set(reader.market_days(lookup_start, request.end + timedelta(days=31))))
    next_day = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    if request.symbols is None:
        universe = getattr(reader, "universe", None)
        if not callable(universe):
            return _unavailable(request, ["reader_universe_missing"])
        symbols = sorted({str(s) for s in universe(request.start, request.end) if str(s)})
    else:
        symbols = sorted({s.strip() for s in request.symbols})
    rows: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    for symbol in symbols:
        bars, censor = _bars(reader.daily_bars(symbol, lookup_start, request.end), symbol)
        if censor:
            censored.append(censor)
            continue
        rows.extend(_rows(symbol, bars, calendar, next_day, request, generation, manifest))
    rows.sort(key=lambda row: (row["market_date"], row["symbol"]))
    is_rows = [row for row in rows if row["market_date"] < request.oos_start]
    oos_rows = [row for row in rows if row["market_date"] >= request.oos_start]
    return {
        "schema": SCHEMA,
        "status": "ok",
        "unavailable_reasons": [],
        "request": request.model_dump(mode="json"),
        "provenance": {
            "pinned_reader": {"generation": generation, "manifest_sha256": manifest},
            "factor_code": {
                "params": dict(MACD_PARAMS),
                "warmup_bars": WARMUP_BARS,
                "ema_seed": "first_valid_close",
                "alpha": "2/(n+1)",
                "price_scale": "raw",
            },
        },
        "segments": {
            "is": {"coverage": _coverage(is_rows), "rows": is_rows},
            "oos": {"coverage": _coverage(oos_rows), "rows": oos_rows},
        },
        "censored": sorted(censored, key=lambda item: (item["symbol"], item["code"])),
    }


ARMS_SCHEMA = "tickflow.research.macd-stages.arms.v1"
ROUND_TRIP_COST_BPS = 20.0
ROUND_TRIP_COST = ROUND_TRIP_COST_BPS / 10000.0
MIN_OOS_TRADES, MIN_OOS_SYMBOLS = 30, 10
BOOTSTRAP_SEED, BOOTSTRAP_ROUNDS, MIN_VALID_BOOTSTRAP_REPLICATES = 42, 5000, 4750
ARMS_WARMUP_BARS = 40
BREAKOUT_WINDOW, PULLBACK_WINDOW = 10, 10
ZERO_STD_WINDOW, MIN_STD_BARS = 60, 30
ZERO_BREAK_STD_MULT, PULLBACK_STD_MULT = 0.25, 0.25
MA_CONFIRM_BARS, MA_STOP_BARS = 5, 20


@dataclass(frozen=True, slots=True)
class MacdArmSpec:
    name: str
    fast: int
    slow: int
    signal: int
    stage: str

    def params(self) -> dict[str, int]:
        return {"fast": self.fast, "slow": self.slow, "signal": self.signal}


MACD_ARMS = (
    MacdArmSpec("default_cross", 12, 26, 9, "cross"),
    MacdArmSpec("tuned_cross", 10, 20, 7, "cross"),
    MacdArmSpec("tuned_breakout", 10, 20, 7, "breakout"),
    MacdArmSpec("tuned_pullback", 10, 20, 7, "pullback"),
    MacdArmSpec("tuned_ma_risk", 10, 20, 7, "ma_risk"),
)
MACD_INCREMENT_STEPS = (
    ("default_cross", "tuned_cross", "param_tuning"),
    ("tuned_cross", "tuned_breakout", "zero_axis_breakout"),
    ("tuned_breakout", "tuned_pullback", "zero_axis_pullback"),
    ("tuned_pullback", "tuned_ma_risk", "ma5_ma20_risk_control"),
)


class MacdArmsRequest(MacdStagesRequest):
    """Arms evaluation shares the frozen stage request contract."""


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _sample_std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _wilson(hits: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z2 = 1.96**2
    p = hits / count
    denominator = 1 + z2 / count
    center = (p + z2 / (2 * count)) / denominator
    half = 1.96 * math.sqrt(p * (1 - p) / count + z2 / (4 * count * count)) / denominator
    return center - half, center + half


def _oos_gates(metrics: Mapping[str, Any] | None) -> bool:
    return bool(
        metrics
        and metrics.get("n_trades_closed", 0) >= MIN_OOS_TRADES
        and metrics.get("n_symbols_traded", 0) >= MIN_OOS_SYMBOLS
    )


def macd_bootstrap_comparison(
    qualified: Mapping[str, Sequence[float]],
    baseline: Mapping[str, Sequence[float]],
    *,
    seed: int = BOOTSTRAP_SEED,
    rounds: int = BOOTSTRAP_ROUNDS,
) -> dict[str, Any]:
    result = selection_cluster_bootstrap(
        qualified,
        baseline,
        seed=seed,
        rounds=rounds,
        min_valid=MIN_VALID_BOOTSTRAP_REPLICATES,
    )
    return {
        "mean_difference": result.mean_difference,
        "ci_lower": result.lower,
        "ci_upper": result.upper,
        "valid_replicates": result.valid_replicates,
        "rounds": result.rounds,
        "n_qualified_trades": sum(len(values) for values in qualified.values()),
        "n_baseline_trades": sum(len(values) for values in baseline.values()),
    }


def macd_arm_verdict(
    oos_metrics: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    predecessor_oos: Mapping[str, Any] | None,
    *,
    is_default_arm: bool = False,
) -> dict[str, Any]:
    gates = {
        "oos_trades": oos_metrics.get("n_trades_closed", 0),
        "oos_symbols": oos_metrics.get("n_symbols_traded", 0),
        "minimum_oos_trades": MIN_OOS_TRADES,
        "minimum_oos_symbols": MIN_OOS_SYMBOLS,
    }
    if not _oos_gates(oos_metrics):
        return {
            "verdict": "unavailable",
            "reasons": ["unavailable_insufficient_oos_samples"],
            "gates": gates,
            "basis": "oos_only",
        }
    if not is_default_arm and not _oos_gates(predecessor_oos):
        return {
            "verdict": "unavailable",
            "reasons": ["unavailable_predecessor_insufficient_oos_samples"],
            "gates": gates,
            "basis": "oos_only",
        }
    if not is_default_arm and (
        comparison is None or comparison.get("valid_replicates", 0) < MIN_VALID_BOOTSTRAP_REPLICATES
    ):
        return {
            "verdict": "unavailable",
            "reasons": ["unavailable_bootstrap_min_valid_replicates"],
            "gates": gates,
            "basis": "oos_only",
        }
    reasons: list[str] = []
    if oos_metrics.get("mean_net_return") is None or oos_metrics["mean_net_return"] <= 0:
        reasons.append("mean_net_return_not_positive")
    if is_default_arm:
        if oos_metrics.get("wilson_lower") is None or oos_metrics["wilson_lower"] <= 0.5:
            reasons.append("win_rate_wilson_lower_not_above_half")
    elif comparison is None or comparison.get("ci_lower") is None or comparison["ci_lower"] <= 0:
        reasons.append("increment_ci_lower_not_positive")
    return {
        "verdict": "accepted" if not reasons else "rejected",
        "reasons": reasons,
        "gates": gates,
        "basis": "oos_only",
    }


def _simulate_macd_arm(
    arm: MacdArmSpec,
    series: Sequence[tuple[int, date, float]],
    start: date,
    end: date,
) -> dict[str, Any]:
    af, ass, ag = (2.0 / (value + 1.0) for value in (arm.fast, arm.slow, arm.signal))
    ef = es = dea = None
    previous: tuple[int, float, float] | None = None
    dif_history: list[float] = []
    short, long = deque(), deque()
    short_sum = long_sum = 0.0
    trade: dict[str, Any] | None = None
    pending_entry: tuple[int, date] | None = None
    pending_exit: tuple[int, date] | None = None
    watch: tuple[int, int] | None = None
    breakout: tuple[int, int, float] | None = None
    pullback_seen = False
    trades: list[dict[str, Any]] = []
    filtered: list[tuple[date, str]] = []

    def reset_watch() -> None:
        nonlocal watch, breakout, pullback_seen
        watch = None
        breakout = None
        pullback_seen = False

    for seen, (index, day, close) in enumerate(series, start=1):
        ef = close if ef is None else ef + af * (close - ef)
        es = close if es is None else es + ass * (close - es)
        dif = ef - es
        dea = dif if dea is None else dea + ag * (dif - dea)
        dif_history.append(dif)
        short.append(close)
        short_sum += close
        if len(short) > MA_CONFIRM_BARS:
            short_sum -= short.popleft()
        long.append(close)
        long_sum += close
        if len(long) > MA_STOP_BARS:
            long_sum -= long.popleft()
        ma5, ma20 = short_sum / len(short), long_sum / len(long)

        if pending_exit is not None and trade is not None and index > pending_exit[0]:
            gross = close / trade["entry_price"] - 1.0
            trade.update(
                exit_signal_date=pending_exit[1],
                exit_exec_date=day,
                exit_price=close,
                exit_lag_bars=index - pending_exit[0],
                gross_return=gross,
                net_return=gross - ROUND_TRIP_COST,
                status="closed",
            )
            trades.append(trade)
            trade = None
            pending_exit = None
        if pending_entry is not None and trade is None and index > pending_entry[0]:
            trade = {
                "entry_signal_date": pending_entry[1],
                "entry_exec_date": day,
                "entry_price": close,
                "entry_lag_bars": index - pending_entry[0],
                "exit_signal_date": None,
                "exit_exec_date": None,
                "exit_price": None,
                "exit_lag_bars": None,
                "gross_return": None,
                "net_return": None,
                "status": "open",
            }
            pending_entry = None
            reset_watch()

        consecutive = previous is not None and index == previous[0] + 1
        cross_up = consecutive and previous is not None and previous[1] <= previous[2] and dif > dea
        cross_down = (
            consecutive and previous is not None and previous[1] >= previous[2] and dif < dea
        )
        eligible = seen >= ARMS_WARMUP_BARS and start <= day <= end
        if eligible:
            if trade is not None:
                if arm.stage == "ma_risk":
                    if (cross_down and close < ma5) or close < ma20:
                        pending_exit = (index, day)
                elif cross_down:
                    pending_exit = (index, day)
            elif pending_entry is None:
                if arm.stage == "cross":
                    if cross_up:
                        pending_entry = (index, day)
                else:
                    std = (
                        _sample_std(dif_history[-ZERO_STD_WINDOW:])
                        if len(dif_history) >= MIN_STD_BARS
                        else None
                    )
                    tolerance = ZERO_BREAK_STD_MULT * std if std is not None else None
                    if watch is None and breakout is None:
                        if cross_up:
                            if dif < 0:
                                watch = (index, index + BREAKOUT_WINDOW)
                            else:
                                filtered.append((day, "cross_above_zero"))
                    elif watch is not None:
                        if dif > 0:
                            if arm.stage == "breakout":
                                pending_entry = (index, day)
                                watch = None
                            else:
                                breakout = (index, index + PULLBACK_WINDOW, dif)
                                watch = None
                        elif index > watch[1]:
                            filtered.append((day, "breakout_window_expired"))
                            watch = None
                    elif breakout is not None:
                        b_index, deadline, maximum = breakout
                        if tolerance is not None and dif <= -tolerance:
                            filtered.append((day, "zero_break_invalidated"))
                            reset_watch()
                        else:
                            maximum = max(maximum, dif)
                            breakout = (b_index, deadline, maximum)
                            pulled = std is not None and dif <= maximum - PULLBACK_STD_MULT * std
                            if arm.stage == "pullback":
                                if pulled:
                                    pending_entry = (index, day)
                                    reset_watch()
                                elif index > deadline:
                                    filtered.append((day, "pullback_window_expired"))
                                    reset_watch()
                            else:
                                if pulled:
                                    pullback_seen = True
                                if pullback_seen and close > ma5:
                                    pending_entry = (index, day)
                                    reset_watch()
                                elif index > deadline:
                                    filtered.append((day, "entry_confirm_window_expired"))
                                    reset_watch()
        previous = (index, dif, dea)

    if pending_entry is not None:
        filtered.append((pending_entry[1], "entry_exec_unavailable"))
    if trade is not None:
        trades.append(trade)
    return {"trades": trades, "filtered": filtered}


def _metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    nets = [float(trade["net_return"]) for trade in closed]
    gross = [float(trade["gross_return"]) for trade in closed]
    wins = sum(value > 0 for value in nets)
    lower, upper = _wilson(wins, len(nets))
    return {
        "n_events": len(trades),
        "n_trades_closed": len(closed),
        "n_trades_open": len(trades) - len(closed),
        "n_symbols_traded": len({str(trade["symbol"]) for trade in closed}),
        "win_rate": wins / len(nets) if nets else None,
        "wilson_lower": lower,
        "wilson_upper": upper,
        "mean_net_return": sum(nets) / len(nets) if nets else None,
        "median_net_return": _median(nets),
        "sum_net_return": sum(nets) if nets else None,
        "mean_gross_return": sum(gross) / len(gross) if gross else None,
    }


def _arm_unavailable(request: MacdArmsRequest, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema": ARMS_SCHEMA,
        "status": "unavailable",
        "unavailable_reasons": reasons,
        "request": request.model_dump(mode="json"),
        "provenance": {},
        "segments": {"is": {"coverage": None, "arms": {}}, "oos": {"coverage": None, "arms": {}}},
        "arms": {},
        "increments": [],
        "censored": [],
    }


def evaluate_macd_arms(
    reader: Any,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    oos_start: date | None = None,
) -> dict[str, Any]:
    try:
        request = MacdArmsRequest(start=start, end=end, symbols=symbols, oos_start=oos_start)
    except ValidationError as exc:
        raise ValueError(f"invalid macd-arms request: {exc.error_count()} errors") from exc
    if reader is None:
        return _arm_unavailable(request, ["generation_pinned_reader_missing"])
    if not _reader_ok(reader):
        return _arm_unavailable(request, ["generation_pinned_reader_invalid"])
    manifest = reader.manifest_sha256()
    if not _valid_hash(manifest):
        return _arm_unavailable(request, ["reader_manifest_identity_invalid"])
    manifest, generation = manifest.lower(), reader.generation()
    lookup_start = request.start - timedelta(days=150)
    calendar = sorted(set(reader.market_days(lookup_start, request.end + timedelta(days=31))))
    if request.symbols is None:
        universe = getattr(reader, "universe", None)
        if not callable(universe):
            return _arm_unavailable(request, ["reader_universe_missing"])
        symbols = sorted(
            {str(symbol) for symbol in universe(request.start, request.end) if str(symbol)}
        )
    else:
        symbols = sorted({symbol.strip() for symbol in request.symbols})
    all_trades: dict[str, dict[str, list[dict[str, Any]]]] = {arm.name: {} for arm in MACD_ARMS}
    all_filtered: dict[str, list[tuple[date, str]]] = {arm.name: [] for arm in MACD_ARMS}
    censored: list[dict[str, Any]] = []
    for symbol in symbols:
        bars, censor = _arm_bars(reader.daily_bars(symbol, lookup_start, request.end), symbol)
        if censor:
            censored.append(censor)
            continue
        series = [
            (index, day, float(bars[day]["close"]))
            for index, day in enumerate(calendar)
            if day in bars
        ]
        for arm in MACD_ARMS:
            simulated = _simulate_macd_arm(arm, series, request.start, request.end)
            for trade in simulated["trades"]:
                trade["symbol"] = symbol
            all_trades[arm.name][symbol] = simulated["trades"]
            all_filtered[arm.name].extend(simulated["filtered"])

    def segment_trades(name: str, segment: str) -> list[dict[str, Any]]:
        return [
            trade
            for trades in all_trades[name].values()
            for trade in trades
            if (trade["entry_exec_date"] >= request.oos_start) == (segment == "oos")
        ]

    segment_stats = {
        segment: {arm.name: _metrics(segment_trades(arm.name, segment)) for arm in MACD_ARMS}
        for segment in ("is", "oos")
    }
    oos_returns = {
        arm.name: {
            symbol: [
                float(trade["net_return"])
                for trade in trades
                if trade["status"] == "closed" and trade["entry_exec_date"] >= request.oos_start
            ]
            for symbol, trades in all_trades[arm.name].items()
        }
        for arm in MACD_ARMS
    }
    arms_payload: dict[str, Any] = {}
    for position, arm in enumerate(MACD_ARMS):
        predecessor = MACD_ARMS[position - 1] if position else None
        comparisons: dict[str, Any] = {}
        if (
            predecessor
            and _oos_gates(segment_stats["oos"][arm.name])
            and _oos_gates(segment_stats["oos"][predecessor.name])
        ):
            comparisons["vs_predecessor"] = macd_bootstrap_comparison(
                oos_returns[arm.name], oos_returns[predecessor.name]
            )
        if (
            position
            and _oos_gates(segment_stats["oos"][arm.name])
            and _oos_gates(segment_stats["oos"][MACD_ARMS[0].name])
        ):
            comparisons["vs_default"] = macd_bootstrap_comparison(
                oos_returns[arm.name], oos_returns[MACD_ARMS[0].name]
            )
        verdict = macd_arm_verdict(
            segment_stats["oos"][arm.name],
            comparisons.get("vs_predecessor"),
            segment_stats["oos"][predecessor.name] if predecessor else None,
            is_default_arm=predecessor is None,
        )
        filtered_counts = {"is": {}, "oos": {}}
        for day, reason in all_filtered[arm.name]:
            segment = "oos" if day >= request.oos_start else "is"
            filtered_counts[segment][reason] = filtered_counts[segment].get(reason, 0) + 1
        arms_payload[arm.name] = {
            "spec": {"stage": arm.stage, "params": arm.params()},
            "filtered_events": filtered_counts,
            "comparisons": comparisons,
            "verdict": verdict,
        }
    increments = [
        {
            "from": source,
            "to": target,
            "increment": label,
            "oos_comparison": arms_payload[target]["comparisons"].get("vs_predecessor"),
            "verdict": arms_payload[target]["verdict"]["verdict"],
            "reasons": arms_payload[target]["verdict"]["reasons"],
        }
        for source, target, label in MACD_INCREMENT_STEPS
    ]
    coverage = {
        "symbols": len(symbols),
        "censored_symbols": len(censored),
        "first_market_date": calendar[0] if calendar else None,
        "last_market_date": calendar[-1] if calendar else None,
    }
    return {
        "schema": ARMS_SCHEMA,
        "status": "ok",
        "unavailable_reasons": [],
        "request": request.model_dump(mode="json"),
        "provenance": {
            "pinned_reader": {"generation": generation, "manifest_sha256": manifest},
            "factor_code": {
                "arms": [
                    {"name": arm.name, "stage": arm.stage, "params": arm.params()}
                    for arm in MACD_ARMS
                ],
                "warmup_bars": ARMS_WARMUP_BARS,
                "price_scale": "adjusted_close",
                "execution": "signal_close_confirmed_next_available_session_close",
                "segment_attribution": "entry_execution_date",
                "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
                "breakout_window": BREAKOUT_WINDOW,
                "pullback_window": PULLBACK_WINDOW,
                "zero_tolerance": "rolling_dif_sample_std",
                "no_lookahead": "recursive_emas_and_trailing_windows_only",
            },
        },
        "segments": {
            "is": {"coverage": coverage, "arms": segment_stats["is"]},
            "oos": {"coverage": coverage, "arms": segment_stats["oos"]},
        },
        "arms": arms_payload,
        "increments": increments,
        "censored": sorted(censored, key=lambda item: (item["symbol"], item["code"])),
    }


__all__ = [
    "ARMS_SCHEMA",
    "ARMS_WARMUP_BARS",
    "MACD_ARMS",
    "MACD_INCREMENT_STEPS",
    "MACD_PARAMS",
    "MIN_OOS_SYMBOLS",
    "MIN_OOS_TRADES",
    "PINNED_READER_ATTR",
    "ROUND_TRIP_COST_BPS",
    "SCHEMA",
    "STATE_VALUES",
    "WARMUP_BARS",
    "GenerationPinnedDailyReader",
    "MacdArmSpec",
    "MacdArmsRequest",
    "MacdStagesAvailability",
    "MacdStagesRequest",
    "classify_stage",
    "evaluate_macd_arms",
    "evaluate_macd_stages",
    "macd_arm_verdict",
    "macd_bootstrap_comparison",
    "macd_stages_availability",
    "resolve_pinned_reader",
    "zero_side",
]
