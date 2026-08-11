"""Shared Tushare opening-auction snapshot I/O for research runners."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from research.paths import DATA_DIR
from scripts.tushare_sync import TushareClient, _frame_from_tushare


AUCTION_FIELDS = (
    "ts_code,trade_date,vol,price,amount,pre_close,turnover_rate,volume_ratio,float_share"
)


def auction_snapshot_path(trading_day: date) -> Path:
    return DATA_DIR / "tushare_auction" / f"date={trading_day.isoformat()}" / "open.parquet"


def _atomic_parquet(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)


def fetch_auction_snapshot(
    *,
    trading_day: date,
    token: str,
    api_base: str,
) -> tuple[pl.DataFrame, Path]:
    if not token:
        raise ValueError("TUSHARE_TOKEN is required to fetch an auction snapshot")
    client = TushareClient(base_url=api_base, token=token)
    response = client.post(
        "stk_auction",
        {"trade_date": trading_day.strftime("%Y%m%d"), "ts_type": "STK"},
        fields=AUCTION_FIELDS,
    )
    auction = _frame_from_tushare(response)
    if auction.is_empty():
        raise ValueError("Tushare stk_auction returned no rows")
    required = {"ts_code", "trade_date", "price", "amount", "pre_close"}
    missing = sorted(required - set(auction.columns))
    if missing:
        raise ValueError(f"auction snapshot missing columns: {missing}")
    duplicates = auction.select(pl.struct(["ts_code", "trade_date"]).is_duplicated().sum()).item()
    if duplicates:
        raise ValueError(f"auction snapshot has {duplicates} duplicate symbol/date rows")
    path = auction_snapshot_path(trading_day)
    _atomic_parquet(path, auction)
    return auction, path


def load_or_fetch_auction_snapshot(
    *,
    trading_day: date,
    token: str,
    api_base: str,
    refresh: bool = False,
) -> tuple[pl.DataFrame, Path, str]:
    path = auction_snapshot_path(trading_day)
    if path.is_file() and not refresh:
        return pl.read_parquet(path), path, "local_snapshot"
    frame, path = fetch_auction_snapshot(
        trading_day=trading_day,
        token=token,
        api_base=api_base,
    )
    return frame, path, "tushare_stk_auction"
