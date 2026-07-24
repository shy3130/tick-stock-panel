"""将 longbridge ClickHouse 行情映射为 TickFlow Provider 契约。"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.data_providers.base import ProviderCapabilities
from app.data_providers.normalizer import normalize_daily
from app.market_rules import market_rule_for_symbol, round_lot_size
from app.plugins.clickhouse import bridge
from app.plugins.clickhouse.financial import (
    FINANCIAL_FIELDS,
    FINANCIAL_TABLES,
    build_financial_frames,
)

QueryFn = Callable[[str], list[dict]]
MinuteFallbackFn = Callable[[str], list[dict]]
DailyFallbackFn = Callable[[str], list[dict]]
_MINUTE_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
_DAILY_SYMBOL_BATCH_SIZE = 500
_FINANCIAL_SYMBOL_BATCH_SIZE = 1000
logger = logging.getLogger(__name__)


@dataclass
class _ClickHouseConfig:
    name: str = "clickhouse"
    display_name: str = "Longbridge ClickHouse - 三市场"
    datasets: dict = field(
        default_factory=lambda: dict.fromkeys(("daily", "minute", "realtime", "financial"))
    )
    path: None = None
    builtin: bool = True


def _sql_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _values_sql(values: list[str] | tuple[str, ...]) -> str:
    return "(" + ", ".join(_sql_string(value) for value in values) + ")"


def _symbols_sql(symbols: list[str]) -> str:
    return _values_sql([symbol.upper() for symbol in symbols])


def _storage_symbol_aliases(symbols: list[str]) -> dict[str, str]:
    """Map WebStock storage aliases back to requested canonical symbols."""
    aliases: dict[str, str] = {}
    for value in symbols:
        requested = str(value).strip().upper()
        if not requested:
            continue
        aliases.setdefault(requested, requested)
        if requested.endswith(".HK"):
            code = requested[:-3]
            if code.isdigit():
                aliases.setdefault(f"{int(code)}.HK", requested)
    return aliases


def _date_filter(column: str, start: datetime | None, end: datetime | None) -> str:
    parts: list[str] = []
    if start is not None:
        parts.append(f"{column} >= toDate({_sql_string(start.date().isoformat())})")
    if end is not None:
        parts.append(f"{column} <= toDate({_sql_string(end.date().isoformat())})")
    return " AND " + " AND ".join(parts) if parts else ""


def _as_shanghai_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _minute_local_time(value: object, symbol: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo(market_rule_for_symbol(symbol).timezone)).replace(tzinfo=None)


def _longbridge_minute_local_time(value: object, symbol: str) -> datetime:
    """Longbridge API emits naive timestamps in Asia/Shanghai wall-clock time."""
    return (
        _as_shanghai_time(value)
        .astimezone(ZoneInfo(market_rule_for_symbol(symbol).timezone))
        .replace(tzinfo=None)
    )


def _fetch_longbridge_intraday(symbol: str) -> list[dict]:
    endpoint = os.getenv("LONGBRIDGE_API_URL", "").strip().rstrip("/")
    if not endpoint:
        return []
    timeout = float(os.getenv("LONGBRIDGE_API_TIMEOUT_SECONDS", "20"))
    try:
        response = httpx.get(
            f"{endpoint}/api/stocks/{quote(symbol, safe='')}/klines",
            params={"period": "1m", "limit": 1200},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Longbridge minute fallback failed for %s: %s", symbol, exc)
        return []
    bars = payload.get("bars") if isinstance(payload, dict) else None
    return bars if isinstance(bars, list) else []


def _fetch_longbridge_daily(symbol: str) -> list[dict]:
    endpoint = os.getenv("LONGBRIDGE_API_URL", "").strip().rstrip("/")
    if not endpoint:
        return []
    timeout = float(os.getenv("LONGBRIDGE_API_TIMEOUT_SECONDS", "20"))
    try:
        response = httpx.get(
            f"{endpoint}/api/stocks/{quote(symbol, safe='')}/klines",
            params={"period": "day", "limit": 1000},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Longbridge daily fallback failed for %s: %s", symbol, exc)
        return []
    bars = payload.get("bars") if isinstance(payload, dict) else None
    return bars if isinstance(bars, list) else []


class ClickHouseProvider:
    name = "clickhouse"
    builtin = True
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        minute=True,
        realtime=True,
        financial=True,
    )

    def __init__(
        self,
        query_fn: QueryFn | None = None,
        minute_fallback_fn: MinuteFallbackFn | None = None,
        daily_fallback_fn: DailyFallbackFn | None = None,
    ) -> None:
        self.config = _ClickHouseConfig()
        self._query_fn = query_fn or bridge.query_json_each_row
        self._minute_fallback_fn = minute_fallback_fn or _fetch_longbridge_intraday
        self._daily_fallback_fn = daily_fallback_fn or _fetch_longbridge_daily
        self.last_sql = ""
        self._financial_cache_key: tuple[str, ...] | None = None
        self._financial_cache_time = 0.0
        self._financial_frames: dict[str, pl.DataFrame] = {}

    def close(self) -> None:
        self._financial_cache_key = None
        self._financial_frames = {}

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        if table not in FINANCIAL_TABLES:
            raise ValueError(f"unsupported financial table: {table}")
        key = tuple(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))
        if not key:
            return pl.DataFrame()
        now = time.monotonic()
        if key != self._financial_cache_key or now - self._financial_cache_time > 300:
            rows: list[dict[str, Any]] = []
            fields_sql = _values_sql(FINANCIAL_FIELDS)
            for offset in range(0, len(key), _FINANCIAL_SYMBOL_BATCH_SIZE):
                symbol_chunk = list(key[offset : offset + _FINANCIAL_SYMBOL_BATCH_SIZE])
                sql = f"""
                    SELECT symbol, report_period, fp_end, field, value, yoy, currency
                    FROM {self._table("lb_financial_report")} FINAL
                    WHERE symbol IN {_symbols_sql(symbol_chunk)}
                      AND field IN {fields_sql}
                    ORDER BY symbol, field, fp_end DESC, updated_at DESC
                    LIMIT 1 BY symbol, field
                """
                rows.extend(self._query(sql))
            self._financial_frames = build_financial_frames(rows)
            self._financial_cache_key = key
            self._financial_cache_time = now
        return self._financial_frames[table].clone()

    def _query(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return self._query_fn(sql)

    @staticmethod
    def _table(name: str) -> str:
        return f"{bridge.database_identifier()}.{name}"

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        symbol_chunks = [
            symbols[offset:offset + _DAILY_SYMBOL_BATCH_SIZE]
            for offset in range(0, len(symbols), _DAILY_SYMBOL_BATCH_SIZE)
        ]
        frames: list[pl.DataFrame] = []
        for index, symbol_chunk in enumerate(symbol_chunks, start=1):
            symbol_aliases = _storage_symbol_aliases(symbol_chunk)
            sql = f"""
                SELECT symbol, market, trade_date, open, high, low, close,
                       volume, turnover AS amount
                FROM {self._table("lb_daily_bars")}
                WHERE adjusted = 1
                  AND symbol IN {_symbols_sql(list(symbol_aliases))}
                  {_date_filter("trade_date", start_time, end_time)}
                ORDER BY symbol, trade_date
            """
            rows = self._query(sql)
            normalized_rows = [
                dict(
                    row,
                    symbol=symbol_aliases.get(str(row.get("symbol") or "").upper()),
                    amount=row.get("amount", row.get("turnover")),
                )
                for row in rows
                if str(row.get("symbol") or "").upper() in symbol_aliases
            ]
            frame = normalize_daily(normalized_rows, source=self.name)
            if not frame.is_empty():
                frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, len(symbol_chunks))
        if asset_type == "index":
            returned_symbols = {
                str(symbol).upper()
                for frame in frames
                for symbol in frame.get_column("symbol").to_list()
            }
            for symbol in symbols:
                normalized_symbol = str(symbol).upper()
                if not normalized_symbol.endswith((".HK", ".US")):
                    continue
                if normalized_symbol in returned_symbols:
                    continue
                fallback_rows = self._daily_fallback_fn(normalized_symbol)
                if not fallback_rows:
                    continue
                normalized_fallback_rows = [
                    dict(
                        row,
                        symbol=normalized_symbol,
                        amount=row.get("amount", row.get("turnover")),
                    )
                    for row in fallback_rows
                ]
                fallback_frame = normalize_daily(normalized_fallback_rows, source="longbridge_api")
                if start_time is not None and not fallback_frame.is_empty():
                    fallback_frame = fallback_frame.filter(pl.col("date") >= start_time.date())
                if end_time is not None and not fallback_frame.is_empty():
                    fallback_frame = fallback_frame.filter(pl.col("date") <= end_time.date())
                if not fallback_frame.is_empty():
                    frames.append(fallback_frame)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def _query_minute_rows(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        freq: str,
    ) -> list[dict]:
        if not symbols:
            return []
        # trade_date_local in the collector is the Asia/Shanghai calendar date.
        # US sessions cross that boundary, so query a guard day on both sides and
        # apply the requested date after converting each bar to its market time.
        query_start = start_time - timedelta(days=1) if start_time is not None else None
        query_end = end_time + timedelta(days=1) if end_time is not None else None
        symbol_aliases = _storage_symbol_aliases(symbols)
        sql = f"""
            SELECT symbol, market, bar_time_utc, open, high, low, close, volume, amount,
                   source_priority
            FROM (
                SELECT symbol, market, toTimeZone(bar_time, 'UTC') AS bar_time_utc,
                       open, high, low, close, volume, turnover AS amount,
                       3 AS source_priority
                FROM {self._table("lb_realtime_candlesticks")} FINAL
                WHERE period = 'min_1'
                  AND symbol IN {_symbols_sql(list(symbol_aliases))}
                  {_date_filter("bar_time", query_start, query_end)}
                UNION ALL
                SELECT symbol, market, bar_time_utc, open, high, low, close, volume, amount,
                       2 AS source_priority
                FROM {self._table("lb_minute_bars")}
                WHERE frequency = {_sql_string(freq)}
                  AND symbol IN {_symbols_sql(list(symbol_aliases))}
                  {_date_filter("trade_date_local", query_start, query_end)}
                UNION ALL
                SELECT symbol, market, toTimeZone(line_time, 'UTC') AS bar_time_utc,
                       price AS open, price AS high, price AS low, price AS close,
                       volume, turnover AS amount, 1 AS source_priority
                FROM {self._table("lb_intraday_lines")}
                WHERE symbol IN {_symbols_sql(list(symbol_aliases))}
                  {_date_filter("line_time", query_start, query_end)}
            )
            ORDER BY symbol, bar_time_utc
        """
        return self._query(sql)

    @staticmethod
    def _minute_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        frame = pl.DataFrame(rows) if rows else pl.DataFrame()
        if frame.is_empty():
            return frame
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in frame.columns:
                frame = frame.with_columns(pl.col(column).cast(pl.Float64, strict=False))
        selected_columns = [column for column in _MINUTE_COLUMNS if column in frame.columns]
        return frame.select(selected_columns).sort(["symbol", "datetime"])

    def _normalize_minute_query_rows(
        self,
        rows: list[dict],
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> pl.DataFrame:
        mapped_by_key: dict[tuple[str, datetime], dict[str, Any]] = {}
        symbol_aliases = _storage_symbol_aliases(symbols)
        for row in rows:
            stored_symbol = str(row.get("symbol") or "").upper()
            symbol = symbol_aliases.get(stored_symbol)
            if not symbol or row.get("bar_time_utc") is None:
                continue
            item = dict(row)
            item["symbol"] = symbol
            local_time = _minute_local_time(row["bar_time_utc"], symbol)
            if start_time is not None and local_time.date() < start_time.date():
                continue
            if end_time is not None and local_time.date() > end_time.date():
                continue
            item["datetime"] = local_time
            key = (symbol, local_time)
            previous = mapped_by_key.get(key)
            current_priority = int(item.get("source_priority") or 0)
            previous_priority = int(previous.get("source_priority") or 0) if previous else -1
            if current_priority >= previous_priority:
                mapped_by_key[key] = item
        return self._minute_frame(list(mapped_by_key.values()))

    def get_minute_strict(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
    ) -> pl.DataFrame:
        rows = self._query_minute_rows(symbols, start_time, end_time, freq)
        frame = self._normalize_minute_query_rows(rows, symbols, start_time, end_time)
        if frame.is_empty():
            return frame.with_columns(pl.Series("source", [], dtype=pl.String))
        return frame.with_columns(pl.lit("webstock").alias("source"))

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        rows = self._query_minute_rows(symbols, start_time, end_time, freq)
        frame = self._normalize_minute_query_rows(rows, symbols, start_time, end_time)
        mapped_by_key = {
            (str(row["symbol"]), row["datetime"]): row for row in frame.to_dicts()
        }

        covered = {symbol for symbol, _ in mapped_by_key}
        missing_symbols = [
            str(value).upper() for value in symbols if str(value).upper() not in covered
        ]
        for symbol in missing_symbols:
            for row in self._minute_fallback_fn(symbol):
                timestamp = row.get("time") or row.get("date") or row.get("timestamp")
                if timestamp is None:
                    continue
                local_time = _longbridge_minute_local_time(timestamp, symbol)
                if start_time is not None and local_time.date() < start_time.date():
                    continue
                if end_time is not None and local_time.date() > end_time.date():
                    continue
                close = row.get("close", row.get("price"))
                item = {
                    "symbol": symbol,
                    "datetime": local_time,
                    "open": row.get("open", close),
                    "high": row.get("high", close),
                    "low": row.get("low", close),
                    "close": close,
                    "volume": row.get("volume"),
                    "amount": row.get("amount", row.get("turnover")),
                    "source_priority": 0,
                }
                mapped_by_key[(symbol, local_time)] = item

        frame = self._minute_frame(list(mapped_by_key.values()))
        if on_chunk_done:
            on_chunk_done(1, 1)
        return frame

    def _query_realtime_rows(self, symbols: list[str] | None) -> list[dict]:
        filters = [
            "snapshot_minute >= toStartOfDay(now('Asia/Shanghai'))",
            "snapshot_minute < toStartOfDay(now('Asia/Shanghai')) + INTERVAL 1 DAY",
        ]
        if symbols:
            filters.append(f"symbol IN {_symbols_sql(symbols)}")
        where_clause = "WHERE " + "\n              AND ".join(filters)
        sql = f"""
            SELECT symbol, market, snapshot_minute, last_done, prev_close,
                   open, high, low, change_value, change_percentage, volume, turnover
            FROM {self._table("lb_realtime_quotes")}
            {where_clause}
            ORDER BY symbol, snapshot_minute DESC, inserted_at DESC
            LIMIT 1 BY symbol
        """
        return self._query(sql)

    def _query_realtime_rows_strict(self, symbols: list[str]) -> list[dict]:
        symbol_aliases = _storage_symbol_aliases(symbols)
        sql = f"""
            WITH latest_quotes AS (
                SELECT symbol, market, snapshot_minute, last_done, prev_close,
                       open, high, low, change_value, change_percentage, volume, turnover
                FROM {self._table("lb_realtime_quotes")}
                WHERE symbol IN {_symbols_sql(list(symbol_aliases))}
                  AND snapshot_minute >= now('Asia/Shanghai') - INTERVAL 1 DAY
                  AND snapshot_minute <= now('Asia/Shanghai')
                ORDER BY symbol, snapshot_minute DESC, inserted_at DESC
                LIMIT 1 BY symbol
            ),
            session_baselines AS (
                SELECT source.symbol,
                       argMaxIf(
                           source.prev_close,
                           tuple(source.snapshot_minute, source.inserted_at),
                           source.prev_close IS NOT NULL
                       ) AS prev_close
                FROM {self._table("lb_realtime_quotes")} AS source
                INNER JOIN latest_quotes AS quote ON quote.symbol = source.symbol
                WHERE toDate(source.snapshot_minute) = toDate(quote.snapshot_minute)
                  AND source.snapshot_minute <= quote.snapshot_minute
                GROUP BY source.symbol
            ),
            symbol_metadata AS (
                SELECT symbol, argMax(name, updated_at) AS name
                FROM {self._table("lb_symbols")}
                WHERE symbol IN {_symbols_sql(list(symbol_aliases))}
                GROUP BY symbol
            )
            SELECT quote.symbol AS symbol, quote.market, quote.snapshot_minute, quote.last_done,
                   coalesce(quote.prev_close, baseline.prev_close) AS prev_close,
                   quote.open, quote.high, quote.low,
                   quote.change_value, quote.change_percentage, quote.volume,
                   quote.turnover, metadata.name
            FROM latest_quotes AS quote
            LEFT JOIN session_baselines AS baseline ON baseline.symbol = quote.symbol
            LEFT JOIN symbol_metadata AS metadata ON metadata.symbol = quote.symbol
        """
        return self._query(sql)

    @staticmethod
    def _normalize_realtime_query_rows(
        rows: list[dict],
        symbol_aliases: dict[str, str] | None = None,
        recompute_change: bool = False,
    ) -> list[dict]:
        records: list[dict] = []
        for row in rows:
            stored_symbol = str(row.get("symbol") or "").upper()
            symbol = (
                symbol_aliases.get(stored_symbol)
                if symbol_aliases is not None
                else stored_symbol
            )
            if not symbol:
                continue
            timestamp = None
            if row.get("snapshot_minute") is not None:
                timestamp = int(_as_shanghai_time(row["snapshot_minute"]).timestamp() * 1000)
            last_price = row.get("last_done")
            prev_close = row.get("prev_close")
            change_amount = row.get("change_value")
            if (
                recompute_change
                and last_price is not None
                and prev_close is not None
            ):
                change_amount = float(last_price) - float(prev_close)
            change_pct = row.get("change_percentage")
            if (
                recompute_change
                and last_price is not None
                and prev_close is not None
                and float(prev_close) != 0
            ):
                change_pct = (
                    (float(last_price) - float(prev_close))
                    / float(prev_close)
                    * 100.0
                )
            records.append({
                "symbol": symbol,
                "name": row.get("name"),
                "market": row.get("market"),
                "last_price": last_price,
                "prev_close": prev_close,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "volume": row.get("volume"),
                "amount": row.get("turnover"),
                "change_amount": change_amount,
                "change_pct": float(change_pct) / 100.0 if change_pct is not None else None,
                "timestamp": timestamp,
            })
        return records

    def get_realtime_strict(self, symbols: list[str]) -> list[dict]:
        if not symbols:
            return []
        return self._normalize_realtime_query_rows(
            self._query_realtime_rows_strict(symbols),
            _storage_symbol_aliases(symbols),
            recompute_change=True,
        )

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        return self._normalize_realtime_query_rows(self._query_realtime_rows(symbols))

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        if asset_type != "stock":
            return []
        rows = self._query(f"""
            WITH daily_symbols AS (
                SELECT symbol, any(market) AS market
                FROM {self._table("lb_daily_bars")}
                GROUP BY symbol
            ),
            symbol_metadata AS (
                SELECT symbol,
                       argMax(name, updated_at) AS name,
                       argMax(currency, updated_at) AS currency,
                       argMax(lot_size, updated_at) AS lot_size
                FROM {self._table("lb_symbols")}
                GROUP BY symbol
            )
            SELECT daily.symbol, daily.market,
                   metadata.name, metadata.currency, metadata.lot_size
            FROM daily_symbols AS daily
            LEFT JOIN symbol_metadata AS metadata ON metadata.symbol = daily.symbol
            ORDER BY daily.symbol
        """)
        instruments: list[dict] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or "." not in symbol:
                continue
            instruments.append({
                "symbol": symbol,
                "name": row.get("name") or symbol,
                "code": symbol.rsplit(".", 1)[0],
                "exchange": symbol.rsplit(".", 1)[1],
                "market": str(row.get("market") or "").lower(),
                "lot_size": row.get("lot_size") or round_lot_size(symbol),
                "currency": row.get("currency") or market_rule_for_symbol(symbol).currency,
                "asset_type": "stock",
                "source": self.name,
            })
        return instruments

    def get_market_industries(self, market: str) -> dict[str, Any]:
        """读取港股/美股 F10 行业成分，并标记最新行业代表股。"""
        normalized = str(market or "").strip().lower()
        if normalized == "hk":
            leader_table = "lb_company_background_industry_leaders"
            leader_date_column = "snapshot_date"
        elif normalized == "us":
            leader_table = "lb_sector_leader_snapshots"
            leader_date_column = "trade_date"
        else:
            return {
                "market": normalized or "cn",
                "as_of": None,
                "source": None,
                "leader_source": None,
                "rows": [],
            }

        profile_table = self._table("lb_eastmoney_f10_profiles")
        qualified_leader_table = self._table(leader_table)
        rows = self._query(f"""
            WITH profiles AS (
                SELECT symbol,
                       argMax(company_name, updated_at) AS profile_name,
                       argMax(industry, updated_at) AS industry,
                       max(updated_at) AS profile_updated_at
                FROM {profile_table}
                WHERE market = {_sql_string(normalized)}
                GROUP BY symbol
                HAVING trimBoth(ifNull(industry, '')) != ''
            ),
            latest_trade_date AS (
                SELECT max(trade_date) AS trade_date
                FROM {self._table("lb_daily_bars")}
                WHERE market = {_sql_string(normalized)}
            ),
            active_symbols AS (
                SELECT DISTINCT symbol
                FROM {self._table("lb_daily_bars")}
                WHERE market = {_sql_string(normalized)}
                  AND trade_date = (SELECT trade_date FROM latest_trade_date)
            ),
            symbol_metadata AS (
                SELECT symbol, argMax(name, updated_at) AS name
                FROM {self._table("lb_symbols")}
                WHERE market = {_sql_string(normalized)}
                GROUP BY symbol
            ),
            leaders AS (
                SELECT DISTINCT symbol
                FROM {qualified_leader_table}
                WHERE market = {_sql_string(normalized)}
                  AND {leader_date_column} = (
                      SELECT max({leader_date_column})
                      FROM {qualified_leader_table}
                      WHERE market = {_sql_string(normalized)}
                  )
            )
            SELECT toString(profiles.profile_updated_at) AS as_of,
                   profiles.symbol AS symbol,
                   coalesce(nullIf(symbol_metadata.name, ''),
                            nullIf(profiles.profile_name, ''),
                            profiles.symbol) AS name,
                   profiles.industry AS industry,
                   leaders.symbol != '' AS is_leader
            FROM profiles
            INNER JOIN active_symbols ON active_symbols.symbol = profiles.symbol
            LEFT JOIN symbol_metadata ON symbol_metadata.symbol = profiles.symbol
            LEFT JOIN leaders ON leaders.symbol = profiles.symbol
            ORDER BY industry, symbol
        """)

        suffix = f".{normalized.upper()}"
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        as_of: str | None = None
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol.endswith(suffix):
                continue
            industry = str(row.get("industry") or "").strip()
            if not industry:
                continue
            key = (symbol, industry)
            if key in seen:
                continue
            seen.add(key)
            row_as_of = str(row.get("as_of")) if row.get("as_of") else None
            if row_as_of and (as_of is None or row_as_of > as_of):
                as_of = row_as_of
            items.append({
                "symbol": symbol,
                "name": row.get("name") or symbol,
                "main_sector": "",
                "sub_industry": industry,
                "industry": industry,
                "is_leader": bool(row.get("is_leader")),
            })
        return {
            "market": normalized,
            "as_of": as_of,
            "source": "lb_eastmoney_f10_profiles",
            "leader_source": leader_table,
            "rows": items,
        }

    def get_market_concepts(self, market: str) -> dict[str, Any]:
        """Return recent event-derived themes for active HK/US symbols."""
        normalized = str(market or "").strip().lower()
        window_days = 30
        if normalized not in {"hk", "us"}:
            return {
                "market": normalized or "cn",
                "as_of": None,
                "source": None,
                "window_days": window_days,
                "rows": [],
            }

        if normalized == "hk":
            symbol_filter = "match(raw_symbol, '^[0-9]{1,5}(\\.HK)?$')"
            symbol_normalizer = (
                "concat(toString(toUInt32OrZero(splitByChar('.', raw_symbol)[1])), '.HK')"
            )
        else:
            symbol_filter = "match(raw_symbol, '^[A-Z][A-Z0-9.+-]*(\\.US)?$')"
            symbol_normalizer = (
                "concat(replaceAll(if(endsWith(raw_symbol, '.US'), "
                "substring(raw_symbol, 1, length(raw_symbol) - 3), raw_symbol), '.', '-'), '.US')"
            )
        concept_quality_filter = (
            "positionCaseInsensitiveUTF8(concept, '中概') = 0"
            if normalized == "us"
            else "1"
        )

        event_table = self._table("lb_sentiment_impact_events")
        rows = self._query(f"""
            WITH event_pairs AS (
                SELECT analysis_date,
                       upper(trimBoth(arrayJoin(affected_symbols))) AS raw_symbol,
                       trimBoth(arrayJoin(affected_sectors)) AS concept
                FROM {event_table}
                WHERE source_market = {_sql_string(normalized)}
                  AND analysis_date >= (SELECT max(analysis_date) - 29 FROM {event_table}
                                        WHERE source_market = {_sql_string(normalized)})
            ),
            normalized_pairs AS (
                SELECT analysis_date,
                       {symbol_normalizer} AS normalized_symbol,
                       concept
                FROM event_pairs
                WHERE concept != '' AND {symbol_filter}
            ),
            supported_pairs AS (
                SELECT normalized_symbol,
                       concept,
                       max(analysis_date) AS last_analysis_date,
                       count() AS support_count,
                       uniqExact(analysis_date) AS support_days
                FROM normalized_pairs
                WHERE {concept_quality_filter}
                GROUP BY normalized_symbol, concept
            ),
            ranked_pairs AS (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY normalized_symbol
                           ORDER BY support_count DESC, support_days DESC, concept
                       ) AS theme_rank
                FROM supported_pairs
            ),
            latest_trade_date AS (
                SELECT max(trade_date) AS trade_date
                FROM {self._table("lb_daily_bars")}
                WHERE market = {_sql_string(normalized)}
            ),
            active_symbols AS (
                SELECT DISTINCT symbol AS active_symbol
                FROM {self._table("lb_daily_bars")}
                WHERE market = {_sql_string(normalized)}
                  AND trade_date = (SELECT trade_date FROM latest_trade_date)
            ),
            symbol_metadata AS (
                SELECT symbol AS metadata_symbol, argMax(name, updated_at) AS metadata_name
                FROM {self._table("lb_symbols")}
                WHERE market = {_sql_string(normalized)}
                GROUP BY symbol
            )
            SELECT toString(max(pairs.last_analysis_date)) AS as_of,
                   pairs.normalized_symbol AS symbol,
                   coalesce(nullIf(metadata.metadata_name, ''), pairs.normalized_symbol) AS name,
                   pairs.concept AS concept
            FROM ranked_pairs AS pairs
            INNER JOIN active_symbols AS active
                    ON active.active_symbol = pairs.normalized_symbol
            LEFT JOIN symbol_metadata AS metadata
                   ON metadata.metadata_symbol = pairs.normalized_symbol
            WHERE pairs.theme_rank <= 20
            GROUP BY pairs.normalized_symbol, metadata.metadata_name, pairs.concept
            ORDER BY concept, symbol
        """)

        suffix = f".{normalized.upper()}"
        items: list[dict[str, Any]] = []
        concept_members: dict[str, set[str]] = {}
        seen: set[tuple[str, str]] = set()
        as_of: str | None = None
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            concept = str(row.get("concept") or "").strip()
            if not symbol.endswith(suffix) or not concept:
                continue
            if normalized == "us" and "中概" in concept:
                continue
            if normalized == "us" and concept.startswith("美股"):
                concept = concept[2:].strip() or concept
            key = (symbol, concept)
            if key in seen:
                continue
            seen.add(key)
            concept_members.setdefault(concept, set()).add(symbol)
            row_as_of = str(row.get("as_of")) if row.get("as_of") else None
            if row_as_of and (as_of is None or row_as_of > as_of):
                as_of = row_as_of
            items.append({
                "symbol": symbol,
                "name": row.get("name") or symbol,
                "concept": concept,
            })

        if normalized == "us":
            items = [
                item for item in items
                if len(concept_members[item["concept"]]) >= 2
            ]

        return {
            "market": normalized,
            "as_of": as_of,
            "source": "lb_sentiment_impact_events",
            "window_days": window_days,
            "rows": items,
        }

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        sample_symbols = symbols or ["000001.SZ"]
        if dataset == "daily":
            frame = self.get_daily(sample_symbols, None, None)
            return _preview(dataset, frame)
        if dataset == "minute":
            frame = self.get_minute(sample_symbols, None, None)
            return _preview(dataset, frame)
        if dataset == "realtime":
            rows = self.get_realtime(symbols=sample_symbols)
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": len(rows),
                "columns": list(rows[0]) if rows else [],
                "preview": rows[:5],
            }
        raise ValueError(f"ClickHouse 不支持数据集: {dataset}")


def _preview(dataset: str, frame: pl.DataFrame) -> dict:
    return {
        "provider": "clickhouse",
        "dataset": dataset,
        "rows": frame.height,
        "columns": frame.columns,
        "preview": frame.head(5).to_dicts() if not frame.is_empty() else [],
    }
