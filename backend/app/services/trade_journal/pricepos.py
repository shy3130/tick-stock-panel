"""买入日 20 日价格分位。"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.services.trade_journal.models import Fill


def build_price_lookup(fills: list[Fill], data_dir: Path) -> tuple[dict[tuple[str, str], dict], list[str]]:
    buy_symbols = {f.symbol for f in fills if f.side == "buy"}
    targets = {(f.symbol, f.date) for f in fills if f.side == "buy"}
    if not targets:
        return {}, sorted(buy_symbols)
    symbols = sorted({s for s, _ in targets})
    globs = _daily_globs(data_dir)
    if not globs:
        return {}, sorted(buy_symbols)
    try:
        scan = pl.concat([pl.scan_parquet(glob) for glob in globs], how="diagonal_relaxed")
        df = (
            scan
            .filter(pl.col("symbol").is_in(symbols))
            .select("symbol", "date", "close")
            .unique(subset=["symbol", "date"], keep="first")
            .sort(["symbol", "date"])
            .with_columns(
                rolling_low=pl.col("close").rolling_min(window_size=20).over("symbol"),
                rolling_high=pl.col("close").rolling_max(window_size=20).over("symbol"),
            )
            .collect()
        )
    except Exception:
        return {}, sorted(buy_symbols)

    out: dict[tuple[str, str], dict] = {}
    for row in df.iter_rows(named=True):
        key = (str(row["symbol"]), str(row["date"])[:10])
        if key not in targets:
            continue
        lo, hi, close = row["rolling_low"], row["rolling_high"], row["close"]
        if lo is None or hi is None or hi == lo:
            continue
        out[key] = {"pos_20d": (close - lo) / (hi - lo), "close": close}
    covered_symbols = {symbol for symbol, _ in out}
    uncovered = {symbol for symbol in symbols if symbol not in covered_symbols}
    return out, sorted(uncovered)


def _daily_globs(data_dir: Path) -> list[str]:
    globs: list[str] = []
    for name in (
        "kline_daily_enriched",
        "kline_etf_enriched",
        "kline_hk_enriched",
        "kline_daily",
        "kline_etf_daily",
        "kline_hk_daily",
    ):
        root = data_dir / name
        if root.exists() and next(root.glob("**/*.parquet"), None) is not None:
            globs.append(str(root / "**" / "*.parquet"))
    return globs
