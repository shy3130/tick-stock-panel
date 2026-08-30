"""Pure C0 turnover-decay chip distribution on a discrete price grid."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .models import GEOMETRIC_BETA, MAX_GRID_CELLS, BetaArm, ChipBar, ChipModelParams, TurnoverDay


class MissingPitTurnoverError(ValueError):
    """A bar lacks a PIT float-share/available-at observation."""


@dataclass(frozen=True)
class ChipSnapshot:
    day: object
    grid: np.ndarray
    density: np.ndarray
    turnover: float

    def winner_ratio(self, price: float) -> float:
        return float(self.density[self.grid <= price].sum())

    def low_chip_share(self, price: float, ratio: float = 0.80) -> float:
        return float(self.density[self.grid <= price * ratio].sum())


def normalize_turnover(value: float | None) -> float:
    """Normalize decimal fractions and percentage inputs to a decimal fraction."""
    if (
        value is None
        or not math.isfinite(float(value))
        or float(value) < 0.0
        or float(value) > 100.0
    ):
        raise MissingPitTurnoverError("missing or invalid PIT turnover")
    t = float(value)
    return t / 100.0 if t > 1.0 else t


def _effective_exchange_fraction(turnover: float, arm: BetaArm | str) -> float:
    if not math.isfinite(turnover) or turnover < 0.0:
        raise MissingPitTurnoverError("missing or invalid PIT turnover")
    arm = arm.value if isinstance(arm, BetaArm) else arm
    if arm == "geometric_0.01":
        return GEOMETRIC_BETA
    multiplier = {"turnover": 1.0, "turnover_x0.5": 0.5, "turnover_x2": 2.0}.get(arm)
    if multiplier is None:
        raise ValueError(f"unknown beta arm: {arm}")
    return max(0.0, min(1.0, turnover * multiplier))


def effective_exchange(turnover: float, arm: BetaArm | str) -> float:
    return _effective_exchange_fraction(normalize_turnover(turnover), arm)


def _grid(first_bar: ChipBar, params: ChipModelParams) -> tuple[np.ndarray, float]:
    lo, hi = float(first_bar.low), float(first_bar.high)
    if not (math.isfinite(lo) and math.isfinite(hi) and lo > 0.0 and hi > 0.0):
        raise ValueError("positive finite price range required")
    lo, hi = min(lo, hi), max(lo, hi)
    step = max(1e-8, lo * params.price_step_pct)
    start = max(1e-8, lo - step)
    n = math.ceil((hi + step - start) / step) + 1
    if n > min(params.max_grid_cells, MAX_GRID_CELLS):
        raise ValueError("price grid exceeds frozen limit")
    return start + np.arange(n, dtype=np.float64) * step, step


def _expand_grid(
    grid: np.ndarray,
    density: np.ndarray,
    *,
    step: float,
    low: float,
    high: float,
    max_cells: int,
) -> tuple[np.ndarray, np.ndarray]:
    if not (math.isfinite(low) and math.isfinite(high) and low > 0.0 and high > 0.0):
        raise ValueError("positive finite price range required")
    low, high = min(low, high), max(low, high)
    lower_target = max(1e-8, low - step)
    upper_target = high + step
    prepend = max(0, math.ceil((float(grid[0]) - lower_target) / step))
    append = max(0, math.ceil((upper_target - float(grid[-1])) / step))
    size = len(grid) + prepend + append
    if size > max_cells:
        raise ValueError("price grid exceeds frozen limit")
    if prepend == 0 and append == 0:
        return grid, density
    start = float(grid[0]) - prepend * step
    expanded_grid = start + np.arange(size, dtype=np.float64) * step
    expanded_density = np.pad(density, (prepend, append), mode="constant")
    return expanded_grid, expanded_density


def _nearest(grid: np.ndarray, value: float) -> int:
    i = int(np.searchsorted(grid, value))
    if i <= 0:
        return 0
    if i >= len(grid):
        return len(grid) - 1
    return i if abs(grid[i] - value) < abs(grid[i - 1] - value) else i - 1


def _uniform_band(grid: np.ndarray, step: float, low: float, high: float) -> np.ndarray:
    weights = np.zeros(len(grid), dtype=np.float64)
    if not (math.isfinite(low) and math.isfinite(high)):
        raise ValueError("invalid price band")
    low, high = min(low, high), max(low, high)
    if high - low <= step * 1e-9:
        weights[_nearest(grid, low)] = 1.0
        return weights
    left, right = grid - step / 2, grid + step / 2
    overlap = np.maximum(0.0, np.minimum(right, high) - np.maximum(left, low))
    total = float(overlap.sum())
    if total <= 0:
        weights[_nearest(grid, (low + high) / 2)] = 1.0
    else:
        weights = overlap / total
    return weights


def build_chip_distribution(
    bars: Sequence[ChipBar],
    turnover: Sequence[float | TurnoverDay | None],
    arm: BetaArm | str = BetaArm.TURNOVER,
    params: ChipModelParams | None = None,
) -> tuple[ChipSnapshot, ...]:
    """Build deterministic snapshots; missing PIT turnover always fails closed."""
    params = params or ChipModelParams()
    if not bars or len(turnover) != len(bars):
        raise ValueError("bars and turnover must align")
    grid, step = _grid(bars[0], params)
    density = np.zeros(len(grid), dtype=np.float64)
    snapshots: list[ChipSnapshot] = []
    max_cells = min(params.max_grid_cells, MAX_GRID_CELLS)
    for bar, item in zip(bars, turnover, strict=False):
        grid, density = _expand_grid(
            grid,
            density,
            step=step,
            low=float(bar.low),
            high=float(bar.high),
            max_cells=max_cells,
        )
        if isinstance(item, TurnoverDay):
            if item.available_at is None or item.float_shares is None:
                raise MissingPitTurnoverError(f"{bar.symbol} {bar.date}")
            fraction = (
                float(bar.volume) / float(item.float_shares) if item.float_shares > 0 else math.nan
            )
            e = _effective_exchange_fraction(fraction, arm)
        else:
            if item is None:
                raise MissingPitTurnoverError(f"{bar.symbol} {bar.date}")
            e = effective_exchange(float(item), arm)
        if density.sum() <= 0:
            density = _uniform_band(grid, step, float(bar.low), float(bar.high))
        else:
            density = density * (1.0 - e) + e * _uniform_band(
                grid, step, float(bar.low), float(bar.high)
            )
        mass = float(density.sum())
        if not math.isfinite(mass) or abs(mass - 1.0) > 1e-9:
            raise ArithmeticError("chip mass conservation violated")
        snapshots.append(ChipSnapshot(bar.date, grid.copy(), density.copy(), float(e)))
    return tuple(snapshots)


def _gaussian_kernel(sigma: float) -> np.ndarray:
    sigma = max(0.5, float(sigma))
    radius = max(1, math.ceil(4 * sigma))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    return kernel / kernel.sum()


def detect_peaks(
    density: Sequence[float],
    grid: Sequence[float],
    *,
    sigma_cells: float = 1.5,
    min_prominence: float = 0.05,
) -> tuple[tuple[float, float], ...]:
    values, axis = np.asarray(density, dtype=np.float64), np.asarray(grid, dtype=np.float64)
    if values.size != axis.size or not values.size:
        return ()
    kernel = _gaussian_kernel(sigma_cells)
    full = np.convolve(values, kernel, mode="full")
    start = (len(kernel) - 1) // 2
    smooth = full[start : start + len(values)]
    candidates = [
        i
        for i in range(1, len(smooth) - 1)
        if smooth[i] >= smooth[i - 1] and smooth[i] > smooth[i + 1] and smooth[i] > 0
    ]
    result = []
    for i in candidates:
        h = smooth[i]
        left = i - 1
        right = i + 1
        lmin = h
        rmin = h
        while left >= 0 and smooth[left] <= h:
            lmin = min(lmin, float(smooth[left]))
            left -= 1
        while right < len(smooth) and smooth[right] <= h:
            rmin = min(rmin, float(smooth[right]))
            right += 1
        contour = max(lmin if left >= 0 else 0.0, rmin if right < len(smooth) else 0.0)
        if h - contour >= min_prominence:
            width = max(1, math.ceil(2 * sigma_cells))
            mass = float(values[max(0, i - width) : min(len(values), i + width + 1)].sum())
            result.append((float(axis[i]), mass))
    return tuple(sorted(result, key=lambda x: x[0]))


def concentration(
    density: Sequence[float], grid: Sequence[float], *, band_pct: float = 0.10
) -> float:
    peaks = detect_peaks(density, grid, min_prominence=0.0)
    if not peaks:
        return 0.0
    main = max(peaks, key=lambda p: p[1])[0]
    values, axis = np.asarray(density), np.asarray(grid)
    return float(values[np.abs(axis - main) <= abs(main) * band_pct].sum())
