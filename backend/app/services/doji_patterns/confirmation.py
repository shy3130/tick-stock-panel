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
    DOJI_VOLUME_REF_WINDOW,
    ConfirmDirection,
    DojiDetection,
    DojiFactorId,
    DojiLandmark,
    DojiLandmarkKind,
)
from .morphology import candle_metrics, is_doji, reference_volume, validate_series, volume_state

FACTOR_ID: DojiFactorId = "next_day_confirmation"


class ConfirmationDetector:
    def __init__(self, theta: float = DOJI_BODY_RATIO_MAX) -> None:
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
            if bar is None or facts.row(symbol, day) is None or not is_doji(bar, theta=self.theta):
                continue
            if i + 1 >= len(cal) or cal[i + 1] not in by or facts.row(symbol, cal[i + 1]) is None:
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
            nxt = by[cal[i + 1]]
            direction = (
                ConfirmDirection.BULLISH
                if nxt.research_close_adj > nxt.research_open_adj
                else ConfirmDirection.BEARISH
                if nxt.research_close_adj < nxt.research_open_adj
                else None
            )
            refs = cal[i - DOJI_VOLUME_REF_WINDOW : i] if i >= DOJI_VOLUME_REF_WINDOW else ()
            rb = [by[d] for d in refs if d in by]
            mean = reference_volume(rb) if len(rb) == DOJI_VOLUME_REF_WINDOW else None
            state = volume_state(bar.volume, mean)
            out.append(
                DojiDetection(
                    FACTOR_ID,
                    symbol,
                    day,
                    DojiLandmark(DojiLandmarkKind.SIGNAL_DAY_CLOSE, day, day),
                    evidence=DetectionEvidence(
                        direction is not None,
                        {
                            "is_doji": True,
                            "body_ratio": candle_metrics(bar).body_ratio,
                            "confirmation_date": cal[i + 1],
                            "confirm_direction": direction.value if direction else None,
                            "confirmation_body_ratio": candle_metrics(nxt).body_ratio,
                            "volume_state": state.value if state else None,
                            "volume_ratio": bar.volume / mean if mean else None,
                        },
                    ),
                )
            )
        return tuple(out)
