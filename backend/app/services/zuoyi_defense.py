"""Auditable daily Zuoyi defense overlay research (v3 contract).

Implements docs/ISSUE-29/final-design.md: single immutable canonical +
markets-source pin, one entry predicate (uptrend false->true at T close,
T+1 open), six arms over a common 60 market-day horizon aligned by entry_id,
per-segment statistics (no portfolio NAV), censoring, provenance and an
OOS-only verdict. Research only; never a trading system.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any, Callable

ARMS = ("buy_hold", "atr_chandelier_k3", "ma20_hold", "ma60_hold", "zuoyi_defense", "zuoyi_atr_combo")
BASELINES = ("buy_hold", "atr_chandelier_k3", "ma20_hold", "ma60_hold")
CENSOR_CODES = (
    "CENSOR_WARMUP_INSUFFICIENT", "CENSOR_MISSING_BAR", "CENSOR_INVALID_OPEN",
    "CENSOR_SUSPENDED", "CENSOR_BUY_LIMIT_UP", "CENSOR_HORIZON_INCOMPLETE",
    "CENSOR_PENDING_EXIT", "CENSOR_DIAGNOSTIC_HORIZON_INCOMPLETE",
)
UNAVAILABLE_CODES = (
    "UNAVAILABLE_READER", "UNAVAILABLE_CANONICAL_PIN", "UNAVAILABLE_MARKETS_PIN",
    "UNAVAILABLE_MARKETS_MANIFEST_MISMATCH", "UNAVAILABLE_REQUIRED_COLUMN",
    "UNAVAILABLE_INVALID_PROVENANCE",
)
REQUIRED_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close")
HORIZON_DAYS = 60
MIN_OOS_SEGMENTS = 20
BOOTSTRAP_ROUNDS = 500
PRICE_TOL = 0.01

ZUOYI_DEFINITION = {
    "definition_version": "v3",
    "arms": list(ARMS),
    "parameters": {"window": 3, "tie_break": "latest", "inclusion": "strict", "recovery": "no_cancel", "atr_k": 3},
    "horizon_days": HORIZON_DAYS,
    "min_oos_segments": MIN_OOS_SEGMENTS,
}


def _fv(fact: Any, key: str, default: Any = None) -> Any:
    if fact is None:
        return default
    if isinstance(fact, dict):
        return fact.get(key, default)
    return getattr(fact, key, default)


def _empty_stats() -> dict[str, Any]:
    return {"segment_count": 0, "complete_count": 0, "stats": []}


def _unavailable(code: str, reason: str, request: dict[str, Any], provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable", "definition_version": "v3", "code": code, "reasons": [reason],
        "request": request, "entry_ids": [], "events": [], "segments": [], "arms": [], "censored": [],
        "denominator_audit": [], "is": _empty_stats(), "oos": _empty_stats(),
        "diagnostics": {"sell_flee": [], "breakdown_depth": {"m": 5, "eligible_exits": 0, "censored_windows": 0, "mean_depth": None, "median_depth": None}},
        "verdict": {"value": "unavailable", "oos_complete_segments": 0, "minimum_required": 0, "rule": "unavailable"},
        "provenance": provenance,
    }


def assess_capability(reader: Any) -> dict[str, Any]:
    has = getattr(reader, "has_columns", None)
    missing = list(REQUIRED_COLUMNS) if reader is None or has is None else [c for c in REQUIRED_COLUMNS if not has(c)]
    return {
        "available": not missing,
        "status": "available" if not missing else "unavailable",
        "definition_version": "v3",
        "required_columns": list(REQUIRED_COLUMNS),
        "missing_columns": missing,
        "reasons": [] if not missing else ["missing_required_columns"],
    }


def _bars(reader: Any, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    frame = reader.daily_bars(symbol, start, end)
    if frame is None or getattr(frame, "is_empty", lambda: True)():
        return []
    return sorted(frame.to_dicts(), key=lambda r: r.get("date"))


def _window_line(rows: list[dict[str, Any]], idx: int) -> float | None:
    """Median-line = argmax(high) over 3 completed bars (tie -> latest);
    left-one = first bar to the left not strictly engulfed (equal points count
    as contained); defense line = left-one low. Search limit 10 bars."""
    if idx < 2:
        return None
    window = rows[idx - 2: idx + 1]
    highs = [float(r["high"]) for r in window if r.get("high") is not None]
    if not highs:
        return None
    peak = max(highs)
    mid_local = max(j for j, r in enumerate(window) if r.get("high") is not None and float(r["high"]) == peak)
    for j in range(idx - 2 + mid_local - 1, max(-1, idx - 2 + mid_local - 11), -1):
        cand, right = rows[j], rows[j + 1]
        ch, cl = cand.get("high"), cand.get("low")
        rh, rl = right.get("high"), right.get("low")
        if ch is None or cl is None or rh is None or rl is None:
            return None
        if not (float(ch) <= float(rh) and float(cl) >= float(rl)):
            return float(cl)
    return None


def _atr14(rows: list[dict[str, Any]]) -> list[float | None]:
    """Wilder EWM ATR14 over completed bars; None until 14 true ranges."""
    out: list[float | None] = []
    prev_close: float | None = None
    atr: float | None = None
    count = 0
    for r in rows:
        h, l, c = r.get("high"), r.get("low"), r.get("close")
        if h is None or l is None or c is None:
            out.append(None)
            continue
        tr = float(h) - float(l)
        if prev_close is not None:
            tr = max(tr, abs(float(h) - prev_close), abs(float(l) - prev_close))
        count += 1
        atr = tr if atr is None else (atr * 13 + tr) / 14
        out.append(atr if count >= 14 else None)
        prev_close = float(c)
    return out


def _summary(values: list[float], metric: str, censored: int = 0) -> dict[str, Any]:
    if not values:
        return {"metric": metric, "eligible_count": 0, "censored_count": censored, "mean": None, "median": None, "p05": None, "p95": None, "confidence_interval": None}
    vals = sorted(values)
    n = len(vals)
    return {
        "metric": metric, "eligible_count": n, "censored_count": censored,
        "mean": sum(vals) / n, "median": vals[n // 2],
        "p05": vals[max(0, int(n * 0.05) - 1)], "p95": vals[min(n - 1, int(n * 0.95))],
        "confidence_interval": None,
    }


def _stats_block(segments: list[dict[str, Any]], metrics: tuple[str, ...]) -> dict[str, Any]:
    complete = [s for s in segments if s["status"] == "complete"]
    censored = len(segments) - len(complete)
    stats = [_summary([s[m] for s in complete if s.get(m) is not None], m, censored) for m in metrics]
    return {"segment_count": len(segments), "complete_count": len(complete), "stats": stats}


def _sellable(fact: Any, row: dict[str, Any]) -> bool | None:
    """Return None for unknown facts and False only for proven sell blocks."""
    if fact is None:
        return None
    if _fv(fact, "suspended") is True:
        return False
    lower = _fv(fact, "published_limit_down")
    raw = [row.get(f"raw_{key}") for key in ("open", "high", "low", "close")]
    if _fv(fact, "signal_limit_down") is True and lower is not None and None not in raw:
        if all(abs(float(value) - float(lower)) <= PRICE_TOL for value in raw):
            return False
    return True


def _exit_for_line(
    line_fn: Callable[[int], float | None], rows: list[dict[str, Any]], horizon_idx: list[int],
    facts_get: Callable[[str, date], dict[str, Any] | None], symbol: str,
) -> tuple[str, float | None, int, str | None, dict[str, Any]]:
    """Walk horizon; breach starts after entry day and executes next sellable raw quote."""
    for j, idx in enumerate(horizon_idx[1:], 1):
        line = line_fn(idx)
        if line is None or rows[idx].get("close") is None or float(rows[idx]["close"]) >= float(line):
            continue
        nxt = horizon_idx[j + 1] if j + 1 < len(horizon_idx) else None
        while nxt is not None:
            row = rows[nxt]
            sellable = _sellable(facts_get(symbol, row["date"]), row)
            if sellable is False:
                nxt = horizon_idx[horizon_idx.index(nxt) + 1] if horizon_idx.index(nxt) + 1 < len(horizon_idx) else None
                continue
            if sellable is None:
                return "censored", None, 0, "CENSOR_PENDING_EXIT", {"breach_day": str(rows[idx]["date"]), "reason": "markets fact missing for exit day"}
            if row.get("open") is None or float(row["open"]) <= 0:
                return "censored", None, 0, "CENSOR_PENDING_EXIT", {"breach_day": str(rows[idx]["date"])}
            return "terminal_exit", float(row["open"]), horizon_idx.index(nxt) + 1, None, {"breach_day": str(rows[idx]["date"]), "exec_day": str(row["date"])}
        return "censored", None, 0, "CENSOR_PENDING_EXIT", {"breach_day": str(rows[idx]["date"]), "reason": "no sellable day within horizon"}
    last = rows[horizon_idx[-1]]
    return "horizon_close", float(last["close"]), len(horizon_idx), None, {}


def _paired_verdict(paired: dict[str, tuple[float, float | None]], complete_count: int) -> dict[str, Any]:
    """Apply the frozen strongest-baseline gate without post-hoc arm selection."""
    binding_arm, (binding_mean, binding_low) = min(
        paired.items(), key=lambda item: item[1][0]
    )
    if binding_mean > 0 and binding_low is not None and binding_low > 0:
        value = "accepted"
        rule = f"paired bootstrap seed=42 rounds={BOOTSTRAP_ROUNDS} vs strongest baseline {binding_arm} (all preregistered baselines sampled)"
    else:
        value = "rejected"
        rule = f"paired bootstrap seed=42 rounds={BOOTSTRAP_ROUNDS} shows no stable increment vs strongest baseline {binding_arm}"
    return {"value": value, "oos_complete_segments": complete_count, "minimum_required": MIN_OOS_SEGMENTS, "rule": rule}


def evaluate_zuoyi_defense(
    reader: Any, *, start: date, end: date, symbols: list[str], oos_start: date,
    cost_bps: float = 10.0, market_facts_reader: Any = None,
) -> dict[str, Any]:
    request = {"symbols": list(dict.fromkeys(symbols)), "start": start, "end": end, "oos_start": oos_start, "cost_bps": cost_bps}
    if start >= oos_start or oos_start > end:
        raise ValueError("start < oos_start <= end required")
    cap = assess_capability(reader)
    if not cap["available"]:
        return _unavailable("UNAVAILABLE_REQUIRED_COLUMN", ",".join(cap["missing_columns"]), request)
    manifest = reader.manifest() if hasattr(reader, "manifest") else {}
    sources = manifest.get("source_generations") if isinstance(manifest, dict) else None
    market_generation = sources.get("markets") if isinstance(sources, dict) else None
    if isinstance(market_generation, dict):
        market_generation = market_generation.get("generation")
    if market_facts_reader is None or not market_generation:
        return _unavailable("UNAVAILABLE_MARKETS_PIN", "immutable markets pin unavailable", request)
    identity_fn = getattr(market_facts_reader, "pin_identity_verified", None)
    identity = identity_fn() if identity_fn is not None else True
    generation_fn = getattr(market_facts_reader, "generation", None)
    generation_ok = generation_fn is None or generation_fn() == market_generation
    if not identity or not generation_ok:
        return _unavailable("UNAVAILABLE_MARKETS_MANIFEST_MISMATCH", "markets pin identity mismatch", request)
    facts_get: Callable[[str, date], dict[str, Any] | None] = getattr(market_facts_reader, "get", lambda *_: None)
    events: list[dict[str, Any]] = []
    entry_ids: list[str] = []
    censored: list[dict[str, Any]] = []
    segments_by_arm: dict[str, list[dict[str, Any]]] = {a: [] for a in ARMS}
    all_rows: dict[str, list[dict[str, Any]]] = {}
    metrics = ("net_return", "mae", "mfe", "holding_days")

    def add_censor(code: str, symbol: str | None, entry_id: str | None, signal_date: date | None, arm: str | None, detail: str) -> None:
        censored.append({"code": code, "symbol": symbol, "entry_id": entry_id, "signal_date": signal_date, "arm": arm, "detail": detail})

    for symbol in request["symbols"]:
        calendar_days = list(reader.market_days(start - timedelta(days=180), end)) if hasattr(reader, "market_days") else []
        warmup_days = [day for day in calendar_days if day < start]
        load_start = warmup_days[-59] if len(warmup_days) >= 59 else start
        rows = _bars(reader, symbol, load_start, end)
        calendar_pos = {day: pos for pos, day in enumerate(calendar_days)}
        row_by_date = {row["date"]: pos for pos, row in enumerate(rows)}
        all_rows[symbol] = rows
        missing_market_days = [r["date"] for r in rows if start <= r["date"] <= end and facts_get(symbol, r["date"]) is None]
        if missing_market_days:
            return _unavailable(
                "UNAVAILABLE_MARKETS_PIN",
                f"{symbol} missing pinned markets facts on {len(missing_market_days)} days",
                request,
            )
        if len(warmup_days) < 59 or len(rows) < 60:
            add_censor("CENSOR_WARMUP_INSUFFICIENT", symbol, None, None, None, f"only {len(warmup_days)} pre-start market days or {len(rows)} completed bars")
            continue
        closes = [r.get("close") for r in rows]
        ma20 = [sum(float(x) for x in closes[max(0, i - 19): i + 1] if x is not None) / min(i + 1, 20) for i in range(len(rows))]
        ma60 = [sum(float(x) for x in closes[max(0, i - 59): i + 1] if x is not None) / min(i + 1, 60) for i in range(len(rows))]
        up = [closes[i] is not None and i >= 59 and float(closes[i]) > ma60[i] and ma20[i] >= ma60[i] for i in range(len(rows))]
        atr = _atr14(rows)
        blocked_until: date | None = None
        for i in range(1, len(rows) - 1):
            signal_day = rows[i]["date"]
            if signal_day < start or signal_day > end:
                continue
            if signal_day not in calendar_pos:
                continue
            if blocked_until is not None and signal_day <= blocked_until:
                continue
            if not (up[i] and not up[i - 1]):
                continue
            pos = calendar_pos[signal_day] + 1
            entry_day = calendar_days[pos] if pos < len(calendar_days) else None
            entry_idx = row_by_date.get(entry_day) if entry_day is not None else None
            entry_row = rows[entry_idx] if entry_idx is not None else None
            eid = f"{symbol}:{signal_day}"
            if entry_day is None:
                # Calendar is bounded at ``end``: a signal whose T+1 falls
                # beyond the window cannot be observed, so the entry is
                # censored as horizon-incomplete (never executed, no hold).
                entry_ids.append(eid)
                events.append({"entry_id": eid, "symbol": symbol, "signal_date": signal_day, "entry_date": None, "status": "censored", "censor_code": "CENSOR_HORIZON_INCOMPLETE", "research_entry_value_adj": None, "quote_entry_open_raw": None, "evidence": {}})
                add_censor("CENSOR_HORIZON_INCOMPLETE", symbol, eid, signal_day, None, "T+1 entry day beyond request end")
                continue
            entry_ids.append(eid)
            fact = facts_get(symbol, entry_day)
            raw_open = entry_row.get("raw_open") if entry_row else None
            raw_high = entry_row.get("raw_high") if entry_row else None
            raw_low = entry_row.get("raw_low") if entry_row else None
            raw_close = entry_row.get("raw_close") if entry_row else None
            upper = _fv(fact, "published_limit_up") if fact else None
            one_price_up = (
                _fv(fact, "signal_limit_up") is True
                and upper is not None and None not in (raw_open, raw_high, raw_low, raw_close)
                and all(abs(float(value) - float(upper)) <= PRICE_TOL for value in (raw_open, raw_high, raw_low, raw_close))
            )
            if fact is None:
                code = "CENSOR_MISSING_BAR"
            elif _fv(fact, "suspended") is True:
                code = "CENSOR_SUSPENDED"
            elif raw_open is None or float(raw_open) <= 0:
                code = "CENSOR_INVALID_OPEN"
            elif one_price_up:
                code = "CENSOR_BUY_LIMIT_UP"
            else:
                code = None
            if code is not None:
                events.append({"entry_id": eid, "symbol": symbol, "signal_date": signal_day, "entry_date": entry_day, "status": "censored", "censor_code": code, "research_entry_value_adj": None, "quote_entry_open_raw": float(raw_open) if raw_open is not None else None, "evidence": {"published_limit_up": upper}})
                add_censor(code, symbol, eid, entry_day, None, f"entry not executable: {code}")
                continue
            entry_value = float(entry_row["open"])
            events.append({"entry_id": eid, "symbol": symbol, "signal_date": signal_day, "entry_date": entry_day, "status": "entry_executed", "censor_code": None, "research_entry_value_adj": entry_value, "quote_entry_open_raw": float(raw_open), "evidence": {}})
            horizon_days = calendar_days[pos:pos + HORIZON_DAYS]
            horizon_idx = [row_by_date[d] for d in horizon_days if d in row_by_date]
            if len(horizon_days) < HORIZON_DAYS or len(horizon_idx) < HORIZON_DAYS:
                for arm in ARMS:
                    segments_by_arm[arm].append({"entry_id": eid, "symbol": symbol, "exit_date": None, "status": "censored", "exit_kind": None, "censor_code": "CENSOR_HORIZON_INCOMPLETE", "net_return": None, "mae": None, "mfe": None, "holding_days": None, "exit_research_value_adj": None, "exit_quote_open_raw": None, "evidence": {"horizon_days_available": len(horizon_idx)}})
                    add_censor("CENSOR_HORIZON_INCOMPLETE", symbol, eid, entry_day, arm, "common 60-day horizon exceeds available completed bars")
                blocked_until = horizon_days[-1] if horizon_days else end
                continue
            def atr_line_at(idx: int) -> float | None:
                atr_value = atr[idx]
                rolling_max = max(
                    (float(r["high"]) for r in rows[i + 1: idx + 1] if r.get("high") is not None),
                    default=None,
                )
                return None if atr_value is None or rolling_max is None else rolling_max - 3 * float(atr_value)
            for arm, line_fn in (
                ("buy_hold", lambda idx: None),
                ("atr_chandelier_k3", atr_line_at),
                ("ma20_hold", lambda idx: ma20[idx]),
                ("ma60_hold", lambda idx: ma60[idx]),
            ):
                kind, exit_value, days, censor, ev = _exit_for_line(line_fn, rows, horizon_idx, facts_get, symbol)
                segments_by_arm[arm].append(_seg(eid, symbol, rows, horizon_idx, kind, exit_value, days, censor, ev, entry_value, cost_bps))
            # Re-walk zuoyi arms deterministically from horizon start.
            for arm in ("zuoyi_defense", "zuoyi_atr_combo"):
                segs = segments_by_arm[arm]
                def zuoyi_line_fn(idx: int, _arm: str = arm) -> float | None:
                    if _arm == "zuoyi_defense":
                        return _replay_zuoyi_line(rows, i, idx, up, list(closes))
                    combo = _replay_zuoyi_line(rows, i, idx, up, list(closes))
                    a = atr[idx]
                    hi = max((float(r["high"]) for r in rows[i + 1: idx + 1] if r.get("high") is not None), default=None)
                    atr_line = None if a is None or hi is None else hi - 3 * float(a)
                    return atr_line if combo is None else combo if atr_line is None else max(combo, atr_line)
                kind, exit_value, days, censor, ev = _exit_for_line(zuoyi_line_fn, rows, horizon_idx, facts_get, symbol)
                segs.append(_seg(eid, symbol, rows, horizon_idx, kind, exit_value, days, censor, ev, entry_value, cost_bps))
            blocked_until = rows[horizon_idx[-1]]["date"]
    arms = [{"arm": arm, "segments": segments_by_arm[arm]} for arm in ARMS]
    pinned_calendar = list(reader.market_days(start, end)) if hasattr(reader, "market_days") else []
    is_segs = [s for a in ARMS for s in segments_by_arm[a] if _signal_date(s) is not None and _signal_date(s) < oos_start]
    oos_segs = [s for a in ARMS for s in segments_by_arm[a] if _signal_date(s) is not None and _signal_date(s) >= oos_start]
    diagnostics = _diagnostics(segments_by_arm["zuoyi_defense"], all_rows)
    denominator_audit = _denominator_audit(segments_by_arm, metrics, diagnostics)
    prov = {
        "definition_version": "v3", "canonical_generation": reader.generation(),
        "canonical_manifest_sha256": reader.manifest_sha256(),
        "markets_generation": market_generation,
        "markets_manifest_sha256": getattr(market_facts_reader, "pin_manifest_sha256", getattr(market_facts_reader, "manifest_sha256", lambda: ""))(),
        "markets_pin_verification_mode": getattr(market_facts_reader, "pin_verification_mode", lambda: "legacy")(),
        "required_columns": list(REQUIRED_COLUMNS), "market_days": len(pinned_calendar),
        "window": 3, "tie_break": "latest", "inclusion": "strict", "recovery": "no_cancel",
        "atr_k": 3, "cost_bps": cost_bps, "horizon_days": HORIZON_DAYS,
        "diagnostics_horizons": {"sell_flee": [5, 10, 20], "breakdown_depth": 5},
    }
    zuoyi_map = {s["entry_id"]: s["net_return"] for s in segments_by_arm["zuoyi_defense"] if s["status"] == "complete" and _signal_date(s) >= oos_start}
    paired: dict[str, tuple[float, float | None]] = {}
    for arm in BASELINES:
        base_map = {s["entry_id"]: s["net_return"] for s in segments_by_arm[arm] if s["status"] == "complete" and _signal_date(s) >= oos_start}
        pairs = [zuoyi_map[eid] - base_map[eid] for eid in zuoyi_map.keys() & base_map.keys()]
        if len(pairs) >= MIN_OOS_SEGMENTS:
            paired[arm] = (sum(pairs) / len(pairs), _bootstrap_low(pairs))
    if len(zuoyi_map) < MIN_OOS_SEGMENTS or len(paired) < len(BASELINES):
        return _unavailable(
            "UNAVAILABLE_INVALID_PROVENANCE",
            f"insufficient paired OOS complete segments: zuoyi {len(zuoyi_map)} < {MIN_OOS_SEGMENTS} or baselines paired {sorted(paired)} != {sorted(BASELINES)}",
            request, prov,
        )
    verdict = _paired_verdict(paired, len(zuoyi_map))
    return {
        "status": "ok", "definition_version": "v3", "request": request,
        "parameters": {"window": 3, "tie_break": "latest", "inclusion": "strict", "recovery": "no_cancel", "atr_k": 3},
        "entry_ids": entry_ids, "events": events,
        "segments": [s for a in ARMS for s in segments_by_arm[a]],
        "arms": arms, "censored": censored, "denominator_audit": denominator_audit,
        "is": _stats_block(is_segs, metrics), "oos": _stats_block(oos_segs, metrics),
        "diagnostics": diagnostics, "verdict": verdict, "provenance": prov,
    }


def _signal_date(segment: dict[str, Any]) -> date | None:
    eid = segment.get("entry_id")
    if not eid or ":" not in eid:
        return None
    try:
        return date.fromisoformat(eid.rsplit(":", 1)[1])
    except ValueError:
        return None





def _bootstrap_low(diffs: list[float]) -> float | None:
    """Deterministic paired-difference bootstrap percentile lower bound."""
    if not diffs:
        return None
    rng = random.Random(42)
    samples = [sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(BOOTSTRAP_ROUNDS)]
    samples.sort()
    return samples[max(0, int(0.05 * len(samples)) - 1)]
def _seg(eid, symbol, rows, horizon_idx, kind, exit_value, days, censor, ev, entry_value, cost_bps=0.0) -> dict[str, Any]:
    if kind == "censored":
        return {"entry_id": eid, "symbol": symbol, "exit_date": None, "status": "censored", "exit_kind": None, "censor_code": censor, "net_return": None, "mae": None, "mfe": None, "holding_days": None, "exit_research_value_adj": None, "exit_quote_open_raw": None, "evidence": ev}
    last = rows[horizon_idx[-1]]
    exit_row = rows[horizon_idx[days - 1]] if kind == "terminal_exit" else last
    net = exit_value / entry_value - 1 - (2 * float(cost_bps) / 10000)
    holding_rows = rows[horizon_idx[0]: horizon_idx[days - 1] + 1]
    lows = [float(r["low"]) for r in holding_rows if r.get("low") is not None]
    highs = [float(r["high"]) for r in holding_rows if r.get("high") is not None]
    evidence = dict(ev)
    evidence.setdefault("entry_date", rows[horizon_idx[0]]["date"])
    return {
        "entry_id": eid, "symbol": symbol, "exit_date": exit_row["date"], "status": "complete",
        "exit_kind": kind, "censor_code": None, "net_return": net,
        "mae": (min(lows) / entry_value - 1) if lows else None,
        "mfe": (max(highs) / entry_value - 1) if highs else None,
        "holding_days": days, "exit_research_value_adj": exit_value,
        "exit_quote_open_raw": exit_row.get("raw_open"), "evidence": evidence,
    }


def _replay_zuoyi_line(rows, signal_idx, current_idx, up, closes) -> float | None:
    """Replay the zuoyi line walk from entry to current day without mutation:
    new-high recalcs, uptrend_lost freeze and recovery."""
    line = _window_line(rows, signal_idx)
    lost = False
    peak = float(closes[signal_idx + 1]) if closes[signal_idx + 1] is not None else None
    for idx in range(signal_idx + 2, current_idx + 1):
        if lost:
            if up[idx] and closes[idx] is not None and (peak is None or float(closes[idx]) > peak):
                lost = False
                new_line = _window_line(rows, idx)
                if new_line is not None:
                    line = new_line if line is None else max(line, new_line)
        else:
            if not up[idx]:
                lost = True
            elif closes[idx] is not None and (peak is None or float(closes[idx]) > peak):
                new_line = _window_line(rows, idx)
                if new_line is not None:
                    line = new_line if line is None else max(line, new_line)
        if closes[idx] is not None:
            peak = float(closes[idx]) if peak is None else max(peak, float(closes[idx]))
    return line


def _diagnostics(zuoyi_segments: list[dict[str, Any]], all_rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    exits = [s for s in zuoyi_segments if s["status"] == "complete" and s["exit_kind"] == "terminal_exit"]
    flee = []
    for n in (5, 10, 20):
        eligible = sold = cens = 0
        for s in exits:
            rows = all_rows.get(s["symbol"], [])
            tail = [r for r in rows if r["date"] > s["exit_date"]][:n]
            if len(tail) < n:
                cens += 1
                continue
            entry_day = s.get("evidence", {}).get("entry_date") or _signal_date(s)
            prior = [r for r in rows if entry_day <= r["date"] <= s["exit_date"]]
            peak = max(float(r["close"]) for r in prior if r.get("close") is not None)
            eligible += 1
            sold += int(max(float(r["close"]) for r in tail if r.get("close") is not None) > peak)
        flee.append({"n": n, "sold_flee_count": sold, "eligible_exits": eligible, "censored_windows": cens, "rate": sold / eligible if eligible else None})
    depths: list[float] = []
    d_cens = 0
    for s in exits:
        rows = all_rows.get(s["symbol"], [])
        tail = [r for r in rows if r["date"] > s["exit_date"]][:5]
        if len(tail) < 5 or s["exit_research_value_adj"] in (None, 0):
            d_cens += 1
            continue
        depths.append(min(float(r["close"]) for r in tail if r.get("close") is not None) / s["exit_research_value_adj"] - 1)
    breakdown = {"m": 5, "eligible_exits": len(depths), "censored_windows": d_cens, "mean_depth": sum(depths) / len(depths) if depths else None, "median_depth": sorted(depths)[len(depths) // 2] if depths else None}
    return {"sell_flee": flee, "breakdown_depth": breakdown}


def _denominator_audit(
    segments_by_arm: dict[str, list[dict[str, Any]]],
    metrics: tuple[str, ...],
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    audit = []
    zuoyi = segments_by_arm["zuoyi_defense"]
    for metric in metrics:
        complete = [s for s in zuoyi if s["status"] == "complete" and s.get(metric) is not None]
        codes: dict[str, int] = {}
        for s in zuoyi:
            if s["status"] != "complete":
                code = s.get("censor_code") or "CENSOR_PENDING_EXIT"
                codes[code] = codes.get(code, 0) + 1
        audit.append({"metric": metric, "eligible_count": len(complete), "censored_count": len(zuoyi) - len(complete), "excluded_count": 0, "codes": codes})
    for item in diagnostics.get("sell_flee", []):
        audit.append({"metric": f"sell_flee_n{item['n']}", "eligible_count": item["eligible_exits"], "censored_count": item["censored_windows"], "excluded_count": 0, "codes": {}})
    depth = diagnostics.get("breakdown_depth", {})
    audit.append({"metric": "breakdown_depth_m5", "eligible_count": depth.get("eligible_exits", 0), "censored_count": depth.get("censored_windows", 0), "excluded_count": 0, "codes": {}})
    return audit
