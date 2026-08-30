"""Catalog-pinned intraday facts for Issue #48 escape-risk research.

The reader resolves every requested trading day exactly once, opens only the
resolved immutable DuckDB files, and never falls back to raw storage.  Minute
``amount`` is intentionally ignored: cumulative VWAP is rebuilt from
transaction ``amount / volume`` after strict minute-vs-transaction volume
reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from app.data_providers.fquant import catalog_resolver
from app.data_providers.fquant.daily_market_research import (
    PublishedDailyMarketFactsReader,
    TurnoverFact,
)
from app.data_providers.fquant.lease import ConnectionSet
from app.data_providers.fquant.symbols import split_symbol
from app.storage.duckdb_runtime import connect_duckdb

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EXPECTED_MINUTE_INDICES = tuple(range(240))


class EscapeRiskIntradayIntegrityError(RuntimeError):
    """One symbol/day cannot be proved complete and internally consistent."""


@dataclass(frozen=True, slots=True)
class IntradayMinute:
    minute_index: int
    timestamp: datetime
    close: float
    high: float
    low: float
    volume_shares: int
    amount: float
    cumulative_vwap: float


@dataclass(frozen=True, slots=True)
class IntradayDay:
    symbol: str
    trade_date: date
    minutes: tuple[IntradayMinute, ...]
    open_price: float
    pre_close: float
    published_limit_up: float
    published_limit_down: float
    turnover: TurnoverFact | None


@dataclass(frozen=True, slots=True)
class EscapeRiskIntradayBundle:
    rows: Mapping[tuple[str, date], IntradayDay]
    unavailable: Mapping[tuple[str, date], str]


@dataclass(frozen=True, slots=True)
class _RoutePair:
    minutes_path: str
    trans_path: str


def _minute_times(index: int) -> tuple[str, ...]:
    if index == 0:
        return ("09:25", "09:30")
    if index == 119:
        return ("11:29", "11:30")
    if 1 <= index <= 118:
        value = datetime.combine(date.min, time(9, 30)) + timedelta(minutes=index)
        return (value.strftime("%H:%M"),)
    if 120 <= index <= 238:
        value = datetime.combine(date.min, time(13, 0)) + timedelta(minutes=index - 120)
        return (value.strftime("%H:%M"),)
    if index == 239:
        return ("14:59", "15:00")
    raise EscapeRiskIntradayIntegrityError(f"unexpected minute_index={index}")


def _minute_timestamp(day: date, index: int) -> datetime:
    clock = _minute_times(index)[-1]
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=_SHANGHAI)


def _tdx_code(symbol: str) -> str:
    code, market = split_symbol(symbol)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market)
    if prefix is None:
        raise ValueError(f"escape-risk intraday supports A-share symbols only: {symbol}")
    return prefix + code


def _manifest_identity(path: str) -> dict[str, object]:
    db = Path(path)
    if db.is_symlink() or not db.is_file():
        raise FileNotFoundError(f"catalog route is not a regular file: {db}")
    manifest_path = db.parent / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(f"catalog route manifest missing: {manifest_path}")
    payload = manifest_path.read_bytes()
    manifest = json.loads(payload)
    generation = db.parent.name
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError(f"catalog route manifest generation mismatch: {db}")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not any(
        isinstance(entry, dict) and Path(str(entry.get("file", ""))).name == db.name
        for entry in entries
    ):
        raise ValueError(f"catalog route manifest does not pin file: {db}")
    return {
        "generation": generation,
        "file": db.name,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
    }


class CatalogPinnedEscapeRiskIntradayReader:
    """Request-owned catalog reader with per-day immutable route pins."""

    def __init__(
        self,
        days: Sequence[date],
        markets_reader: PublishedDailyMarketFactsReader,
    ) -> None:
        self._days = tuple(sorted(set(days)))
        self._markets = markets_reader
        self._minutes_connections = ConnectionSet(
            lambda path: connect_duckdb(path, read_only=True)
        )
        self._trans_connections = ConnectionSet(
            lambda path: connect_duckdb(path, read_only=True)
        )
        self._routes: dict[date, _RoutePair] = {}
        self._route_failures: dict[date, str] = {}
        self._identities: dict[str, dict[str, object]] = {}
        self._closed = False
        for day in self._days:
            try:
                minutes_path = catalog_resolver.resolve_route("tdx_minutes", "a", day)
                trans_path = catalog_resolver.resolve_route("tdx_trans", "a", day)
                self._identities.setdefault(minutes_path, _manifest_identity(minutes_path))
                self._identities.setdefault(trans_path, _manifest_identity(trans_path))
                self._routes[day] = _RoutePair(minutes_path, trans_path)
            except (catalog_resolver.CatalogError, OSError, TypeError, ValueError) as exc:
                self._route_failures[day] = f"catalog_route_unavailable:{exc}"

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("escape-risk intraday reader is closed")

    def run_manifest(self) -> dict[str, object]:
        self._ensure_open()
        file_keys = {
            path: f"file-{index}"
            for index, path in enumerate(sorted(self._identities), start=1)
        }
        return {
            "provider": "fquant.catalog_pinned_escape_risk_intraday",
            "markets": {
                "generation": self._markets.generation(),
                "manifest_sha256": self._markets.manifest_sha256(),
                "pin_verified": self._markets.pin_identity_verified(),
            },
            "files": {
                file_keys[path]: identity for path, identity in self._identities.items()
            },
            "routes": {
                day.isoformat(): {
                    "minutes": file_keys[pair.minutes_path],
                    "trans": file_keys[pair.trans_path],
                }
                for day, pair in self._routes.items()
            },
            "route_failures": {
                day.isoformat(): reason for day, reason in self._route_failures.items()
            },
            "coverage": {
                "requested_days": len(self._days),
                "resolved_days": len(self._routes),
                "unavailable_days": len(self._route_failures),
                "first_day": self._days[0].isoformat() if self._days else None,
                "last_day": self._days[-1].isoformat() if self._days else None,
            },
        }

    @staticmethod
    def _group_rows(rows: list[tuple]) -> dict[str, list[tuple]]:
        grouped: dict[str, list[tuple]] = {}
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row[1:])
        return grouped

    def _query_day(
        self, pair: _RoutePair, codes: Sequence[str], day: date
    ) -> tuple[dict[str, list[tuple]], dict[str, list[tuple]]]:
        placeholders = ",".join("?" for _ in codes)
        with self._minutes_connections.lease(pair.minutes_path) as connection:
            minute_rows = connection.execute(
                "SELECT code, minute_index, price, volume FROM market_minutes "
                f"WHERE trade_date = ? AND code IN ({placeholders}) "
                "ORDER BY code, minute_index",
                [day, *codes],
            ).fetchall()
        with self._trans_connections.lease(pair.trans_path) as connection:
            trans_rows = connection.execute(
                "SELECT code, time, MIN(price), MAX(price), "
                "SUM(volume), SUM(amount) FROM market_transactions "
                f"WHERE trade_date = ? AND code IN ({placeholders}) "
                "GROUP BY code, time ORDER BY code, time",
                [day, *codes],
            ).fetchall()
        return self._group_rows(minute_rows), self._group_rows(trans_rows)

    @staticmethod
    def _build_day(
        symbol: str,
        day: date,
        minute_rows: Sequence[tuple],
        trans_rows: Sequence[tuple],
        open_price: float,
        pre_close: float,
        limit_up: float,
        limit_down: float,
        turnover: TurnoverFact | None,
    ) -> IntradayDay:
        indices = tuple(int(row[0]) for row in minute_rows)
        if indices != _EXPECTED_MINUTE_INDICES:
            raise EscapeRiskIntradayIntegrityError(
                f"minute_coverage_incomplete:{len(indices)}"
            )
        expected_total_shares = sum(
            int(row[2]) * 100 for row in minute_rows if row[2] is not None
        )
        trans_by_time = {
            str(clock): (low, high, volume, amount)
            for clock, low, high, volume, amount in trans_rows
        }
        consumed: set[str] = set()
        cumulative_volume = 0
        cumulative_amount = 0.0
        built: list[IntradayMinute] = []
        for minute_index, price_raw, minute_volume_raw in minute_rows:
            close = float(price_raw) if price_raw is not None else math.nan
            minute_hands = int(minute_volume_raw) if minute_volume_raw is not None else -1
            if not math.isfinite(close) or close <= 0 or minute_hands < 0:
                raise EscapeRiskIntradayIntegrityError("minute_values_invalid")
            lows: list[float] = []
            highs: list[float] = []
            trans_volume = 0
            trans_amount = 0.0
            for clock in _minute_times(int(minute_index)):
                row = trans_by_time.get(clock)
                if row is None:
                    continue
                consumed.add(clock)
                low_raw, high_raw, volume_raw, amount_raw = row
                volume = int(volume_raw or 0)
                amount = float(amount_raw or 0.0)
                if volume < 0 or not math.isfinite(amount) or amount < 0:
                    raise EscapeRiskIntradayIntegrityError("transaction_values_invalid")
                trans_volume += volume
                trans_amount += amount
                if low_raw is not None:
                    lows.append(float(low_raw))
                if high_raw is not None:
                    highs.append(float(high_raw))
            # Engine minute buckets occasionally shift one or a few hands at
            # the lunch/close boundary. Per-minute equality is therefore not a
            # valid invariant; the sealed full-day totals below must match
            # exactly, while price/amount/VWAP always come from transactions.
            if trans_volume > 0 and (not lows or not highs or trans_amount <= 0):
                raise EscapeRiskIntradayIntegrityError(
                    f"transaction_ohlca_missing:index={minute_index}"
                )
            low = min(lows) if lows else close
            high = max(highs) if highs else close
            cumulative_volume += trans_volume
            cumulative_amount += trans_amount
            if cumulative_volume <= 0:
                raise EscapeRiskIntradayIntegrityError("cumulative_volume_zero")
            cumulative_vwap = cumulative_amount / cumulative_volume
            if not math.isfinite(cumulative_vwap) or cumulative_vwap <= 0:
                raise EscapeRiskIntradayIntegrityError("cumulative_vwap_invalid")
            built.append(
                IntradayMinute(
                    minute_index=int(minute_index),
                    timestamp=_minute_timestamp(day, int(minute_index)),
                    close=close,
                    high=high,
                    low=low,
                    volume_shares=trans_volume,
                    amount=trans_amount,
                    cumulative_vwap=cumulative_vwap,
                )
            )
        for clock, (_, _, volume_raw, amount_raw) in trans_by_time.items():
            if clock not in consumed and (int(volume_raw or 0) != 0 or float(amount_raw or 0) != 0):
                raise EscapeRiskIntradayIntegrityError(
                    f"transaction_time_unmapped:{clock}"
                )
        if cumulative_volume != expected_total_shares:
            raise EscapeRiskIntradayIntegrityError(
                "volume_mismatch:"
                f"minute={expected_total_shares}:trans={cumulative_volume}"
            )
        if not all(
            math.isfinite(value) and value > 0
            for value in (open_price, pre_close, limit_up, limit_down)
        ):
            raise EscapeRiskIntradayIntegrityError("market_facts_invalid")
        return IntradayDay(
            symbol=symbol,
            trade_date=day,
            minutes=tuple(built),
            open_price=open_price,
            pre_close=pre_close,
            published_limit_up=limit_up,
            published_limit_down=limit_down,
            turnover=turnover,
        )

    def load(self, symbols: Sequence[str]) -> EscapeRiskIntradayBundle:
        self._ensure_open()
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        tdx_by_symbol = {symbol: _tdx_code(symbol) for symbol in normalized}
        symbol_by_tdx = {code: symbol for symbol, code in tdx_by_symbol.items()}
        rows: dict[tuple[str, date], IntradayDay] = {}
        unavailable: dict[tuple[str, date], str] = {}
        for day in self._days:
            failure = self._route_failures.get(day)
            if failure is not None:
                for symbol in normalized:
                    unavailable[(symbol, day)] = failure
                continue
            pair = self._routes[day]
            try:
                minutes_by_code, trans_by_code = self._query_day(
                    pair, tuple(symbol_by_tdx), day
                )
                facts = self._markets.escape_risk_facts(normalized, day)
            except Exception as exc:  # query failure is data unavailability, never fallback
                for symbol in normalized:
                    unavailable[(symbol, day)] = f"intraday_query_unavailable:{exc}"
                continue
            for tdx_code, symbol in symbol_by_tdx.items():
                minute_rows = minutes_by_code.get(tdx_code)
                trans_rows = trans_by_code.get(tdx_code)
                market_pair = facts.get(symbol)
                if not minute_rows or not trans_rows:
                    unavailable[(symbol, day)] = "intraday_rows_missing"
                    continue
                if market_pair is None:
                    unavailable[(symbol, day)] = "market_facts_missing"
                    continue
                market_fact, turnover = market_pair
                try:
                    assert market_fact.pre_close is not None
                    assert market_fact.published_limit_up is not None
                    assert market_fact.published_limit_down is not None
                    rows[(symbol, day)] = self._build_day(
                        symbol,
                        day,
                        minute_rows,
                        trans_rows,
                        market_fact.raw_open,
                        market_fact.pre_close,
                        market_fact.published_limit_up,
                        market_fact.published_limit_down,
                        turnover,
                    )
                except (AssertionError, EscapeRiskIntradayIntegrityError, TypeError, ValueError) as exc:
                    unavailable[(symbol, day)] = f"intraday_integrity:{exc}"
        return EscapeRiskIntradayBundle(
            rows=MappingProxyType(rows),
            unavailable=MappingProxyType(unavailable),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._minutes_connections.close()
        self._trans_connections.close()
        self._markets.close()


__all__ = [
    "CatalogPinnedEscapeRiskIntradayReader",
    "EscapeRiskIntradayBundle",
    "EscapeRiskIntradayIntegrityError",
    "IntradayDay",
    "IntradayMinute",
    "TurnoverFact",
]
