"""Production-owned, fail-closed data seam for weak-to-strong research."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader
from app.data_providers.fquant.ordered_trans import (
    DEFAULT_ORDERED_TRANS_ROOT,
    ORDERED_TRANS_ROOT_ENV,
    OrderedTransIntegrityError,
    PublishedOrderedTransMinuteReader,
    canonical_json_bytes,
)
from app.data_providers.fquant.published_call_auction import PublishedCallAuctionReader
from app.data_providers.fquant.symbols import exchange_of
from app.services.research_sealed_data import PublishedCanonicalDailyReader
from app.services.weak_to_strong import (
    MINIMUM_CAPABILITIES,
    AuctionEvidence,
    DailyBar,
    MinuteBar,
    PITRecord,
    WeakToStrongReader,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CUTOFF = time(9, 25)


def _research_symbol(code: str) -> str:
    market = exchange_of(code)
    if market not in {"SH", "SZ", "BJ"}:
        raise RuntimeError("unknown market for research symbol")
    return f"{code}.{market}"
def _cutoff(day: date) -> datetime:
    return datetime.combine(day, _CUTOFF, tzinfo=_SHANGHAI)


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed


def _coverage(days: list[date]) -> dict[str, object]:
    return {
        "days": len(days),
        "first_day": min(days).isoformat() if days else None,
        "last_day": max(days).isoformat() if days else None,
    }


def _regime_pct(regime: str) -> float | None:
    return {"st_5": 0.05, "main_10": 0.10, "star_20": 0.20, "chinext_20": 0.20, "beijing_30": 0.30}.get(regime)


class WeakToStrongProductionReader(WeakToStrongReader):
    """Composite pinned reader; caller owns and closes this object."""

    def __init__(self, canonical: Any, markets: Any, minute: Any, callauction: Any | None) -> None:
        self._canonical = canonical
        self._markets = markets
        self._minute = minute
        self._callauction = callauction
        self._closed = False
        actual = set(MINIMUM_CAPABILITIES)
        if callauction is not None:
            actual.add("auction_evidence_reader")
        self._capabilities = frozenset(actual)
        self._components = self._build_components()
        digest = hashlib.sha256(canonical_json_bytes(self._components)).hexdigest()
        self._manifest = {"generation": f"composite-{digest[:16]}", "sha256": digest, "components": self._components}

    def _build_components(self) -> dict[str, dict[str, object]]:
        canonical_manifest = self._canonical.manifest() if callable(getattr(self._canonical, "manifest", None)) else {}
        canonical_days = self._canonical.market_days(date.min, date.max)
        minute_manifest = self._minute.catalog_manifest() if callable(getattr(self._minute, "catalog_manifest", None)) else {}
        minute_days = [date.fromisoformat(v) for v in minute_manifest.get("coverage", {}).get(next(iter(minute_manifest.get("coverage", {})), ""), [])] if minute_manifest.get("coverage") else []
        markets_created = _parse_created_at(self._markets.created_at() if callable(getattr(self._markets, "created_at", None)) else None)
        market_days = self._markets.market_days(date.min, date.max)
        first_pit = next((day for day in market_days if markets_created is not None and markets_created <= _cutoff(day)), None)
        def component(provider: str, route: object, reader: Any, coverage: dict[str, object], first: object = None) -> dict[str, object]:
            return {"provider": provider, "route": route, "generation": reader.generation(), "manifest_sha256": reader.manifest_sha256(), "coverage": coverage, "first_available_at": first}
        call_component = (
            component("fquant.published_call_auction", self._callauction.route(), self._callauction, self._callauction.coverage())
            if self._callauction is not None
            else {"provider": "fquant.published_call_auction", "route": {"logical": "tdx_callauction", "root_env": "FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION"}, "generation": None, "manifest_sha256": None, "coverage": {}, "first_available_at": None}
        )
        canonical_created = _parse_created_at(canonical_manifest.get("created_at") if isinstance(canonical_manifest, dict) else None)
        return {
            "canonical": component("canonical.published_daily", "canonical_history", self._canonical, _coverage(canonical_days), canonical_created.isoformat() if canonical_created else None),
            "markets": component("fquant.published_markets", {"logical": "markets"}, self._markets, _coverage(market_days), _cutoff(first_pit).isoformat() if first_pit else None),
            "ordered_trans": component("fquant.published_ordered_trans", {"logical": "tdx_ordered_trans", "root_env": ORDERED_TRANS_ROOT_ENV}, self._minute, _coverage(minute_days), None),
            "callauction": call_component,
        }

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def run_manifest(self) -> dict[str, object]:
        if self._closed:
            raise RuntimeError("research reader is closed")
        return json.loads(json.dumps(self._manifest))

    def daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        research_symbol = _research_symbol(symbol)
        frame = self._canonical.daily_bars(research_symbol, start, end)
        rows = frame.to_dicts()
        required = ("raw_open", "raw_high", "raw_low", "raw_close", "volume")
        if rows and not all(field in rows[0] for field in required):
            raise RuntimeError("canonical daily raw columns unavailable")
        return [{"trade_date": row["date"] if isinstance(row.get("date"), date) else row["trade_date"], "open": float(row["raw_open"]), "high": float(row["raw_high"]), "low": float(row["raw_low"]), "close": float(row["raw_close"]), "volume": float(row["volume"])} for row in rows]

    def suspended_dates(self, symbol: str, start: date, end: date) -> list[date]:
        traded = {row["trade_date"] for row in self.daily_bars(symbol, start, end)}
        return [day for day in self._canonical.market_days(start, end) if day not in traded]

    def minute_bars(self, symbol: str, trade_date: date) -> list[MinuteBar]:
        research_symbol = _research_symbol(symbol)
        try:
            rows = self._minute.minute_bars(research_symbol, trade_date)
        except (OrderedTransIntegrityError, OSError, ValueError):
            return []
        return [{"timestamp": row.ts, "open": row.open, "high": row.high, "low": row.low, "close": row.close, "volume": row.volume} for row in rows]

    def auction_snapshot(self, symbol: str, trade_date: date) -> AuctionEvidence | None:
        if self._callauction is None:
            return None
        row = self._callauction.preopen_final(_research_symbol(symbol), trade_date)
        if row is None:
            return None
        return {"open_price": row.price, "matched_volume": row.matched_volume, "tick_index": row.tick_index, "event_time": row.event_time}

    def ticks(self, symbol: str, trade_date: date) -> tuple[()]:
        return ()

    def order_book_snapshots(self, symbol: str, trade_date: date) -> tuple[()]:
        return ()

    def pit_snapshot(self, symbol: str, as_of: date) -> PITRecord | None:
        created_at = _parse_created_at(self._markets.created_at() if callable(getattr(self._markets, "created_at", None)) else None)
        effective = _cutoff(as_of)
        if created_at is None or created_at > effective:
            return None
        row = self._markets.limit_regime_facts(symbol, as_of, as_of).get(as_of)
        if row is None:
            return None
        pct = _regime_pct(str(row.get("regime", "")))
        if pct is None or row.get("is_st") is None or row.get("limit_up_price") is None:
            return None
        return {"effective_at": effective, "available_at": created_at, "limit_up_pct": pct, "limit_down_pct": pct, "is_st": bool(row["is_st"]), "float_shares": None, "limit_up_price": float(row["limit_up_price"])}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for child in (self._callauction, self._minute, self._markets, self._canonical):
            close = getattr(child, "close", None)
            if callable(close):
                close()


def build_production_reader(repo: Any, signal_year: int) -> WeakToStrongProductionReader | None:
    acquired: list[Any] = []
    try:
        canonical = PublishedCanonicalDailyReader.from_repository(repo)
        if canonical is None:
            return None
        acquired.append(canonical)
        markets = PublishedDailyMarketFactsReader.from_repository(repo)
        acquired.append(markets)
        minute_root = os.getenv(ORDERED_TRANS_ROOT_ENV) or DEFAULT_ORDERED_TRANS_ROOT
        minute = PublishedOrderedTransMinuteReader(minute_root)
        acquired.append(minute)
        try:
            callauction = PublishedCallAuctionReader(signal_year)
            acquired.append(callauction)
        except Exception:
            callauction = None
        reader = WeakToStrongProductionReader(canonical, markets, minute, callauction)
        if not set(MINIMUM_CAPABILITIES).issubset(reader.capabilities()):
            reader.close()
            return None
        return reader
    except Exception:
        for child in reversed(acquired):
            close = getattr(child, "close", None)
            if callable(close):
                close()
        return None


@contextmanager
def production_reader_scope(repo: Any, signal_year: int) -> Iterator[WeakToStrongProductionReader | None]:
    reader = build_production_reader(repo, signal_year)
    try:
        yield reader
    finally:
        if reader is not None:
            reader.close()


__all__ = ["WeakToStrongProductionReader", "build_production_reader", "production_reader_scope"]
