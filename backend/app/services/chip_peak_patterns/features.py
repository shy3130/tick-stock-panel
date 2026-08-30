"""Daily C0-derived features used by C1-C5 detectors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .chip_distribution import build_chip_distribution, concentration, detect_peaks
from .models import BetaArm, ChipBar, ChipModelParams, TurnoverDay


@dataclass(frozen=True)
class ChipDayFeatures:
    date: date
    winner_ratio: float
    low_chip_share: float
    peak_count: int
    peaks: tuple[tuple[float, float], ...]
    concentration_90: float
    concentration_70: float
    main_peak_price: float | None
    main_peak_mass: float
    price_pct_120: float | None
    main_peak_position_60: float | None
    is_60d_high_breakout: bool
    prior_run_60: float | None
    in_cooldown: bool


def build_feature_series(
    bars: Sequence[ChipBar],
    turnover: Sequence[float | TurnoverDay | None],
    *,
    arm: BetaArm | str = BetaArm.TURNOVER,
    params: ChipModelParams | None = None,
) -> tuple[ChipDayFeatures, ...]:
    params = params or ChipModelParams()
    bars = tuple(bars)
    snaps = build_chip_distribution(bars, turnover, arm, params)
    ratios = [b.adj_ratio for b in bars]
    cooldown_until = -1
    out: list[ChipDayFeatures] = []
    for i, (bar, snap) in enumerate(zip(bars, snaps, strict=False)):
        if i and abs(ratios[i] - ratios[i - 1]) > 1e-9:
            cooldown_until = i + params.ex_div_cooldown_market_days
        peaks = detect_peaks(
            snap.density,
            snap.grid,
            sigma_cells=max(0.5, params.kernel_sigma_pct / params.price_step_pct),
            min_prominence=params.peak_min_prominence,
        )
        main = max(peaks, key=lambda x: x[1]) if peaks else (None, 0.0)
        conc90 = concentration(snap.density, snap.grid, band_pct=params.concentration_band_pct)
        conc70 = concentration(snap.density, snap.grid, band_pct=0.07)
        closes = [float(x.close) for x in bars[max(0, i - 119) : i + 1]]
        pct = (sum(x <= bar.close for x in closes) / len(closes)) if len(closes) >= 120 else None
        prior = (
            (bar.close / bars[i - 60].close - 1.0) if i >= 60 and bars[i - 60].close > 0 else None
        )
        pos = None
        if main[0] is not None and i >= 59:
            lows = [b.low for b in bars[i - 59 : i + 1]]
            highs = [b.high for b in bars[i - 59 : i + 1]]
            spread = max(highs) - min(lows)
            pos = max(0.0, min(1.0, (main[0] - min(lows)) / spread)) if spread > 1e-12 else None
        breakout = i >= 60 and bar.close > max(b.close for b in bars[i - 60 : i])
        out.append(
            ChipDayFeatures(
                bar.date,
                snap.winner_ratio(bar.close),
                snap.low_chip_share(bar.close, params.low_chip_price_ratio),
                len(peaks),
                peaks,
                conc90,
                conc70,
                main[0],
                main[1],
                pct,
                pos,
                breakout,
                prior,
                i <= cooldown_until,
            )
        )
    return tuple(out)


def c2_lower_peak_mass(feature: ChipDayFeatures, close: float) -> float:
    below = [mass for price, mass in feature.peaks if price < close]
    return max(below, default=0.0)


def c2_holds(
    previous: ChipDayFeatures,
    current: ChipDayFeatures,
    previous_close: float,
    current_close: float,
    *,
    tolerance: float = 0.10,
) -> bool:
    old, new = (
        c2_lower_peak_mass(previous, previous_close),
        c2_lower_peak_mass(current, current_close),
    )
    return old > 0 and new >= old * (1.0 - tolerance)
