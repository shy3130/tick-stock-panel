from datetime import date, timedelta

import numpy as np
import pytest

from app.services.chip_peak_patterns.chip_distribution import (
    MissingPitTurnoverError,
    build_chip_distribution,
    detect_peaks,
)
from app.services.chip_peak_patterns.models import BetaArm, ChipBar, TurnoverDay


def bars(prices):
    return tuple(
        ChipBar(
            symbol="000001.SZ",
            date=date(2025, 1, 1) + timedelta(days=i),
            open=p,
            high=p * 1.02,
            low=p * 0.98,
            close=p,
            raw_open=p,
            raw_high=p * 1.02,
            raw_low=p * 0.98,
            raw_close=p,
            volume=10.0,
            amount=100.0,
        )
        for i, p in enumerate(prices)
    )


def turnover(n, reported_pct=1.0):
    return tuple(
        TurnoverDay(
            available_at=date(2025, 1, 1) + timedelta(days=i),
            reported_turnover_pct=reported_pct,
            source_day=date(2025, 1, 1) + timedelta(days=i),
        )
        for i in range(n)
    )


def test_mass_conservation_and_determinism():
    bs = bars([10, 10.5, 11, 10.2])
    ts = turnover(len(bs))
    a = build_chip_distribution(bs, ts)
    b = build_chip_distribution(bs, ts)
    assert all(abs(x.density.sum() - 1) < 1e-9 for x in a)
    assert all(np.array_equal(x.density, y.density) for x, y in zip(a, b, strict=False))


def test_zero_exchange_freezes_density_and_missing_fails_closed():
    bs = bars([10, 11])
    a = build_chip_distribution(bs, [0.0, 0.0])
    assert np.dot(a[0].grid, a[0].density) == pytest.approx(np.dot(a[1].grid, a[1].density))
    with pytest.raises(MissingPitTurnoverError):
        build_chip_distribution(bs, [0.0, None])


def test_uniform_band_and_beta_arms_are_distinguishable():
    bs = bars([10, 20])
    ts = turnover(2, 10.0)
    main = build_chip_distribution(bs, ts, BetaArm.TURNOVER)
    fast = build_chip_distribution(bs, ts, BetaArm.TURNOVER_DOUBLE)
    assert not np.array_equal(main[-1].density, fast[-1].density)
    assert abs(main[-1].density.sum() - 1) < 1e-9
    assert main[-1].turnover == pytest.approx(0.10)
    assert fast[-1].turnover == pytest.approx(0.20)


def test_reported_hslv_is_always_interpreted_as_percentage_points():
    bs = bars([10, 20])
    sub_one_percent = build_chip_distribution(bs, turnover(2, 0.47), BetaArm.TURNOVER)
    assert sub_one_percent[-1].turnover == pytest.approx(0.0047)

    ordinary = build_chip_distribution(bs, turnover(2, 3.2), BetaArm.TURNOVER)
    assert ordinary[-1].turnover == pytest.approx(0.032)


def test_untyped_turnover_percent_and_decimal_inputs_remain_supported():
    bs = bars([10, 20])
    decimal = build_chip_distribution(bs, (0.10, 0.10), BetaArm.TURNOVER)
    percent = build_chip_distribution(bs, (10.0, 10.0), BetaArm.TURNOVER)
    assert np.array_equal(decimal[-1].density, percent[-1].density)
    assert percent[-1].turnover == pytest.approx(0.10)
    double = build_chip_distribution(bs, (20.0, 20.0), BetaArm.TURNOVER)
    assert double[-1].turnover == pytest.approx(0.20)
    assert abs(double[-1].density.sum() - 1) < 1e-9
    with pytest.raises(MissingPitTurnoverError):
        build_chip_distribution(bs, (150.0, 150.0), BetaArm.TURNOVER)


def test_reported_turnover_beta_scaling_uses_decimal_fraction():
    bs = bars([10])
    snapshots = build_chip_distribution(
        bs,
        turnover(1, reported_pct=3.2),
        BetaArm.TURNOVER_HALF,
    )

    assert snapshots[0].turnover == pytest.approx(0.016)


def test_lagged_float_shares_fallback_uses_canonical_bar_volume():
    bs = bars([10])
    snapshots = build_chip_distribution(
        bs,
        (
            TurnoverDay(
                available_at=date(2024, 12, 31),
                float_shares=100.0,
                source_day=date(2024, 12, 31),
                availability_basis="previous_daily_market_close",
            ),
        ),
    )
    assert snapshots[0].turnover == pytest.approx(0.10)


def test_future_price_extremes_do_not_change_prior_snapshots():
    prefix_bars = bars([10, 11])
    prefix = build_chip_distribution(prefix_bars, turnover(2))
    extended = build_chip_distribution(bars([10, 11, 100]), turnover(3))

    for expected, actual in zip(prefix, extended[:2], strict=True):
        assert np.array_equal(expected.grid, actual.grid)
        assert np.array_equal(expected.density, actual.density)
        assert expected.turnover == actual.turnover


def test_peak_detection_is_deterministic():
    grid = np.arange(100, dtype=float)
    density = np.exp(-(((grid - 25) / 4) ** 2)) + 0.7 * np.exp(-(((grid - 70) / 5) ** 2))
    density /= density.sum()
    assert detect_peaks(density, grid) == detect_peaks(density, grid)
    assert len(detect_peaks(density, grid, min_prominence=0.01)) == 2
