"""Weekly flagpole F1/F3/F4 detector, injected-data only."""

from __future__ import annotations

from datetime import date

from app.services.swing_zigzag import KIND_HIGH, KIND_LOW, confirmed_zigzag

from .entries import flag_low_retest, volume_shrink_restart, weekly_reclaim
from .models import (
    FLAG_MAX_WEEKS,
    FORWARD_HORIZONS,
    NEW_POLE_WINDOW_WEEKS,
    THETA2_GRID,
    WEEKLY_ZIGZAG_THRESHOLD,
    is_limit_up,
)


def _forward(rows, calendar, anchor):
    base = rows.get(anchor, {}).get("raw_close")
    out = {"base_raw_close": base}
    index = {d: i for i, d in enumerate(calendar)}
    idx = index.get(anchor)
    for h in FORWARD_HORIZONS:
        value = None
        if base is not None and idx is not None and idx + h < len(calendar):
            close = rows.get(calendar[idx + h], {}).get("raw_close")
            if close is not None:
                value = float(close) / float(base) - 1
        out[f"forward_{h}d_raw_return"] = value
    return out


def _strict_flag(rows, facts, first, last):
    missing = []
    hit = False
    for row in rows:
        day = row.get("date")
        if isinstance(day, date) and first <= day <= last:
            state = is_limit_up(facts, row)
            if state is None:
                missing.append(day)
            elif state:
                hit = True
    return (None if missing else hit), missing


def detect_symbol_events(
    *, symbol, weekly_bars, rows, calendar, regime_facts, event_start, event_end
):
    bars = [b for b in weekly_bars if b.complete]
    if len(bars) < 2:
        return [], [], {"poles": 0, "failures": 0, "re_established": 0, "failure_records": []}
    pivots = confirmed_zigzag(
        [b.high for b in bars], [b.low for b in bars], WEEKLY_ZIGZAG_THRESHOLD
    )
    lows = {p.index: p for p in pivots if p.kind == KIND_LOW}
    highs = {p.index: p for p in pivots if p.kind == KIND_HIGH}
    rows_by_date = {r["date"]: r for r in rows if isinstance(r.get("date"), date)}
    runs = []
    i = 0
    while i < len(bars):
        if bars[i].close <= bars[i].open:
            i += 1
            continue
        j = i
        while j + 1 < len(bars) and bars[j + 1].close > bars[j + 1].open:
            j += 1
        if 2 <= j - i + 1 <= 4:
            runs.append((i, j))
        i = j + 1
    events = []
    censored = []
    failures = []
    for start_idx, end_idx in runs:
        pole = bars[start_idx : end_idx + 1]
        gain = pole[-1].close / pole[0].open - 1
        pole_high = max(b.high for b in pole)
        if gain <= 0:
            continue
        low_pivot = lows.get(start_idx) or lows.get(start_idx - 1)
        high_pivot = next(
            (p for idx, p in highs.items() if start_idx <= idx <= end_idx and p.price == pole_high),
            None,
        )
        flag = []
        flag_low = None
        for k in range(end_idx + 1, min(len(bars), end_idx + 1 + FLAG_MAX_WEEKS + 1)):
            current = bars[k]
            if flag_low is not None and current.close < flag_low:
                failures.append(
                    {
                        "symbol": symbol,
                        "pole_start": bars[start_idx].week_key,
                        "failure_week": current.week_key,
                        "failure_index": k,
                        "re_established": False,
                    }
                )
                break
            next_low = min(current.low, flag_low) if flag_low is not None else current.low
            if (pole_high - next_low) / pole_high > max(THETA2_GRID):
                break
            if flag and event_start <= current.week_key <= event_end:
                variants = []
                if weekly_reclaim(bars[k - 1], current, pole_high):
                    variants.append("weekly_reclaim")
                if volume_shrink_restart(pole, flag, current):
                    variants.append("volume_shrink_restart")
                if flag_low is not None and flag_low_retest(current, flag_low):
                    variants.append("flag_low_retest")
                if (
                    variants
                    and low_pivot is not None
                    and low_pivot.confirm_index <= k
                    and high_pivot is not None
                    and high_pivot.confirm_index <= k
                ):
                    strict, missing = _strict_flag(
                        rows, regime_facts, pole[0].first_day, pole[-1].last_day
                    )
                    if missing:
                        censored.append(
                            {
                                "symbol": symbol,
                                "code": "censor_limit_regime_fact_missing",
                                "detail": {"dates": [str(d) for d in missing]},
                            }
                        )
                    events.append(
                        {
                            "symbol": symbol,
                            "pole_start": pole[0].week_key,
                            "pole_end": pole[-1].week_key,
                            "pole_weeks": len(pole),
                            "pole_high": pole_high,
                            "pole_start_open": pole[0].open,
                            "cum_gain": gain,
                            "flag_low": flag_low,
                            "flag_depth": (pole_high - flag_low) / pole_high,
                            "confirm_week_key": current.week_key,
                            "confirm_date": current.last_day,
                            "confirm_week_close": current.close,
                            "variants": variants,
                            "strict_limit_up": strict,
                            "loose": True,
                            "structure": {
                                "swing_low_confirmed": True,
                                "swing_high_confirmed": True,
                                "zigzag_threshold": WEEKLY_ZIGZAG_THRESHOLD,
                            },
                            "forward": _forward(
                                rows_by_date,
                                {d for d in calendar} if False else calendar,
                                current.last_day,
                            ),
                        }
                    )
                    break
            flag.append(current)
            flag_low = next_low
    for failure in failures:
        fidx = failure["failure_index"]
        candidate = next(
            ((a, b) for a, b in runs if a > fidx and a - fidx <= NEW_POLE_WINDOW_WEEKS), None
        )
        if candidate is not None:
            failure["re_established"] = True
            failure["new_pole_start"] = bars[candidate[0]].week_key
    diagnostics = {
        "poles": len(runs),
        "failures": len(failures),
        "re_established": sum(1 for f in failures if f["re_established"]),
        "re_establishment_rate": (
            sum(1 for f in failures if f["re_established"]) / len(failures) if failures else None
        ),
        "failure_records": failures,
    }
    return events, censored, diagnostics
