"""Use the existing corporate-action factors with the compatibility backtester."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

import numpy as np
import polars as pl
from tivelo_backtest.research.matrix import LEGACY_PRICE_SCALE

from app.services.minute_adjust import PRICE_COLS, _load_factor_frames

if TYPE_CHECKING:
    from app.backtest.matrix import MarketDataMatrix

BacktestPriceBasis = Literal["qfq", "hfq", "dynamic"]


class BackwardAdjustment:
    """Project forward-adjusted input with the factors used by the daily pipeline.

    QFQ = raw × cumulative / total. HFQ = QFQ × total. The cumulative
    factor lets the existing matcher work in exchange prices for lots and fills.
    """

    def __init__(self, root: Path, asset_type: str):
        self.cumulative, self.totals = _load_factor_frames(root, asset_type, positive_only=True)
        if not self.cumulative.is_empty() and self.cumulative.filter(
            ~pl.col("cum").is_finite() | (pl.col("cum") <= 0)
        ).height:
            raise ValueError("复权因子包含无效值，请检查日线因子数据")

    def panel(self, panel: pl.DataFrame) -> pl.DataFrame:
        if panel.is_empty() or self.totals.is_empty():
            return panel
        return (
            panel.sort(["symbol", "date"])
            .join_asof(
                self.cumulative,
                left_on="date",
                right_on="trade_date",
                by="symbol",
                check_sortedness=False,
            )
            .join(self.totals, on="symbol", how="left", maintain_order="left")
            .with_columns(
                (pl.col(name) * pl.col("total").fill_null(1.0)).alias(name)
                for name in PRICE_COLS if name in panel.columns
            )
            .with_columns(pl.col("cum").fill_null(1.0).alias(LEGACY_PRICE_SCALE))
            .drop("trade_date", "cum", "total")
        )

    def matrix(self, market: MarketDataMatrix) -> MarketDataMatrix:
        by_symbol = dict(self.totals.iter_rows())
        if not any(symbol in by_symbol for symbol in market.symbols):
            return market
        totals = np.asarray([by_symbol.get(symbol, 1.0) for symbol in market.symbols])
        totals = totals.astype(market.close.dtype, copy=False)
        days = np.asarray([label[:10] for label in market.timestamp_labels], dtype="datetime64[D]")
        scales = np.ones(market.shape, dtype=np.float32)
        indices = {symbol: index for index, symbol in enumerate(market.symbols)}
        for (symbol,), events in self.cumulative.partition_by("symbol", as_dict=True).items():
            index = indices.get(symbol)
            if index is None:
                continue
            dates = events["trade_date"].to_numpy()
            positions = np.searchsorted(dates, days, side="right")
            values = np.r_[1.0, events["cum"].to_numpy()]
            scales[:, index] = values[positions]
        prices = {name: getattr(market, name) * totals for name in PRICE_COLS}
        for values in (*prices.values(), scales):
            values.setflags(write=False)
        fields = MappingProxyType({**market.fields, LEGACY_PRICE_SCALE: scales})
        return replace(market, **prices, fields=fields)
