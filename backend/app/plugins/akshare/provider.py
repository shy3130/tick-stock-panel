"""Explicit AKShare fallback provider for A-share market data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
import polars as pl

from app.data_providers.normalizer import cumulative_to_event_factors, normalize_daily, to_polars


@dataclass
class _AkShareConfig:
    name: str = "akshare"
    display_name: str = "AKShare (显式备用源)"
    datasets: dict = field(
        default_factory=lambda: {
            "daily": None,
            "adj_factor": None,
            "instruments": None,
        }
    )
    path: None = None
    builtin: bool = True


def _yyyymmdd(value: date | datetime | None) -> str | None:
    return value.strftime("%Y%m%d") if value else None


class AkShareProvider:
    """AKShare adapter. Callers must select it explicitly."""

    name = "akshare"
    builtin = True

    def __init__(self, client=None) -> None:
        self.config = _AkShareConfig()
        if client is None:
            import akshare as ak

            client = ak
        self._client = client

    def close(self) -> None:
        pass

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        if asset_type != "stock":
            return []
        raw = self._client.stock_zh_a_spot_em()
        if raw is None or raw.empty:
            return []
        rows: list[dict] = []
        for item in raw.to_dict(orient="records"):
            raw_code = item.get("代码")
            if raw_code is None or pd.isna(raw_code):
                continue
            code_text = str(raw_code).strip()
            if not code_text:
                continue
            code = code_text.zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            if code.startswith(("4", "8", "92")):
                exchange = "BJ"
            elif code.startswith("6"):
                exchange = "SH"
            else:
                exchange = "SZ"
            rows.append(
                {
                    "symbol": f"{code}.{exchange}",
                    "name": item.get("名称") or code,
                    "code": code,
                    "exchange": exchange,
                    "region": "CN",
                    "type": "stock",
                    "ext": {
                        "listing_date": None,
                        "delist_date": None,
                        "list_status": "L",
                        "market": None,
                        "universe_history": "current_only",
                    },
                }
            )
        return sorted(rows, key=lambda item: item["symbol"])

    def get_daily(
        self,
        symbols: list[str],
        start_time: date | datetime | None,
        end_time: date | datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        if asset_type != "stock":
            raise ValueError("AkShareProvider currently supports stock daily data only")
        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            raw = self._client.stock_zh_a_hist(
                symbol=symbol.split(".", 1)[0],
                period="daily",
                start_date=_yyyymmdd(start_time),
                end_date=_yyyymmdd(end_time),
                adjust="",
            )
            if raw is not None and not raw.empty:
                normalized_input = raw.rename(
                    columns={
                        "日期": "date",
                        "开盘": "open",
                        "最高": "high",
                        "最低": "low",
                        "收盘": "close",
                        "成交量": "volume",
                        "成交额": "amount",
                    }
                ).copy()
                normalized_input["symbol"] = symbol
                normalized_input["date"] = pd.to_datetime(
                    normalized_input["date"],
                    errors="coerce",
                ).dt.date
                normalized_input["quote_ts"] = None
                frame = normalize_daily(
                    normalized_input,
                    default_symbol=symbol,
                    source=self.name,
                )
                if not frame.is_empty():
                    frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, total)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: date | datetime | None,
        end_time: date | datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        if asset_type != "stock":
            raise ValueError("AkShareProvider currently supports stock factors only")
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            code, exchange = symbol.split(".", 1)
            raw = self._client.stock_zh_a_daily(
                symbol=f"{prefix.get(exchange, exchange.lower())}{code}",
                adjust="hfq-factor",
            )
            if raw is not None and not raw.empty:
                normalized_input = raw.rename(
                    columns={
                        "date": "trade_date",
                        "hfq_factor": "adj_factor",
                    }
                ).copy()
                normalized_input["symbol"] = symbol
                normalized_input["trade_date"] = pd.to_datetime(
                    normalized_input["trade_date"],
                    errors="coerce",
                ).dt.date
                frame = to_polars(normalized_input)
                if start_time is not None:
                    start_date = (
                        start_time.date() if isinstance(start_time, datetime) else start_time
                    )
                    frame = frame.filter(pl.col("trade_date") >= start_date)
                if end_time is not None:
                    end_date = end_time.date() if isinstance(end_time, datetime) else end_time
                    frame = frame.filter(pl.col("trade_date") <= end_date)
                frame = cumulative_to_event_factors(frame)
                if not frame.is_empty():
                    frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, total)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
