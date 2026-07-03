from __future__ import annotations

import numpy as np
import polars as pl


def compute_factor(panel: pl.DataFrame, factor_name: str) -> pl.DataFrame:
    if factor_name == "alpha101_001":
        return alpha101_001(panel)
    return panel


def alpha101_001(panel: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for group in panel.sort(["symbol", "date"]).partition_by("symbol", as_dict=False):
        symbol = group["symbol"][0]
        dates = group["date"].to_list()
        close = group["close"].cast(pl.Float64).to_numpy()
        returns = np.full(len(close), np.nan)
        returns[1:] = close[1:] / close[:-1] - 1
        rolling_std = _rolling_std(returns, 20)
        cond = (returns < 0).astype(float)
        x = rolling_std * cond + close * (1.0 - cond)
        argmax = _rolling_argmax(np.sign(x) * np.abs(x) ** 2, 5)
        rows.extend({"symbol": symbol, "date": d, "_alpha_raw": v} for d, v in zip(dates, argmax))

    if not rows:
        return panel.with_columns(pl.lit(None).cast(pl.Float64).alias("alpha101_001"))
    alpha = (
        pl.DataFrame(rows)
        .with_columns(
            pl.when(pl.col("_alpha_raw").is_nan()).then(None).otherwise(pl.col("_alpha_raw")).alias("_alpha_raw")
        )
        .with_columns(
            (pl.col("_alpha_raw").rank(method="average").over("date") / pl.col("_alpha_raw").count().over("date") - 0.5)
            .alias("alpha101_001")
        )
        .select("symbol", "date", "alpha101_001")
    )
    return panel.join(alpha, on=["symbol", "date"], how="left")


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        win = values[i - window + 1:i + 1]
        if np.isfinite(win).all():
            out[i] = np.std(win, ddof=1)
    return out


def _rolling_argmax(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        win = values[i - window + 1:i + 1]
        if not np.isfinite(win).all():
            continue
        out[i] = float(np.nanargmax(win))
    return out
