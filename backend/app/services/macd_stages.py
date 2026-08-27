"""MACD(10/20/7) staged research with strict PIT boundaries."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Protocol

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

MACD_PARAMS = {"fast": 10, "slow": 20, "signal": 7}
SCHEMA = "tickflow.research.macd-stages.v1"
WARMUP_BARS = 26
STATE_VALUES = ("initial", "below_shrink", "below_expand", "cross_up", "above_expand", "above_shrink", "cross_down")
PINNED_READER_ATTR = "generation_pinned_daily_reader"
_READER_METHODS = ("generation", "manifest_sha256", "market_days", "daily_bars")
_SHA256 = frozenset("0123456789abcdef")


class GenerationPinnedDailyReader(Protocol):
    def generation(self) -> str: ...
    def manifest_sha256(self) -> str: ...
    def market_days(self, start: date, end: date) -> list[date]: ...
    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...


def _reader_ok(reader: Any) -> bool:
    return reader is not None and all(callable(getattr(reader, name, None)) for name in _READER_METHODS)


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
    return MacdStagesAvailability(SCHEMA, status, dict(MACD_PARAMS), reasons, {"daily_state_machine": False, "oos_evaluation": False, "pit_reader": not valid}, {"required_fields": ["raw", "pit", "generation", "available_from"], "state_values": list(STATE_VALUES)})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MacdStagesRequest(_StrictModel):
    start: date
    end: date
    symbols: list[str] | None = None
    oos_start: date

    @model_validator(mode="after")
    def validate_bounds(self) -> "MacdStagesRequest":
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if self.oos_start < self.start:
            raise ValueError("oos_start must be >= start")
        if self.symbols is not None and (not self.symbols or any(not isinstance(s, str) or not s.strip() for s in self.symbols)):
            raise ValueError("symbols must contain non-empty strings")
        return self


def classify_stage(prev: tuple[float, float, float] | None, cur: tuple[float, float, float]) -> str | None:
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
            return {}, {"symbol": symbol, "code": "raw_field_missing", "detail": {"fields": ["date" if day is None else "raw_close"]}}
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
            return {}, {"symbol": symbol, "code": "raw_field_invalid", "detail": {"fields": ["raw_close"], "date": str(day)}}
        output[day] = row
    return output, None


def _rows(symbol: str, bars: dict[date, dict[str, Any]], calendar: list[date], next_day: dict[date, date], request: MacdStagesRequest, generation: str, manifest: str) -> list[dict[str, Any]]:
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
            state = "initial" if seen == WARMUP_BARS else classify_stage(previous if prev_market_has_bar else None, current)
            result.append({"market_date": day, "symbol": symbol, "state": state, "zero_side": zero_side(dif), "available_from": next_day[day], "raw": {"snapshot_ref": f"sealed:{manifest}:{symbol}:{day.isoformat()}", "raw_close": close, "source_fields": ["raw_close"]}, "pit": {"as_of": f"{day.isoformat()}T23:59:59Z", "generation": generation}, "generation": generation, "macd": {"ema_fast": ef, "ema_slow": es, "dif": dif, "dea": dea, "hist": hist}})
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
    return {"rows": len(rows), "symbols": len({r["symbol"] for r in rows}), "first_market_date": rows[0]["market_date"] if rows else None, "last_market_date": rows[-1]["market_date"] if rows else None, "state_counts": counts, "state_missing_rows": missing}


def _unavailable(request: MacdStagesRequest, reasons: list[str]) -> dict[str, Any]:
    return {"schema": SCHEMA, "status": "unavailable", "unavailable_reasons": reasons, "request": request.model_dump(mode="json"), "provenance": {}, "segments": {"is": {"coverage": None, "rows": []}, "oos": {"coverage": None, "rows": []}}, "censored": []}


def evaluate_macd_stages(reader: Any, start: date, end: date, symbols: list[str] | None = None, oos_start: date | None = None) -> dict[str, Any]:
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
    return {"schema": SCHEMA, "status": "ok", "unavailable_reasons": [], "request": request.model_dump(mode="json"), "provenance": {"pinned_reader": {"generation": generation, "manifest_sha256": manifest}, "factor_code": {"params": dict(MACD_PARAMS), "warmup_bars": WARMUP_BARS, "ema_seed": "first_valid_close", "alpha": "2/(n+1)", "price_scale": "raw"}}, "segments": {"is": {"coverage": _coverage(is_rows), "rows": is_rows}, "oos": {"coverage": _coverage(oos_rows), "rows": oos_rows}}, "censored": sorted(censored, key=lambda item: (item["symbol"], item["code"]))}


__all__ = ["MACD_PARAMS", "SCHEMA", "WARMUP_BARS", "STATE_VALUES", "PINNED_READER_ATTR", "GenerationPinnedDailyReader", "MacdStagesAvailability", "MacdStagesRequest", "macd_stages_availability", "resolve_pinned_reader", "classify_stage", "zero_side", "evaluate_macd_stages"]
