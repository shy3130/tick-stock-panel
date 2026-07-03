"""买入日 20 日价格分位。"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.services.trade_journal.models import Fill


def build_price_lookup(fills: list[Fill], data_dir: Path) -> dict[tuple[str, str], dict]:
    targets = {(f.symbol, f.date) for f in fills if f.side == "buy" and not f.symbol.endswith(".HK")}
    if not targets:
        return {}
    symbols = sorted({s for s, _ in targets})
    glob = _daily_glob(data_dir)
    if glob is None:
        return {}
    try:
        df = (
            pl.scan_parquet(glob)
            .filter(pl.col("symbol").is_in(symbols))
            .select("symbol", "date", "close")
            .sort(["symbol", "date"])
            .with_columns(
                rolling_low=pl.col("close").rolling_min(window_size=20).over("symbol"),
                rolling_high=pl.col("close").rolling_max(window_size=20).over("symbol"),
            )
            .collect()
        )
    except Exception:
        return {}

    out: dict[tuple[str, str], dict] = {}
    for row in df.iter_rows(named=True):
        key = (str(row["symbol"]), str(row["date"])[:10])
        if key not in targets:
            continue
        lo, hi, close = row["rolling_low"], row["rolling_high"], row["close"]
        if lo is None or hi is None or hi == lo:
            continue
        out[key] = {"pos_20d": (close - lo) / (hi - lo), "close": close}
    return out


def _daily_glob(data_dir: Path) -> str | None:
    for name in ("kline_daily_enriched", "kline_daily"):
        root = data_dir / name
        if root.exists():
            return str(root / "**" / "*.parquet")
    return None
