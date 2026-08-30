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
    DOJI_PRIOR_MOVE_MIN_PCT,
    DOJI_PRIOR_MOVE_WINDOW_DAYS,
    DOJI_VOLUME_REF_WINDOW,
    DojiDetection,
    DojiFactorId,
    DojiLandmark,
    DojiLandmarkKind,
)
from .morphology import (
    candle_metrics,
    is_t_bar,
    prior_move_pct,
    reference_volume,
    validate_series,
    volume_state,
)

FACTOR_ID: DojiFactorId = "t_bar_low"


class TBarDetector:
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
            if bar is None or facts.row(symbol, day) is None:
                continue
            if i < DOJI_PRIOR_MOVE_WINDOW_DAYS:
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
            ds = cal[i - DOJI_PRIOR_MOVE_WINDOW_DAYS : i + 1]
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
            move = prior_move_pct([by[d] for d in ds])
            if move is None or move > -DOJI_PRIOR_MOVE_MIN_PCT:
                continue
            m = candle_metrics(bar)
            refs = cal[i - DOJI_VOLUME_REF_WINDOW : i] if i >= DOJI_VOLUME_REF_WINDOW else ()
            rb = [by[d] for d in refs if d in by]
            mean = reference_volume(rb) if len(rb) == DOJI_VOLUME_REF_WINDOW else None
            state = volume_state(bar.volume, mean)
            qualified = is_t_bar(bar, theta=self.theta)
            out.append(
                DojiDetection(
                    FACTOR_ID,
                    symbol,
                    day,
                    DojiLandmark(DojiLandmarkKind.SIGNAL_DAY_CLOSE, day, day),
                    evidence=DetectionEvidence(
                        qualified,
                        {
                            "prior_move": move,
                            "is_t_bar": qualified,
                            "body_ratio": m.body_ratio,
                            "upper_shadow": m.upper,
                            "lower_shadow": m.lower,
                            "volume_state": state.value if state else None,
                            "volume_ratio": bar.volume / mean if mean else None,
                        },
                    ),
                )
            )
        return tuple(out)
