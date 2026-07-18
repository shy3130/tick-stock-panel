"""将 longbridge ClickHouse 行情映射为 TickFlow Provider 契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.base import ProviderCapabilities
from app.data_providers.normalizer import normalize_daily
from app.market_rules import market_rule_for_symbol, round_lot_size
from app.plugins.clickhouse import bridge

QueryFn = Callable[[str], list[dict]]
_MINUTE_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
_DAILY_SYMBOL_BATCH_SIZE = 500


@dataclass
class _ClickHouseConfig:
    name: str = "clickhouse"
    display_name: str = "Longbridge ClickHouse - 三市场"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(("daily", "minute", "realtime")))
    path: None = None
    builtin: bool = True


def _sql_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _symbols_sql(symbols: list[str]) -> str:
    return "(" + ", ".join(_sql_string(symbol.upper()) for symbol in symbols) + ")"


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


class ClickHouseProvider:
    name = "clickhouse"
    builtin = True
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        minute=True,
        realtime=True,
    )

    def __init__(self, query_fn: QueryFn | None = None) -> None:
        self.config = _ClickHouseConfig()
        self._query_fn = query_fn or bridge.query_json_each_row
        self.last_sql = ""

    def close(self) -> None:
        return None

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
            sql = f"""
                SELECT symbol, market, trade_date, open, high, low, close,
                       volume, turnover AS amount
                FROM {self._table("lb_daily_bars")}
                WHERE adjusted = 1
                  AND symbol IN {_symbols_sql(symbol_chunk)}
                  {_date_filter("trade_date", start_time, end_time)}
                ORDER BY symbol, trade_date
            """
            rows = self._query(sql)
            normalized_rows = [dict(row, amount=row.get("amount", row.get("turnover"))) for row in rows]
            frame = normalize_daily(normalized_rows, source=self.name)
            if not frame.is_empty():
                frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, len(symbol_chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

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
        sql = f"""
            SELECT symbol, market, bar_time_utc, open, high, low, close, volume, amount
            FROM {self._table("lb_minute_bars")}
            WHERE frequency = {_sql_string(freq)}
              AND symbol IN {_symbols_sql(symbols)}
              {_date_filter("trade_date_local", start_time, end_time)}
            ORDER BY symbol, bar_time_utc
        """
        rows = self._query(sql)
        mapped: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or row.get("bar_time_utc") is None:
                continue
            item = dict(row)
            item["symbol"] = symbol
            item["datetime"] = _minute_local_time(row["bar_time_utc"], symbol)
            mapped.append(item)
        frame = pl.DataFrame(mapped) if mapped else pl.DataFrame()
        if not frame.is_empty():
            for column in ("open", "high", "low", "close", "volume", "amount"):
                if column in frame.columns:
                    frame = frame.with_columns(pl.col(column).cast(pl.Float64, strict=False))
            frame = frame.select([column for column in _MINUTE_COLUMNS if column in frame.columns])
        if on_chunk_done:
            on_chunk_done(1, 1)
        return frame

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        symbol_filter = f"WHERE symbol IN {_symbols_sql(symbols)}" if symbols else ""
        sql = f"""
            SELECT symbol, market, snapshot_minute, last_done, prev_close,
                   open, high, low, change_value, change_percentage, volume, turnover
            FROM {self._table("lb_realtime_quotes")}
            {symbol_filter}
            ORDER BY symbol, snapshot_minute DESC, inserted_at DESC
            LIMIT 1 BY symbol
        """
        records: list[dict] = []
        for row in self._query(sql):
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            timestamp = None
            if row.get("snapshot_minute") is not None:
                timestamp = int(_as_shanghai_time(row["snapshot_minute"]).timestamp() * 1000)
            change_pct = row.get("change_percentage")
            records.append({
                "symbol": symbol,
                "market": row.get("market"),
                "last_price": row.get("last_done"),
                "prev_close": row.get("prev_close"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "volume": row.get("volume"),
                "amount": row.get("turnover"),
                "change_amount": row.get("change_value"),
                "change_pct": float(change_pct) / 100.0 if change_pct is not None else None,
                "timestamp": timestamp,
            })
        return records

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
