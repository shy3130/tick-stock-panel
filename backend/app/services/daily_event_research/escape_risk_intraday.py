"""Pure S2-S7/S10 detectors over catalog-pinned intraday facts."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping, Sequence

from app.data_providers.fquant.escape_risk_intraday import (
    EscapeRiskIntradayBundle,
    IntradayDay,
    IntradayMinute,
)

from .escape_risk import (
    DETECTOR_ID_S10,
    DETECTOR_ID_S2,
    DETECTOR_ID_S3,
    DETECTOR_ID_S4,
    DETECTOR_ID_S5,
    DETECTOR_ID_S6,
    DETECTOR_ID_S7,
    INTRADAY_SIGNAL_VARIANT,
    EscapeCensorReason,
)
from .models import Detection, DetectionEvidence

TAIL_START_INDEX = 210  # 14:30
TAIL_DROP_MIN = 0.02
TAIL_SPEED_RATIO_MIN = 3.0
CLOSE_LOCATION_MAX = 0.20
VOLUME_HISTORY_DAYS = 5
VOLUME_RATIO_MIN = 2.0
STALL_CLOSE_CHANGE_MAX = 0.01
STALL_INTRADAY_HIGH_MIN = 0.03
HIGH_OPEN_MIN = 0.02
VWAP_BREAK_MINUTES = 5
EARLY_FIRST_WINDOW_END = 29  # 09:59
EARLY_SIGNAL_INDEX = 60  # 10:30
TURNOVER_RATIO_MIN = 2.0
PRICE_TOLERANCE = 0.005

_SIGNAL_IDS = ("s2", "s3", "s4", "s5", "s6", "s7", "s10")


@dataclass(frozen=True, slots=True)
class IntradayDetectionResult:
    detections: tuple[Detection, ...]
    censor_codes: Mapping[str, tuple[str, ...]]
    coverage: Mapping[str, int]


def _evidence(
    qualified: bool,
    day: IntradayDay,
    *,
    available: IntradayMinute,
    execution_session: str,
    execution_price: float,
    execution_reachable: bool,
    values: Mapping[str, object],
) -> DetectionEvidence:
    return DetectionEvidence(
        qualified=qualified,
        values={
            "available_date": day.trade_date.isoformat(),
            "available_at": available.timestamp.isoformat(),
            "existing_position_required": True,
            "execution_session": execution_session,
            "execution_price": execution_price,
            "execution_reachable": execution_reachable,
            **values,
        },
    )


def _detection(
    detector_id: str,
    day: IntradayDay,
    evidence: DetectionEvidence,
) -> Detection:
    return Detection(
        detector_id=detector_id,
        variant=INTRADAY_SIGNAL_VARIANT,
        symbol=day.symbol,
        signal_date=day.trade_date,
        evidence=evidence,
    )


def _total_volume(day: IntradayDay) -> int:
    return sum(minute.volume_shares for minute in day.minutes)


def _tail_dive(day: IntradayDay) -> Detection:
    bars = day.minutes
    anchor = bars[TAIL_START_INDEX]
    close_bar = bars[-1]
    pre_returns = [
        abs(bars[index].close / bars[index - 1].close - 1)
        for index in range(1, TAIL_START_INDEX + 1)
        if bars[index - 1].close > 0
    ]
    pre_speed = sum(pre_returns) / len(pre_returns) if pre_returns else 0.0
    tail_drop = max(0.0, anchor.close / close_bar.close - 1)
    tail_minutes = max(1, close_bar.minute_index - anchor.minute_index)
    tail_speed = tail_drop / tail_minutes
    speed_ratio = math.inf if pre_speed == 0 and tail_drop > 0 else (
        tail_speed / pre_speed if pre_speed > 0 else 0.0
    )
    day_high = max(bar.high for bar in bars)
    day_low = min(bar.low for bar in bars)
    close_location = (
        (close_bar.close - day_low) / (day_high - day_low)
        if day_high > day_low
        else 1.0
    )
    qualified = (
        tail_drop >= TAIL_DROP_MIN
        and speed_ratio >= TAIL_SPEED_RATIO_MIN
        and close_location <= CLOSE_LOCATION_MAX
    )
    return _detection(
        DETECTOR_ID_S2,
        day,
        _evidence(
            qualified,
            day,
            available=close_bar,
            execution_session="next_open",
            execution_price=close_bar.close,
            execution_reachable=True,
            values={
                "tail_start": anchor.timestamp.isoformat(),
                "tail_drop": tail_drop,
                "pre_tail_abs_speed": pre_speed,
                "tail_speed_ratio": speed_ratio,
                "close_location": close_location,
            },
        ),
    )


def _opened_limit_up(day: IntradayDay) -> Detection:
    threshold = day.published_limit_up - PRICE_TOLERANCE
    touched = [index for index, bar in enumerate(day.minutes) if bar.high >= threshold]
    qualified = bool(touched) and day.minutes[-1].close < threshold
    first_touch = touched[0] if touched else len(day.minutes) - 1
    open_indices: list[int] = []
    previous_sealed = False
    for index, bar in enumerate(day.minutes):
        touched_and_opened = bar.high >= threshold and bar.close < threshold
        broke_seal = previous_sealed and bar.close < threshold
        if touched_and_opened or broke_seal:
            if not open_indices or open_indices[-1] != index:
                open_indices.append(index)
        previous_sealed = bar.close >= threshold
    execution_index = open_indices[0] if open_indices else first_touch
    execution = day.minutes[execution_index]
    return _detection(
        DETECTOR_ID_S3,
        day,
        _evidence(
            qualified,
            day,
            available=execution if qualified else day.minutes[-1],
            execution_session="same_day",
            execution_price=execution.close,
            execution_reachable=qualified and execution.volume_shares > 0,
            values={
                "limit_up": day.published_limit_up,
                "first_touch_at": day.minutes[first_touch].timestamp.isoformat()
                if touched
                else None,
                "open_count": len(open_indices),
                "sealed_at_close": day.minutes[-1].close >= threshold,
            },
        ),
    )


def _volume_stall(day: IntradayDay, history: Sequence[IntradayDay]) -> Detection:
    current_volume = _total_volume(day)
    mean_volume = sum(_total_volume(item) for item in history) / len(history)
    volume_ratio = current_volume / mean_volume if mean_volume > 0 else math.inf
    close_bar = day.minutes[-1]
    close_change = close_bar.close / day.pre_close - 1
    intraday_high = max(bar.high for bar in day.minutes) / day.pre_close - 1
    qualified = (
        volume_ratio >= VOLUME_RATIO_MIN
        and abs(close_change) <= STALL_CLOSE_CHANGE_MAX + 1e-12
        and intraday_high >= STALL_INTRADAY_HIGH_MIN
    )
    return _detection(
        DETECTOR_ID_S4,
        day,
        _evidence(
            qualified,
            day,
            available=close_bar,
            execution_session="next_open",
            execution_price=close_bar.close,
            execution_reachable=True,
            values={
                "volume_ratio_vs_prev5": volume_ratio,
                "close_change": close_change,
                "intraday_high_change": intraday_high,
            },
        ),
    )


def _touched_limit_down(day: IntradayDay) -> Detection:
    threshold = day.published_limit_down + PRICE_TOLERANCE
    touched = [index for index, bar in enumerate(day.minutes) if bar.low <= threshold]
    qualified = bool(touched)
    first_touch = touched[0] if touched else len(day.minutes) - 1
    reopened = next(
        (
            index
            for index in range(first_touch, len(day.minutes))
            if day.minutes[index].close > threshold
            and day.minutes[index].volume_shares > 0
        ),
        None,
    )
    execution_index = reopened if reopened is not None else first_touch
    execution = day.minutes[execution_index]
    crossed_pre_close = qualified and any(
        bar.close > day.pre_close for bar in day.minutes[first_touch:]
    )
    sealed = qualified and reopened is None
    branch = (
        "sealed"
        if sealed
        else "reopened_above_pre_close"
        if crossed_pre_close
        else "reopened_below_pre_close"
        if qualified
        else "not_touched"
    )
    return _detection(
        DETECTOR_ID_S5,
        day,
        _evidence(
            qualified,
            day,
            available=execution if qualified else day.minutes[-1],
            execution_session="same_day",
            execution_price=execution.close,
            execution_reachable=qualified and reopened is not None,
            values={
                "limit_down": day.published_limit_down,
                "branch": branch,
                "touch_at": day.minutes[first_touch].timestamp.isoformat()
                if qualified
                else None,
                "reopened_at": execution.timestamp.isoformat()
                if reopened is not None
                else None,
            },
        ),
    )


def _break_vwap(day: IntradayDay) -> Detection:
    high_open = day.open_price / day.pre_close - 1
    streak = 0
    signal: IntradayMinute | None = None
    if high_open >= HIGH_OPEN_MIN:
        for bar in day.minutes:
            if bar.close < bar.cumulative_vwap:
                streak += 1
                if streak >= VWAP_BREAK_MINUTES:
                    signal = bar
                    break
            else:
                streak = 0
    available = signal or day.minutes[-1]
    return _detection(
        DETECTOR_ID_S6,
        day,
        _evidence(
            signal is not None,
            day,
            available=available,
            execution_session="same_day",
            execution_price=available.close,
            execution_reachable=signal is not None and available.volume_shares > 0,
            values={
                "open_change": high_open,
                "below_vwap_minutes": VWAP_BREAK_MINUTES,
                "signal_vwap": signal.cumulative_vwap if signal else None,
            },
        ),
    )


def _early_stall(day: IntradayDay) -> Detection:
    first = day.minutes[: EARLY_FIRST_WINDOW_END + 1]
    second = day.minutes[EARLY_FIRST_WINDOW_END + 1 : EARLY_SIGNAL_INDEX + 1]
    signal = day.minutes[EARLY_SIGNAL_INDEX]
    first_high = max(bar.high for bar in first)
    later_high = max(bar.high for bar in second)
    qualified = (
        first_high > day.open_price
        and later_high <= first_high + PRICE_TOLERANCE
        and signal.close < signal.cumulative_vwap
    )
    return _detection(
        DETECTOR_ID_S7,
        day,
        _evidence(
            qualified,
            day,
            available=signal,
            execution_session="same_day",
            execution_price=signal.close,
            execution_reachable=qualified and signal.volume_shares > 0,
            values={
                "first_30m_high": first_high,
                "ten_to_1030_high": later_high,
                "signal_vwap": signal.cumulative_vwap,
            },
        ),
    )


def _turnover_abnormal(
    day: IntradayDay,
    history: Sequence[IntradayDay],
) -> tuple[Detection | None, str | None]:
    all_days = (*history, day)
    if any(
        item.turnover is None
        or item.turnover.float_shares is None
        or item.turnover.available_at is None
        for item in all_days
    ):
        return None, EscapeCensorReason.PIT_FACT_MISSING.value
    cumulative = [0] * len(all_days)
    last_eligible: tuple[IntradayMinute, float, float, float] | None = None
    for minute_index in range(240):
        for index, item in enumerate(all_days):
            cumulative[index] += item.minutes[minute_index].volume_shares
        signal = day.minutes[minute_index]
        if any(
            item.turnover is None
            or item.turnover.available_at is None
            or item.turnover.available_at > item.minutes[minute_index].timestamp
            for item in all_days
        ):
            continue
        historical_turnover = [
            cumulative[index] / float(item.turnover.float_shares)
            for index, item in enumerate(history)
            if item.turnover is not None and item.turnover.float_shares is not None
        ]
        mean_history = sum(historical_turnover) / len(historical_turnover)
        current_turnover = cumulative[-1] / float(day.turnover.float_shares)
        ratio = current_turnover / mean_history if mean_history > 0 else math.inf
        last_eligible = (signal, ratio, current_turnover, mean_history)
        if ratio >= TURNOVER_RATIO_MIN and signal.close > day.pre_close:
            return (
                _turnover_detection(
                    day,
                    signal=signal,
                    qualified=True,
                    ratio=ratio,
                    current_turnover=current_turnover,
                    mean_history=mean_history,
                ),
                None,
            )
    if last_eligible is None:
        return None, EscapeCensorReason.PIT_FACT_MISSING.value
    signal, ratio, current_turnover, mean_history = last_eligible
    return (
        _turnover_detection(
            day,
            signal=signal,
            qualified=False,
            ratio=ratio,
            current_turnover=current_turnover,
            mean_history=mean_history,
        ),
        None,
    )


def _turnover_detection(
    day: IntradayDay,
    *,
    signal: IntradayMinute,
    qualified: bool,
    ratio: float,
    current_turnover: float,
    mean_history: float,
) -> Detection:
    assert day.turnover is not None
    assert day.turnover.float_shares is not None
    assert day.turnover.available_at is not None
    return _detection(
        DETECTOR_ID_S10,
        day,
        _evidence(
            qualified,
            day,
            available=signal,
            execution_session="same_day",
            execution_price=signal.close,
            execution_reachable=qualified and signal.volume_shares > 0,
            values={
                "turnover_ratio_vs_prev5_same_minute": ratio,
                "current_turnover": current_turnover,
                "historical_turnover_mean": mean_history,
                "float_shares": day.turnover.float_shares,
                "turnover_available_at": day.turnover.available_at.isoformat(),
            },
        ),
    )


def detect_intraday_escape_signals(
    bundle: EscapeRiskIntradayBundle,
    *,
    symbols: Sequence[str],
    calendar: Sequence[date],
    start: date,
    end: date,
) -> IntradayDetectionResult:
    """Evaluate every requested symbol/day; missing facts become explicit censors."""
    ordered_days = tuple(calendar)
    positions = {day: index for index, day in enumerate(ordered_days)}
    censor_codes: dict[str, set[str]] = {signal_id: set() for signal_id in _SIGNAL_IDS}
    detections: list[Detection] = []
    requested_pairs = available_pairs = 0
    for symbol in symbols:
        for day in ordered_days:
            if not start <= day <= end:
                continue
            requested_pairs += 1
            current = bundle.rows.get((symbol, day))
            if current is None:
                reason = bundle.unavailable.get((symbol, day), "intraday_rows_missing")
                code = (
                    EscapeCensorReason.INTRADAY_INTEGRITY.value
                    if "integrity" in reason or "mismatch" in reason
                    else EscapeCensorReason.INTRADAY_DATA_MISSING.value
                )
                for signal_id in _SIGNAL_IDS:
                    censor_codes[signal_id].add(code)
                continue
            available_pairs += 1
            detections.extend(
                (
                    _tail_dive(current),
                    _opened_limit_up(current),
                    _touched_limit_down(current),
                    _break_vwap(current),
                    _early_stall(current),
                )
            )
            position = positions[day]
            previous_days = ordered_days[max(0, position - VOLUME_HISTORY_DAYS) : position]
            history = [bundle.rows.get((symbol, value)) for value in previous_days]
            if len(history) != VOLUME_HISTORY_DAYS or any(item is None for item in history):
                censor_codes["s4"].add(EscapeCensorReason.HISTORY_INCOMPLETE.value)
                censor_codes["s10"].add(EscapeCensorReason.HISTORY_INCOMPLETE.value)
                continue
            complete_history = tuple(item for item in history if item is not None)
            detections.append(_volume_stall(current, complete_history))
            s10, censor = _turnover_abnormal(current, complete_history)
            if s10 is not None:
                detections.append(s10)
            if censor is not None:
                censor_codes["s10"].add(censor)
    return IntradayDetectionResult(
        detections=tuple(detections),
        censor_codes=MappingProxyType(
            {key: tuple(sorted(values)) for key, values in censor_codes.items()}
        ),
        coverage=MappingProxyType(
            {
                "requested_pairs": requested_pairs,
                "available_pairs": available_pairs,
                "unavailable_pairs": requested_pairs - available_pairs,
            }
        ),
    )


__all__ = [
    "IntradayDetectionResult",
    "detect_intraday_escape_signals",
]
