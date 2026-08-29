"""Pure, pre-registered 独孤趋势 daily-event detector."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.services.hold_firm_patterns.models import Bar

from .models import BandMode, CensorReason, Detection, DetectionEvidence, DuguVariantId

DETECTOR_ID: Final = "dugu_trend"
DUGU_VARIANTS: Final[dict[str, tuple[int, int, int]]] = {
    "ma_24_72": (24, 72, 200),
    "ma_20_70": (20, 70, 200),
}
RECLAIM_MA_DAYS: Final = 5
PULLBACK_LOOKBACK_DAYS: Final = 10
M3_WINDOW_DAYS: Final = 20
M3_MAX_RETURN: Final = 0.30
FIXED_BAND_PCT: Final = 0.03
ATR_WINDOW_DAYS: Final = 20
ATR_BAND_MULT: Final = 1.0
HYPOTHESIS_LABEL: Final = "dugu_trend_preregistered_v1"


@dataclass(frozen=True, slots=True)
class DuguTrendConfig:
    variant: DuguVariantId = "ma_24_72"
    band_mode: BandMode = "fixed"
    require_m3: bool = False

    @property
    def windows(self) -> tuple[int, int, int]:
        return DUGU_VARIANTS[self.variant]


def resolve_dugu_config(
    variant: DuguVariantId = "ma_24_72",
    band_mode: BandMode = "fixed",
    require_m3: bool = False,
) -> DuguTrendConfig:
    if variant not in DUGU_VARIANTS:
        raise ValueError(f"unknown Dugu variant: {variant}")
    if band_mode not in ("fixed", "atr"):
        raise ValueError(f"unknown band mode: {band_mode}")
    return DuguTrendConfig(variant=variant, band_mode=band_mode, require_m3=require_m3)


def _mean(values: list[float] | tuple[float, ...]) -> float | None:
    if not values or not all(math.isfinite(value) for value in values):
        return None
    result = sum(values) / len(values)
    return result if math.isfinite(result) else None


def _atr(
    closes: list[float | None], highs: list[float | None], lows: list[float | None], index: int
) -> float | None:
    if index < ATR_WINDOW_DAYS:
        return None
    values: list[float] = []
    for position in range(index - ATR_WINDOW_DAYS + 1, index + 1):
        if position == 0:
            return None
        high, low, previous = highs[position], lows[position], closes[position - 1]
        if high is None or low is None or previous is None:
            return None
        true_range = max(high - low, abs(high - previous), abs(low - previous))
        if not math.isfinite(true_range) or true_range < 0:
            return None
        values.append(true_range)
    return _mean(values)


class DuguTrendDetector:
    """Evaluate T1/T2/T3 using only the prefix ending at each signal day."""

    def __init__(self, config: DuguTrendConfig | None = None) -> None:
        self.config = config or DuguTrendConfig()

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID

    @property
    def variant(self) -> str:
        return self.config.variant

    def detect(
        self,
        symbol: str,
        bars: tuple[Bar, ...] | list[Bar],
        calendar: tuple[date, ...] | list[date],
    ) -> tuple[Detection, ...]:
        ordered = tuple(bars)
        cal = tuple(calendar)
        if not ordered or not cal:
            return ()
        if any(cal[index] >= cal[index + 1] for index in range(len(cal) - 1)):
            raise ValueError("calendar must be strictly ascending")
        previous: date | None = None
        for bar in ordered:
            if previous is not None and bar.date <= previous:
                raise ValueError("bars must be strictly ascending by date")
            if bar.date not in set(cal):
                raise ValueError(f"bar date {bar.date} missing from calendar")
            previous = bar.date
        cal_index = {day: index for index, day in enumerate(cal)}
        by_date = {bar.date: bar for bar in ordered}
        closes: list[float | None] = [None] * len(cal)
        highs: list[float | None] = [None] * len(cal)
        lows: list[float | None] = [None] * len(cal)
        for day, bar in by_date.items():
            index = cal_index[day]
            closes[index] = bar.research_close_adj
            highs[index] = bar.research_high_adj
            lows[index] = bar.research_low_adj
        fast_window, mid_window, long_window = self.config.windows
        windows = (RECLAIM_MA_DAYS, fast_window, mid_window, long_window)
        ma: dict[int, list[float | None]] = {window: [None] * len(cal) for window in windows}
        for window in windows:
            for index in range(window - 1, len(cal)):
                values = closes[index - window + 1 : index + 1]
                if all(value is not None for value in values):
                    ma[window][index] = _mean(tuple(value for value in values if value is not None))
        detections: list[Detection] = []
        for index, day in enumerate(cal):
            if day not in by_date or index < PULLBACK_LOOKBACK_DAYS:
                continue
            pullback = range(index - PULLBACK_LOOKBACK_DAYS, index)
            if any(
                closes[position] is None or ma[RECLAIM_MA_DAYS][position] is None
                for position in pullback
            ):
                continue
            anchor_ma5 = ma[RECLAIM_MA_DAYS][index]
            anchor_close = closes[index]
            if anchor_close is None or anchor_ma5 is None:
                continue
            t3 = anchor_close > anchor_ma5 and any(
                closes[position] is not None
                and ma[RECLAIM_MA_DAYS][position] is not None
                and closes[position] <= ma[RECLAIM_MA_DAYS][position]
                for position in pullback
            )
            touch: tuple[int, float, float | None] | None = None
            for position in pullback:
                fast = ma[fast_window][position]
                mid = ma[mid_window][position]
                low = lows[position]
                if fast is None or mid is None or low is None:
                    continue
                atr = (
                    _atr(closes, highs, lows, position) if self.config.band_mode == "atr" else None
                )
                if self.config.band_mode == "atr" and atr is None:
                    continue
                band = (
                    (fast * FIXED_BAND_PCT)
                    if self.config.band_mode == "fixed"
                    else float(atr) * ATR_BAND_MULT
                )
                upper, lower = fast + band, mid - band
                if lower <= low <= upper:
                    touch = (position, upper, atr)
                    break
            t2 = touch is not None
            if not (t2 and t3):
                continue
            fast = ma[fast_window][index]
            mid = ma[mid_window][index]
            long = ma[long_window][index]
            if fast is None or mid is None or long is None:
                detections.append(
                    Detection(
                        DETECTOR_ID,
                        self.variant,
                        symbol,
                        day,
                        censor=CensorReason.WARMUP_INCOMPLETE,
                    )
                )
                continue
            t1 = fast > mid > long and anchor_close > fast
            m3_max: float | None = None
            m3_pass = True
            if self.config.require_m3:
                if (
                    index < M3_WINDOW_DAYS
                    or closes[index - M3_WINDOW_DAYS] is None
                    or closes[index - M3_WINDOW_DAYS] <= 0
                    or any(value is None for value in closes[index - M3_WINDOW_DAYS : index + 1])
                ):
                    detections.append(
                        Detection(
                            DETECTOR_ID,
                            self.variant,
                            symbol,
                            day,
                            censor=CensorReason.WARMUP_INCOMPLETE,
                        )
                    )
                    continue
                reference = closes[index - M3_WINDOW_DAYS]
                assert reference is not None
                m3_max = max(
                    value
                    for value in closes[index - M3_WINDOW_DAYS : index + 1]
                    if value is not None
                )
                m3_return = anchor_close / reference - 1.0
                m3_pass = m3_return <= M3_MAX_RETURN
            else:
                m3_return = None
            values: dict[str, object] = {
                "t1": t1,
                "t2": t2,
                "t3": t3,
                "m3_required": self.config.require_m3,
                "m3_pass": m3_pass,
                "m3_max_return": M3_MAX_RETURN,
                "m3_return_20d": m3_return,
                "ma_fast": fast,
                "ma_mid": mid,
                "ma_long": long,
                "ma5": anchor_ma5,
                "close": anchor_close,
                "band_mode": self.config.band_mode,
                "fixed_band_pct": FIXED_BAND_PCT,
                "atr_band_mult": ATR_BAND_MULT,
                "pullback_lookback_days": PULLBACK_LOOKBACK_DAYS,
                "hypothesis_label": HYPOTHESIS_LABEL,
            }
            if touch is not None:
                values["touch_day_offset"] = index - touch[0]
                values["touch_level_upper"] = touch[1]
                values["atr20"] = touch[2]
            if m3_max is not None:
                values["m3_max20"] = m3_max
            detections.append(
                Detection(
                    DETECTOR_ID,
                    self.variant,
                    symbol,
                    day,
                    evidence=DetectionEvidence(t1 and m3_pass, values),
                )
            )
        return tuple(detections)


__all__ = [
    "ATR_BAND_MULT",
    "ATR_WINDOW_DAYS",
    "DETECTOR_ID",
    "DUGU_VARIANTS",
    "FIXED_BAND_PCT",
    "DuguTrendConfig",
    "DuguTrendDetector",
    "M3_WINDOW_DAYS",
    "PULLBACK_LOOKBACK_DAYS",
    "RECLAIM_MA_DAYS",
    "resolve_dugu_config",
]
