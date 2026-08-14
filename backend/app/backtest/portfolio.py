from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl


def load_price_panel(
    repo,
    symbols: list[str],
    start: date,
    end: date,
    *,
    raise_on_error: bool = False,
) -> tuple[pl.DataFrame, list[str]]:
    """Load canonical closes into a date-aligned frame: ``date`` + symbol columns.

    ``raise_on_error`` is reserved for callers that must distinguish an
    unavailable repository from a valid query with no canonical rows.  The
    default remains fail-soft for portfolio consumers.
    """
    from app.api.kline import _asset_type_for_symbol

    frames = []
    for symbol in symbols:
        asset_type = _asset_type_for_symbol(symbol)
        try:
            kwargs = {"raise_on_error": True} if raise_on_error else {}
            df = repo.get_daily_asset(
                asset_type,
                symbol,
                start,
                end,
                ["symbol", "date", "close"],
                **kwargs,
            )
        except Exception:
            if raise_on_error:
                raise
            df = None
        if df is not None and not df.is_empty():
            frames.append(df.select(["symbol", "date", "close"]))

    if not frames:
        return pl.DataFrame({"date": []}, schema={"date": pl.Date}), []

    wide = (
        pl.concat(frames, how="vertical_relaxed")
        .pivot(index="date", on="symbol", values="close")
        .sort("date")
        .drop_nulls()
    )
    kept = [symbol for symbol in symbols if symbol in wide.columns]
    return wide.select(["date", *kept]), kept


def load_price_matrix(repo, symbols: list[str], start: date, end: date) -> tuple[np.ndarray, list[str]]:
    """Load close prices into a date-aligned [T,N] matrix."""
    wide, kept = load_price_panel(repo, symbols, start, end)
    if not kept or wide.height == 0:
        return np.empty((0, len(kept)), dtype=float), kept
    return wide.select(kept).to_numpy().astype(float), kept


def returns_from_prices(prices: np.ndarray) -> np.ndarray:
    """Convert [T,N] prices to [T-1,N] simple daily returns."""
    if prices.ndim != 2 or prices.shape[0] < 2:
        n = prices.shape[1] if prices.ndim == 2 else 0
        return np.empty((0, n), dtype=float)
    return prices[1:] / prices[:-1] - 1.0


def momentum_from_prices(prices: np.ndarray) -> np.ndarray:
    """Cumulative momentum, used as the default score_weight signal."""
    if prices.ndim != 2 or prices.shape[0] < 1 or prices.shape[1] == 0:
        return np.empty((0,), dtype=float)
    return prices[-1] / prices[0] - 1.0
