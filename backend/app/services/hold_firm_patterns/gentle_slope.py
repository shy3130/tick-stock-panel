"""Pure F3 low-gentle-slope detector."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

from .models import (
    Bar,
    CensorReason,
    DetectionEvidence,
    FactorId,
    Landmark,
    LandmarkKind,
    MarketFactsSource,
    ParentDetection,
)

FACTOR_ID: FactorId = "low_gentle_slope"
LOW_LOOKBACK_DAYS = 120
WINDOW_DAYS = 20
PRIOR_HIGH_DAYS = 19
LOW_POSITION_MAX = 0.35
DAILY_RETURN_BAND = (-0.03, 0.03)
OLS_SLOPE_MIN = 0.0
OLS_R2_MIN = 0.60
BULLISH_RATIO_MIN = 0.60
LOG_VOLUME_SLOPE_MAX = 0.0
VOLUME_CONTRACTION_RATIO_MAX = 0.80
HYPOTHESIS_LABEL = "control_inference_unverified"
_REQUIRED_BARS = LOW_LOOKBACK_DAYS + WINDOW_DAYS


def _finite(*values: float) -> bool:
    return all(math.isfinite(value) for value in values)


def _ols(values: Sequence[float]) -> tuple[float, float]:
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    s_xx = sum((index - x_mean) ** 2 for index in range(n))
    s_xy = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    slope = s_xy / s_xx
    ss_total = sum((value - y_mean) ** 2 for value in values)
    if ss_total <= 0.0:
        return slope, 0.0
    intercept = y_mean - slope * x_mean
    ss_residual = sum(
        (value - (intercept + slope * index)) ** 2 for index, value in enumerate(values)
    )
    return slope, 1.0 - ss_residual / ss_total


def _matches_parent_shape(sequence: Sequence[Bar]) -> bool:
    if len(sequence) != WINDOW_DAYS + 1:
        return False
    prior_close = sequence[0].research_close_adj
    window = sequence[1:]
    closes = [bar.research_close_adj for bar in window]
    opens = [bar.research_open_adj for bar in window]
    if (
        not _finite(prior_close, *closes, *opens)
        or prior_close <= 0.0
        or any(close <= 0.0 for close in closes)
    ):
        return False
    returns = [
        closes[0] / prior_close - 1.0,
        *(closes[index] / closes[index - 1] - 1.0 for index in range(1, WINDOW_DAYS)),
    ]
    if any(value < DAILY_RETURN_BAND[0] or value > DAILY_RETURN_BAND[1] for value in returns):
        return False
    price_slope, price_r2 = _ols([math.log(close) for close in closes])
    prior_highs = [bar.research_high_adj for bar in window[:-1]]
    return (
        price_slope > OLS_SLOPE_MIN
        and price_r2 >= OLS_R2_MIN
        and _finite(*prior_highs)
        and closes[-1] > max(prior_highs)
    )


class GentleSlopeDetector:
    @property
    def factor_id(self) -> FactorId:
        return FACTOR_ID

    def detect(
        self,
        symbol: str,
        bars: Sequence[Bar],
        facts: MarketFactsSource,
        calendar: Sequence[date],
    ) -> tuple[ParentDetection, ...]:
        del facts
        if not bars or not calendar:
            return ()
        ordered = tuple(bars)
        cal = tuple(calendar)
        cal_index = {day: index for index, day in enumerate(cal)}
        bar_by_date = {bar.date: bar for bar in ordered}
        if len(cal_index) != len(cal):
            raise ValueError("calendar dates must be unique")
        if any(cal[index] >= cal[index + 1] for index in range(len(cal) - 1)):
            raise ValueError("calendar must be strictly ascending")
        previous: date | None = None
        for bar in ordered:
            if previous is not None and bar.date <= previous:
                raise ValueError("bars must be strictly ascending by date")
            if bar.date not in cal_index:
                raise ValueError(f"bar date {bar.date} missing from calendar")
            previous = bar.date
        detections: list[ParentDetection] = []
        for anchor_bar in ordered:
            anchor_cal_index = cal_index[anchor_bar.date]
            if anchor_cal_index < WINDOW_DAYS:
                continue
            parent_days = cal[anchor_cal_index - WINDOW_DAYS : anchor_cal_index + 1]
            if any(day not in bar_by_date for day in parent_days):
                # Without the observable 20-day parent shape there is no event.
                continue
            parent_sequence = [bar_by_date[day] for day in parent_days]
            if not _matches_parent_shape(parent_sequence):
                continue
            if anchor_cal_index < _REQUIRED_BARS - 1:
                detections.append(
                    ParentDetection(
                        FACTOR_ID,
                        symbol,
                        anchor_bar.date,
                        None,
                        censor=CensorReason.WARMUP_INCOMPLETE,
                    )
                )
                continue
            required_days = cal[anchor_cal_index - _REQUIRED_BARS + 1 : anchor_cal_index + 1]
            if any(day not in bar_by_date for day in required_days):
                detections.append(
                    ParentDetection(
                        FACTOR_ID,
                        symbol,
                        anchor_bar.date,
                        None,
                        censor=CensorReason.WARMUP_INCOMPLETE,
                    )
                )
                continue
            contiguous_bars = [bar_by_date[day] for day in required_days]
            result = self._detect_anchor(symbol, contiguous_bars, _REQUIRED_BARS - 1)
            if result is not None:
                detections.append(result)
        return tuple(detections)

    def _detect_anchor(
        self, symbol: str, bars: Sequence[Bar], anchor: int
    ) -> ParentDetection | None:
        window = bars[anchor - WINDOW_DAYS + 1 : anchor + 1]
        pre_low = bars[anchor - WINDOW_DAYS - LOW_LOOKBACK_DAYS + 1 : anchor - WINDOW_DAYS + 1]
        high_120 = max(bar.research_high_adj for bar in pre_low)
        low_120 = min(bar.research_low_adj for bar in pre_low)
        if not _finite(high_120, low_120) or high_120 - low_120 <= 0.0:
            return ParentDetection(
                FACTOR_ID,
                symbol,
                bars[anchor].date,
                None,
                censor=CensorReason.LOW_POSITION_UNDEFINED,
            )
        start_close = window[0].research_close_adj
        low_position = (start_close - low_120) / (high_120 - low_120)
        if not _finite(low_position) or low_position > LOW_POSITION_MAX:
            return None

        closes = [bar.research_close_adj for bar in window]
        opens = [bar.research_open_adj for bar in window]
        prior_close = bars[anchor - WINDOW_DAYS].research_close_adj
        if (
            not _finite(prior_close, *closes, *opens)
            or prior_close <= 0.0
            or any(close <= 0.0 for close in closes)
        ):
            return None
        returns = [
            closes[0] / prior_close - 1.0,
            *(closes[i] / closes[i - 1] - 1.0 for i in range(1, WINDOW_DAYS)),
        ]
        if any(value < DAILY_RETURN_BAND[0] or value > DAILY_RETURN_BAND[1] for value in returns):
            return None
        price_slope, price_r2 = _ols([math.log(close) for close in closes])
        if price_slope <= OLS_SLOPE_MIN or price_r2 < OLS_R2_MIN:
            return None
        prior_highs = [bar.research_high_adj for bar in window[:-1]]
        anchor_close = closes[-1]
        if not _finite(*prior_highs) or anchor_close <= max(prior_highs):
            return None

        volumes = [bar.volume for bar in window]
        if not _finite(*volumes):
            return ParentDetection(
                FACTOR_ID,
                symbol,
                bars[anchor].date,
                None,
                censor=CensorReason.DIAGNOSTIC_WINDOW_INCOMPLETE,
            )
        has_zero_volume = any(volume <= 0.0 for volume in volumes)
        volume_slope = (
            None if has_zero_volume else _ols([math.log(volume) for volume in volumes])[0]
        )
        first5_mean = sum(volumes[:5]) / 5.0
        last5_mean = sum(volumes[-5:]) / 5.0
        contraction_ratio = last5_mean / first5_mean if first5_mean > 0.0 else None
        bullish_ratio = (
            sum(close > open_ for close, open_ in zip(closes, opens, strict=True)) / WINDOW_DAYS
        )
        prior_volumes = [bar.volume for bar in bars[anchor - WINDOW_DAYS : anchor]]
        amounts = [bar.amount for bar in window]
        values = self._values(
            high_120,
            low_120,
            start_close,
            low_position,
            closes,
            returns,
            price_slope,
            price_r2,
            anchor_close,
            max(prior_highs),
            opens,
            volumes,
            prior_volumes,
            amounts,
            volume_slope,
            first5_mean,
            last5_mean,
            contraction_ratio,
            bullish_ratio,
        )
        return ParentDetection(
            FACTOR_ID,
            symbol,
            bars[anchor].date,
            Landmark(
                LandmarkKind.SIGNAL_DAY_CLOSE,
                bars[anchor].date,
                bars[anchor].date,
            ),
            evidence=DetectionEvidence(
                qualified=(
                    volume_slope is not None
                    and contraction_ratio is not None
                    and bullish_ratio >= BULLISH_RATIO_MIN
                    and volume_slope < LOG_VOLUME_SLOPE_MAX
                    and contraction_ratio <= VOLUME_CONTRACTION_RATIO_MAX
                ),
                values=values,
            ),
        )

    @staticmethod
    def _values(
        high_120: float,
        low_120: float,
        start_close: float,
        low_position: float,
        closes: list[float],
        returns: list[float],
        price_slope: float,
        price_r2: float,
        anchor_close: float,
        prior_high: float,
        opens: list[float],
        volumes: list[float],
        prior_volumes: list[float],
        amounts: list[float],
        volume_slope: float | None,
        first5_mean: float,
        last5_mean: float,
        contraction_ratio: float | None,
        bullish_ratio: float,
    ) -> Mapping[str, object]:
        return {
            "low_lookback_days": LOW_LOOKBACK_DAYS,
            "low_position_max": LOW_POSITION_MAX,
            "window_days": WINDOW_DAYS,
            "daily_return_band": list(DAILY_RETURN_BAND),
            "ols_slope_min_strict": OLS_SLOPE_MIN,
            "ols_r2_min": OLS_R2_MIN,
            "prior_high_days": PRIOR_HIGH_DAYS,
            "bullish_ratio_min": BULLISH_RATIO_MIN,
            "log_volume_slope_max_strict": LOG_VOLUME_SLOPE_MAX,
            "volume_contraction_ratio_max": VOLUME_CONTRACTION_RATIO_MAX,
            "low_window_high_max": high_120,
            "low_window_low_min": low_120,
            "window_start_close": start_close,
            "low_position": low_position,
            "max_abs_daily_return": max(abs(value) for value in returns),
            "ols_slope": price_slope,
            "ols_r2": price_r2,
            "anchor_close": anchor_close,
            "prior_19_high": prior_high,
            "bullish_ratio": bullish_ratio,
            "log_volume_slope": volume_slope,
            "volume_first5_mean": first5_mean,
            "volume_last5_mean": last5_mean,
            "volume_contraction_ratio": contraction_ratio,
            "ma20": sum(closes) / WINDOW_DAYS,
            "prior_20d_mean_volume": sum(prior_volumes) / WINDOW_DAYS,
            "mean_amount_window": sum(amounts) / WINDOW_DAYS,
            "min_volume_window": min(volumes),
            "zero_volume_days_window": sum(volume <= 0.0 for volume in volumes),
            "hypothesis_label": HYPOTHESIS_LABEL,
            "liquidity_diagnostic_inputs": {
                "amounts": tuple(amounts),
                "volumes": tuple(volumes),
            },
            "dynamic_defense_inputs": {
                "ma20": sum(closes) / WINDOW_DAYS,
                "prior_20d_mean_volume": sum(prior_volumes) / WINDOW_DAYS,
            },
        }
