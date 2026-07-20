"""A-share price-limit rules shared by indicators, backtests, and APIs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import polars as pl

MAIN_BOARD_ST_LIMIT_CHANGE_DATE = date(2026, 7, 6)

MAIN_BOARD_LIMIT = 0.10
LEGACY_MAIN_BOARD_ST_LIMIT = 0.05
GROWTH_BOARD_LIMIT = 0.20
BEIJING_BOARD_LIMIT = 0.30


def is_risk_warning_name(name: str | None) -> bool:
    return "ST" in str(name or "").upper()


def board_limit_pct(symbol: str) -> float:
    if symbol.endswith(".BJ"):
        return BEIJING_BOARD_LIMIT
    if symbol.startswith(("300", "301", "688", "689")):
        return GROWTH_BOARD_LIMIT
    return MAIN_BOARD_LIMIT


def price_limit_pct(
    symbol: str,
    trade_date: date,
    *,
    is_risk_warning: bool = False,
) -> float:
    base = board_limit_pct(symbol)
    if (
        base == MAIN_BOARD_LIMIT
        and is_risk_warning
        and trade_date < MAIN_BOARD_ST_LIMIT_CHANGE_DATE
    ):
        return LEGACY_MAIN_BOARD_ST_LIMIT
    return base


def polars_price_limit_pct(
    symbol: pl.Expr,
    trade_date: pl.Expr,
    is_risk_warning: pl.Expr,
) -> pl.Expr:
    """Return a vectorized Polars expression for the effective daily limit."""
    is_growth = symbol.str.starts_with("300") | symbol.str.starts_with("301")
    is_star = symbol.str.starts_with("688") | symbol.str.starts_with("689")
    is_beijing = symbol.str.ends_with(".BJ")
    is_non_main = is_growth | is_star | is_beijing
    base = (
        pl.when(is_growth | is_star).then(GROWTH_BOARD_LIMIT)
        .when(is_beijing).then(BEIJING_BOARD_LIMIT)
        .otherwise(MAIN_BOARD_LIMIT)
    )
    legacy_main_st = (
        is_risk_warning.fill_null(False)
        & ~is_non_main
        & (trade_date < pl.lit(MAIN_BOARD_ST_LIMIT_CHANGE_DATE))
    )
    return (
        pl.when(legacy_main_st).then(LEGACY_MAIN_BOARD_ST_LIMIT)
        .otherwise(base)
        .cast(pl.Float64)
    )


def polars_is_risk_warning_name(name: pl.Expr) -> pl.Expr:
    """Return whether an instrument name contains the ST risk-warning marker."""
    return name.fill_null("").str.to_uppercase().str.contains("ST", literal=True)


def polars_limit_price(previous: pl.Expr, limit_pct: pl.Expr, *, up: bool) -> pl.Expr:
    """Calculate exchange half-up prices with integer-cent arithmetic."""
    sign = 1 if up else -1
    numerator = ((1 + sign * limit_pct) * 100).round(0).cast(pl.Int64)
    cents = (previous * 100 + 0.5).floor().cast(pl.Int64)
    return ((cents * numerator + 50) // 100) / 100


def numpy_limit_pct_vectors(
    symbols: Sequence[str],
    names: Sequence[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Return pre/post-change vectors once; callers select one per date."""
    current = np.fromiter(
        (board_limit_pct(str(symbol)) for symbol in symbols),
        dtype=np.float64,
        count=len(symbols),
    )
    legacy = current.copy()
    for asset_id, (_symbol, name) in enumerate(zip(symbols, names, strict=True)):
        if current[asset_id] == MAIN_BOARD_LIMIT and is_risk_warning_name(name):
            legacy[asset_id] = LEGACY_MAIN_BOARD_ST_LIMIT
    return legacy, current


def numpy_price_limit_matrix(
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
) -> np.ndarray:
    """Build a float32 time-by-asset matrix only for strategies that request it."""
    result = np.empty((len(trading_dates), len(symbols)), dtype=np.float32)
    return write_numpy_price_limit_matrix(result, trading_dates, symbols, names)


def write_numpy_price_limit_matrix(
    target: np.ndarray,
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
    *,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """Write date-aware limits directly into an existing matrix or memmap."""
    expected_shape = (len(trading_dates), len(symbols))
    if target.shape != expected_shape:
        raise ValueError("price-limit output shape mismatch")
    if valid is not None and valid.shape != expected_shape:
        raise ValueError("price-limit validity mask shape mismatch")

    legacy, current = numpy_limit_pct_vectors(symbols, names)
    target[:] = current.astype(np.float32, copy=False)
    legacy_rows = np.fromiter(
        (value < MAIN_BOARD_ST_LIMIT_CHANGE_DATE for value in trading_dates),
        dtype=bool,
        count=len(trading_dates),
    )
    if legacy_rows.any():
        target[legacy_rows] = legacy.astype(np.float32, copy=False)
    if valid is not None:
        target[~valid] = np.nan
    return target


def numpy_limit_price(
    previous: np.ndarray,
    limit_pct: np.ndarray,
    *,
    up: bool,
) -> np.ndarray:
    """NumPy counterpart of :func:`polars_limit_price`."""
    sign = 1 if up else -1
    numerator = np.rint((1.0 + sign * limit_pct) * 100.0).astype(np.int64)
    result = np.full(previous.shape, np.nan, dtype=np.float64)
    finite = np.isfinite(previous)
    cents = np.floor(previous[finite] * 100.0 + 0.5).astype(np.int64)
    result[finite] = (
        ((cents * numerator[finite] + 50) // 100).astype(np.float64) / 100.0
    )
    return result
