from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.services.hold_firm_patterns.models import (
    Bar,
    CensorReason,
    DetectionEvidence,
    MarketFactsSource,
)

from .models import (
    DOJI_BODY_RATIO_MAX,
    DOJI_POSITION_HIGH_MIN,
    DOJI_POSITION_LOW_MAX,
    DOJI_POSITION_WINDOW_DAYS,
    DOJI_VOLUME_REF_WINDOW,
    DojiDetection,
    DojiFactorId,
    DojiLandmark,
    DojiLandmarkKind,
    PositionStratum,
)
from .morphology import (
    candle_metrics,
    is_doji,
    position_percentile,
    reference_volume,
    validate_series,
    volume_state,
)

FACTOR_ID: DojiFactorId = "doji_position_interaction"


class DojiPositionDetector:
    def __init__(self, theta: float = DOJI_BODY_RATIO_MAX):
        self.theta = theta

    @property
    def factor_id(self) -> DojiFactorId:
        return FACTOR_ID

    def detect(
        self, symbol: str, bars: Sequence[Bar], facts: MarketFactsSource, calendar: Sequence[date]
    ) -> tuple[DojiDetection, ...]:
        cal, by = validate_series(bars, calendar)
        out = []
        for i, day in enumerate(cal):
            bar = by.get(day)
            if bar is None or facts.row(symbol, day) is None:
                continue
            if i < DOJI_POSITION_WINDOW_DAYS - 1:
                out.append(
                    DojiDetection(
                        FACTOR_ID,
                        symbol,
                        day,
                        None,
                        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
                    )
                )
                continue
            ds = cal[i - DOJI_POSITION_WINDOW_DAYS + 1 : i + 1]
            if any(d not in by for d in ds):
                out.append(
                    DojiDetection(
                        FACTOR_ID,
                        symbol,
                        day,
                        None,
                        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
                    )
                )
                continue
            pos = position_percentile([by[d] for d in ds])
            if pos is None:
                out.append(
                    DojiDetection(
                        FACTOR_ID, symbol, day, None, censor=CensorReason.LOW_POSITION_UNDEFINED
                    )
                )
                continue
            stratum = (
                PositionStratum.HIGH
                if pos >= DOJI_POSITION_HIGH_MIN
                else PositionStratum.LOW
                if pos <= DOJI_POSITION_LOW_MAX
                else PositionStratum.MIDDLE
            )
            metric = candle_metrics(bar)
            refs = cal[i - DOJI_VOLUME_REF_WINDOW : i] if i >= DOJI_VOLUME_REF_WINDOW else ()
            rb = [by[d] for d in refs if d in by]
            mean = reference_volume(rb) if len(rb) == DOJI_VOLUME_REF_WINDOW else None
            state = volume_state(bar.volume, mean)
            doji = is_doji(bar, theta=self.theta)
            out.append(
                DojiDetection(
                    FACTOR_ID,
                    symbol,
                    day,
                    DojiLandmark(DojiLandmarkKind.SIGNAL_DAY_CLOSE, day, day),
                    evidence=DetectionEvidence(
                        doji and stratum is not PositionStratum.MIDDLE,
                        {
                            "position": pos,
                            "stratum": stratum.value,
                            "is_doji": doji,
                            "body_ratio": metric.body_ratio,
                            "volume_state": state.value if state else None,
                            "volume_ratio": bar.volume / mean if mean else None,
                        },
                    ),
                )
            )
        return tuple(out)
