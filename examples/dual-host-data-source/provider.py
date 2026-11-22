"""TSP v1 view of the versioned observations distributed with the standalone source."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

SYMBOL = "600000.SH"
SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def _trade_date(value):
    if isinstance(value, datetime):
        value = value.astimezone(ZoneInfo("Asia/Shanghai")) if value.tzinfo else value
        return value.date()
    return value


class Provider:
    name = "example_archive"

    def __init__(self):
        self.config = SimpleNamespace(datasets={"daily": {}})
        latest = {}
        for bar in json.loads(
            Path(__file__).with_name("observations.json").read_bytes()
        ):
            if (
                bar["instrument_id"],
                bar["source_id"],
                bar["price_basis"],
                bar["quantity_unit"],
                bar["quote_currency"],
                bar["timeframe"],
            ) != ("demo.security", "example.archive", "raw", "share", "CNY", "1d"):
                raise ValueError("Unexpected observation declaration")
            day = date.fromisoformat(bar["trading_date"])
            revision = datetime.fromisoformat(bar["available_at"]), bar["revision_id"]
            if day not in latest or revision > latest[day][0]:
                latest[day] = (revision, bar)
        rows = []
        for day, (_, bar) in sorted(latest.items()):
            rows.append(
                {
                    "symbol": SYMBOL,
                    "date": day,
                    **{k: float(bar[k]) for k in ("open", "high", "low", "close")},
                    "volume": float(Decimal(bar["volume"]) / 100),
                    "amount": float(bar["turnover"]),
                }
            )
        self.frame = pl.from_dicts(rows, schema=SCHEMA)

    def get_daily(self, symbols, start_time=None, end_time=None, asset_type="stock"):
        if asset_type != "stock" or SYMBOL not in (symbols or ()):
            return self.frame.head(0)
        frame = self.frame
        if start_time is not None:
            frame = frame.filter(pl.col("date") >= _trade_date(start_time))
        if end_time is not None:
            frame = frame.filter(pl.col("date") <= _trade_date(end_time))
        return frame

    def test_dataset(self, dataset, symbols=None):
        if dataset != "daily":
            raise ValueError("Only stock daily bars are declared")
        frame = self.get_daily([SYMBOL] if symbols is None else symbols).head(5)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": frame.height,
            "columns": frame.columns,
            "preview": [
                {**row, "date": row["date"].isoformat()} for row in frame.to_dicts()
            ],
        }

    def close(self):
        pass
