"""TDX disk CSV client with EngineDataClient-compatible methods."""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)
_MARKET = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}


def _tdx_name(symbol: str) -> tuple[str, str, int]:
    code, _, exchange = symbol.partition(".")
    market = _MARKET.get(exchange.upper(), exchange.lower() or "sh")
    if market == "hk":
        code = code.zfill(5)
        return market, f"{market}{code}", 4
    return market, f"{market}{code}", 5


def _csv_path(base: Path, dataset: str, symbol: str) -> Path:
    _, name, group_len = _tdx_name(symbol)
    return base / dataset / name[:group_len] / f"{name}.csv"


def _dated_csv_path(base: Path, dataset: str, symbol: str, date_yyyymmdd: str) -> Path:
    _, name, _ = _tdx_name(symbol)
    return base / dataset / date_yyyymmdd[:4] / date_yyyymmdd / f"{name}.csv"


def _symbol_from_code(code: str, asset_type: str | None = None) -> str:
    if "." in code:
        return code
    if asset_type == "hk":
        return f"{code}.HK"
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


class EngineDataDiskClient:
    def __init__(self) -> None:
        self.base = Path(os.environ.get("TDX_DATA_DIR", "/Volumes/vol3/tdx"))

    def _read(self, dataset: str, symbol_or_code: str, asset_type: str | None = None) -> pl.DataFrame:
        path = _csv_path(self.base, dataset, _symbol_from_code(symbol_or_code, asset_type))
        if not path.exists():
            logger.debug("TDX disk csv missing: %s", path)
            return pl.DataFrame()
        try:
            return pl.read_csv(path, infer_schema_length=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TDX disk csv read failed %s: %s", path, exc)
            return pl.DataFrame()

    def get_wide(self, code: str, limit: int = 250, asset_type: str | None = None) -> list[dict]:
        df = self._read("wide", code, asset_type)
        if df.is_empty():
            logger.debug("TDX disk wide missing for %s; falling back to day", code)
            df = self._read("day", code, asset_type)
        if df.is_empty():
            return []
        return df.tail(limit).reverse().with_columns(pl.col("date").cast(pl.Utf8)).to_dicts()

    def get_day(self, code: str, limit: int = 250, asset_type: str | None = None) -> list[dict]:
        df = self._read("day", code, asset_type)
        if df.is_empty():
            return []
        return df.tail(limit).reverse().with_columns(pl.col("date").cast(pl.Utf8)).to_dicts()

    def get_xdxr(self, code: str, limit: int = 100, asset_type: str | None = None) -> list[dict]:
        df = self._read("xdxr", code, asset_type)
        if df.is_empty():
            return []
        rows = []
        for row in df.tail(limit).iter_rows(named=True):
            rows.append({
                "date": str(row.get("Date")),
                "category": int(row.get("Category") or 0),
                "name": row.get("Name"),
                "fenhong": float(row.get("FenHong") or 0),
                "fenshu": float(row.get("FenShu") or 0),
                "songzhuangu": float(row.get("SongZhuanGu") or 0),
                "peigu": float(row.get("PeiGu") or 0),
                "peigujia": float(row.get("PeiGuJia") or 0),
            })
        return rows

    def _read_dated(
        self,
        dataset: str,
        symbol_or_code: str,
        date_yyyymmdd: str,
        asset_type: str | None = None,
    ) -> pl.DataFrame:
        path = _dated_csv_path(self.base, dataset, _symbol_from_code(symbol_or_code, asset_type), date_yyyymmdd)
        if not path.exists():
            logger.debug("TDX disk dated csv missing: %s", path)
            return pl.DataFrame()
        try:
            return pl.read_csv(path, infer_schema_length=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TDX disk dated csv read failed %s: %s", path, exc)
            return pl.DataFrame()

    def get_minutes(
        self,
        code: str,
        date_yyyymmdd: str,
        limit: int = 5000,
        asset_type: str | None = None,
    ) -> list[dict]:
        df = self._read_dated("minutes", code, date_yyyymmdd, asset_type)
        if df.is_empty():
            return []
        return (
            df.head(limit)
            .rename({"Price": "price", "Vol": "_volume_hand"})
            .with_columns((pl.col("_volume_hand").cast(pl.Float64, strict=False) * 100).alias("volume"))
            .select("price", "volume")
            .to_dicts()
        )

    def get_trans(
        self,
        code: str,
        date_yyyymmdd: str,
        limit: int = 5000,
        asset_type: str | None = None,
    ) -> list[dict]:
        df = self._read_dated("trans", code, date_yyyymmdd, asset_type)
        if df.is_empty():
            return []
        return (
            df.head(limit)
            .rename({"vol": "volume", "num": "order_count", "buyorsell": "direction"})
            .select("time", "price", "volume", "amount", "order_count", "direction")
            .to_dicts()
        )

    def get_fund_daily(self, code: str, date_iso: str, asset_type: str | None = None) -> dict:
        df = self._read("fund", code, asset_type)
        if df.is_empty():
            return {}
        rows = df.filter(pl.col("Date").cast(pl.Utf8) == date_iso).to_dicts()
        if not rows:
            return {}
        row = rows[-1]
        main = float(row.get("Main") or 0)
        medium = float(row.get("Medium") or 0)
        small = float(row.get("Small") or 0)
        return {
            "main_net": main,
            "total_net": main + medium + small,
            "super_large_net": float(row.get("SuperLarge") or 0),
            "large_net": float(row.get("Large") or 0),
            "medium_net": medium,
            "small_net": small,
            "main_ratio": float(row.get("MainRatio") or 0),
            "super_large_ratio": float(row.get("SuperLargeRatio") or 0),
            "large_ratio": float(row.get("LargeRatio") or 0),
            "medium_ratio": float(row.get("MediumRatio") or 0),
            "small_ratio": float(row.get("SmallRatio") or 0),
        }

    def get_fund_range(self, code: str, start_iso: str, end_iso: str, asset_type: str | None = None) -> pl.DataFrame:
        """区间资金流查询：一次性读取该标的历史 CSV，按日期过滤到闭区间。"""
        df = self._read("fund", code, asset_type)
        if df.is_empty() or "Date" not in df.columns or "Main" not in df.columns:
            return pl.DataFrame()
        return (
            df.filter(
                (pl.col("Date").cast(pl.Utf8) >= start_iso)
                & (pl.col("Date").cast(pl.Utf8) <= end_iso)
            )
            .select(
                pl.col("Date").cast(pl.Utf8).alias("date"),
                pl.col("Main").cast(pl.Float64).alias("main_net_inflow"),
            )
            .sort("date")
        )

    def freshness(self, code: str = "600519") -> date | None:
        rows = self.get_wide(code, limit=1)
        if not rows:
            return None
        return date.fromisoformat(str(rows[0]["date"]))
