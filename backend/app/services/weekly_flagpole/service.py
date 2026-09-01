"""Public injected evaluation and capability entries (API-free)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from .benchmark import EqualWeightBenchmark, IndexBenchmarkLeg
from .detector import detect_symbol_events
from .evaluation import build_research_layer
from .models import (
    FORWARD_HORIZONS,
    POLE_WEEKS_MAX,
    POLE_WEEKS_MIN,
    THETA1_GRID,
    THETA2_GRID,
    WeeklyFlagpoleCapabilities,
    WeeklyFlagpoleFactor,
    WeeklyFlagpoleRequest,
    WeeklyFlagpoleResponse,
    valid_provenance,
    validate_payload,
)
from .weekly import aggregate_weekly_bars, bars_to_dicts

REQUIRED_READER_METHODS = (
    "generation",
    "manifest_sha256",
    "provider_id",
    "source_provenance",
    "market_days",
    "universe",
    "daily_bars",
    "limit_regime_facts",
)


def resolve_reader(repo: Any) -> Any | None:
    return getattr(repo, "n_shape_research_reader", None)


def assess_capability(reader: Any | None) -> WeeklyFlagpoleCapabilities:
    if reader is None:
        return WeeklyFlagpoleCapabilities(problems=["weekly_flagpole_research_reader_missing"])
    missing = [n for n in REQUIRED_READER_METHODS if not callable(getattr(reader, n, None))]
    if missing:
        return WeeklyFlagpoleCapabilities(
            reader_available=True,
            provenance_valid=False,
            problems=[f"reader_method_missing:{n}" for n in missing],
        )
    valid = valid_provenance(reader.source_provenance())
    return WeeklyFlagpoleCapabilities(
        reader_available=True,
        methods_complete=True,
        provenance_valid=valid,
        problems=[] if valid else ["pit_source_provenance_invalid"],
    )


def _unavailable(request, caps, reasons):
    return WeeklyFlagpoleResponse(
        factor=WeeklyFlagpoleFactor(),
        status="unavailable",
        unavailable_reasons=reasons,
        request=request,
        capabilities=caps,
        parameters={},
        provenance={},
        coverage=None,
        events=[],
        censored=[],
        research=None,
        diagnostics={},
        note="sealed composite reader is required; no fallback source is used",
    )


def _canonical_closes(rows, symbol):
    series = {}
    for row in rows:
        day = row.get("date")
        value = row.get("close")
        if not isinstance(day, date) or value is None:
            continue
        try:
            close = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(close):
            series[day] = close
    if not series:
        return {}, {
            "symbol": symbol,
            "code": "censor_canonical_close_missing",
            "detail": {"rows": len(rows)},
        }
    return series, None


MARKET_INDEX_CODE = "000300"


def _build_index_leg(index_reader: Any | None, calendar: list[date]):
    if index_reader is None:
        return None, "index_reader_missing"
    read = getattr(index_reader, "read_index_daily", None)
    if not callable(read):
        return None, "index_reader_missing"
    try:
        panel = read({"codes": [MARKET_INDEX_CODE], "start": calendar[0], "end": calendar[-1]})
        leg = next(
            (
                item
                for item in getattr(panel, "legs", [])
                if item.code == MARKET_INDEX_CODE and item.status == "ok"
            ),
            None,
        )
        if leg is None:
            unavailable = next(
                (
                    item
                    for item in getattr(panel, "legs", [])
                    if item.code == MARKET_INDEX_CODE
                ),
                None,
            )
            return None, getattr(unavailable, "reason_code", None) or "index_layer_not_sealed"
        pin = panel.pin.model_dump() if hasattr(panel.pin, "model_dump") else dict(panel.pin)
        closes = {bar.date: bar.close for bar in leg.bars}
        if not closes:
            return None, "index_layer_not_sealed"
        return IndexBenchmarkLeg(MARKET_INDEX_CODE, closes, calendar, pin), None
    except Exception:
        return None, "index_layer_not_sealed"


def evaluate(request: WeeklyFlagpoleRequest, reader: Any | None, index_reader: Any | None = None) -> WeeklyFlagpoleResponse:
    caps = assess_capability(reader)
    if reader is None or not caps.methods_complete or not caps.provenance_valid:
        return _unavailable(request, caps, caps.problems or ["reader_unavailable"])
    warm_start = request.start - timedelta(days=400)
    calendar = sorted(reader.market_days(warm_start, request.end + timedelta(days=14)))
    if not calendar:
        return _unavailable(request, caps, ["market_calendar_insufficient"])
    symbols = sorted(set(request.symbols or reader.universe(request.start, request.end)))
    events = []
    censored = []
    diagnostics = {"poles": 0, "failures": 0, "re_established": 0, "failure_records": []}
    panel = {}
    weeks_total = complete_weeks = incomplete_weeks = 0
    for symbol in symbols:
        frame = reader.daily_bars(symbol, warm_start, request.end)
        rows_raw = frame.to_dicts() if hasattr(frame, "to_dicts") else list(frame or [])
        rows, error = bars_to_dicts(rows_raw, symbol)
        if error:
            censored.append(error)
            continue
        adjusted, close_error = _canonical_closes(rows, symbol)
        if close_error:
            censored.append(close_error)
        else:
            panel[symbol] = adjusted
        facts = reader.limit_regime_facts(symbol, warm_start, request.end)
        weekly = aggregate_weekly_bars(
            symbol=symbol, rows=rows, market_days=calendar, window_end=request.end
        )
        weeks_total += len(weekly)
        complete_weeks += sum(b.complete for b in weekly)
        incomplete_weeks += sum(not b.complete for b in weekly)
        found, cut, diag = detect_symbol_events(
            symbol=symbol,
            weekly_bars=weekly,
            rows=rows,
            calendar=calendar,
            regime_facts=facts,
            event_start=request.start,
            event_end=request.end,
        )
        events.extend(found)
        censored.extend(cut)
        for key in ("poles", "failures", "re_established"):
            diagnostics[key] += int(diag.get(key, 0))
        diagnostics["failure_records"].extend(diag.get("failure_records", []))
    diagnostics["re_establishment_rate"] = (
        diagnostics["re_established"] / diagnostics["failures"] if diagnostics["failures"] else None
    )
    index_leg, index_reason = _build_index_leg(index_reader, calendar)
    diagnostics["market_index_reason"] = index_reason
    benchmark = EqualWeightBenchmark(panel, calendar)
    research = build_research_layer(
        events,
        calendar,
        benchmark,
        oos_start=request.oos_start,
        cost_bps=request.cost_bps,
        diagnostics=diagnostics,
        source_provenance=reader.source_provenance(),
        index_benchmark=index_leg,
    )
    coverage = {
        "symbols_total": len(symbols),
        "evaluated": len(panel),
        "events": len(events),
        "censored": len(censored),
        "weeks_total": weeks_total,
        "complete_weeks": complete_weeks,
        "incomplete_weeks": incomplete_weeks,
    }
    provenance = {
        "reader": {
            "generation": reader.generation(),
            "manifest_sha256": str(reader.manifest_sha256()).lower(),
            "provider_id": reader.provider_id(),
        },
        "sources": reader.source_provenance(),
        "pattern_price_scale": "raw",
        "return_price_scale": "canonical_adjusted",
        "benchmark_source": "sealed_universe_equal_weight",
    }
    response = WeeklyFlagpoleResponse(
        factor=WeeklyFlagpoleFactor(),
        status="ok",
        unavailable_reasons=[],
        request=request,
        capabilities=caps,
        parameters={
            "theta1_grid": list(THETA1_GRID),
            "theta2_grid": list(THETA2_GRID),
            "pole_weeks": [POLE_WEEKS_MIN, POLE_WEEKS_MAX],
            "horizons": list(FORWARD_HORIZONS),
        },
        provenance=provenance,
        coverage=coverage,
        events=events,
        censored=censored,
        research=research,
        diagnostics=diagnostics,
        note="read-only sealed weekly research",
    )
    validate_payload(response.model_dump(mode="json"))
    return response


evaluate_weekly_flagpole = evaluate
