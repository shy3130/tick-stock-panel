"""Stable read-only market data facade exposed to extensions and ExtCollectors."""
from __future__ import annotations

import contextlib
import logging
import math
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)


def _to_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _normalize_tickflow_quote(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return {
        "symbol": symbol,
        "name": item.get("name"),
        "last_price": _to_finite_float(item.get("last_price")),
        "prev_close": _to_finite_float(item.get("prev_close")),
        "open": _to_finite_float(item.get("open")),
        "high": _to_finite_float(item.get("high")),
        "low": _to_finite_float(item.get("low")),
        "volume": _to_finite_float(item.get("volume")),
        "amount": _to_finite_float(item.get("amount")),
        "timestamp": item.get("timestamp"),
    }


class CoreMarketAccess:
    """Default ``MarketAccess`` backed by capability routing and local kline store."""

    def __init__(self, data_dir: Path, repository: Any = None) -> None:
        self._data_dir = Path(data_dir)
        self._repository = repository

    def get_realtime_stocks(self) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """Fetch full-market realtime snapshots via the active realtime provider."""
        from app.services import preferences

        provider_name = preferences.get_realtime_data_provider()
        configured = str(preferences.load().get("realtime_data_provider") or "").strip().lower()
        if configured and configured != "tickflow" and provider_name == "tickflow":
            provider_name = configured

        if provider_name != "tickflow":
            from app.data_providers import custom as custom_sources

            if not custom_sources.provider_has_dataset(provider_name, "realtime"):
                raise RuntimeError(
                    f"当前实时数据源 '{provider_name}' 不可用或未声明 realtime 能力"
                )
            provider = custom_sources.get_provider(provider_name)
            records = provider.get_realtime() or []
            meta = getattr(provider, "last_realtime_meta", None)
            partial_errors: tuple[str, ...] = ()
            if isinstance(meta, dict) and meta.get("failed_batches"):
                raw_errors = [str(e) for e in (meta.get("errors") or []) if e]
                if not raw_errors:
                    raw_errors = [
                        f"{meta['failed_batches']}/{meta.get('total_batches', '?')} 批快照拉取失败"
                    ]
                partial_errors = tuple(raw_errors)
                if not records:
                    raise RuntimeError(
                        f"全市场实时快照拉取失败: {'; '.join(partial_errors[:3])}"
                    )
            if not records:
                raise RuntimeError(f"实时数据源 '{provider_name}' 未返回任何行情快照")
            return records, partial_errors

        from app.tickflow.client import get_paid_realtime_client

        tf = get_paid_realtime_client()
        if tf is None:
            raise RuntimeError("当前未配置可用的实时行情数据源")
        resp = tf.quotes.get_by_universes(universes=["CN_Equity_A"]) or []
        records = [
            rec for item in resp
            if (rec := _normalize_tickflow_quote(item)) is not None
        ]
        if not records:
            raise RuntimeError("TickFlow 未返回任何 A 股实时行情快照")
        return records, ()

    def get_vol_ma5_baseline(
        self, target_date: date, *, lookback_days: int = 5,
    ) -> dict[str, float]:
        """Load average daily volume (in lots/手) strictly before ``target_date``."""
        if lookback_days < 1:
            return {}
        if self._repository is not None and hasattr(self._repository, "execute_all"):
            try:
                rows = self._repository.execute_all(
                    """
                    WITH ranked AS (
                        SELECT symbol, volume,
                               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                        FROM kline_daily
                        WHERE date < ? AND volume IS NOT NULL AND volume > 0
                    )
                    SELECT symbol, AVG(volume) AS vol_ma5
                    FROM ranked
                    WHERE rn <= ?
                    GROUP BY symbol
                    """,
                    [target_date, lookback_days],
                )
                return {
                    str(sym).upper(): float(avg_v)
                    for sym, avg_v in rows
                    if sym and avg_v is not None and math.isfinite(float(avg_v)) and float(avg_v) > 0
                }
            except Exception as exc:  # noqa: BLE001 — fallback to direct parquet scan
                logger.debug("CoreMarketAccess DuckDB vol_ma5 回退 Polars: %s", exc)

        daily_dir = self._data_dir / "kline_daily"
        if not daily_dir.exists():
            return {}
        parquet_files = sorted(daily_dir.rglob("*.parquet"))
        if not parquet_files:
            return {}
        try:
            lf = pl.scan_parquet([str(p) for p in parquet_files])
            df = (
                lf.select(["symbol", "date", "volume"])
                .with_columns(
                    pl.col("symbol").cast(pl.Utf8).str.to_uppercase(),
                    pl.col("date").cast(pl.Date),
                    pl.col("volume").cast(pl.Float64),
                )
                .filter(
                    (pl.col("date") < pl.lit(target_date))
                    & pl.col("volume").is_not_null()
                    & (pl.col("volume") > 0)
                )
                .sort(["symbol", "date"])
                .group_by("symbol")
                .agg(pl.col("volume").tail(lookback_days).mean().alias("vol_ma5"))
                .collect()
            )
            return {
                row["symbol"]: float(row["vol_ma5"])
                for row in df.to_dicts()
                if row["symbol"] and row["vol_ma5"] is not None
                and math.isfinite(float(row["vol_ma5"])) and float(row["vol_ma5"]) > 0
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("CoreMarketAccess 读取本地日K基准量失败: %s", exc)
            return {}

    def get_name_map(self, symbols: list[str] | None = None) -> dict[str, str]:
        if self._repository is None or not hasattr(self._repository, "get_name_map"):
            return {}
        try:
            return self._repository.get_name_map(symbols) or {}
        except TypeError:
            try:
                return self._repository.get_name_map() or {}
            except Exception:  # noqa: BLE001
                return {}
        except Exception:  # noqa: BLE001
            return {}

    def get_excluded_non_stock_symbols(self) -> set[str]:
        excluded: set[str] = set()
        if self._repository is None:
            return excluded
        if hasattr(self._repository, "get_index_symbol_set"):
            with contextlib.suppress(Exception):
                excluded.update(
                    str(s).upper() for s in self._repository.get_index_symbol_set() if s
                )
        if hasattr(self._repository, "get_etf_instruments"):
            with contextlib.suppress(Exception):
                etf_df = self._repository.get_etf_instruments()
                if etf_df is not None and not etf_df.is_empty() and "symbol" in etf_df.columns:
                    excluded.update(
                        str(s).upper() for s in etf_df["symbol"].cast(pl.Utf8).to_list() if s
                    )
        return excluded
