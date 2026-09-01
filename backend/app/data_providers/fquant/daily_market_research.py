"""Immutable fstore markets reader for point-in-time research.

``PublishedDailyMarketFactsReader`` reads ONE published ``markets``
generation and never follows ``current``.  ``from_canonical_manifest``
additionally binds the reader to the markets generation recorded by the
canonical history manifest (``source_generations["markets"]``) and, when the
canonical manifest records a markets manifest hash, verifies the identity of
the resolved generation against it (PIT source pin, fail closed).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.data_providers.fquant.generation import current_path, root_for
from app.data_providers.fquant.symbols import code_to_symbol, symbol_to_code
from app.storage.duckdb_runtime import connect_duckdb

# Raw quote columns in daily_markets (direct columns or payload_json keys).
_RAW_QUOTES = (
    ("jrkpj", "Jrkpj"),  # raw open (今日开盘价)
    ("zgj", "Zgj"),  # raw high (最高价)
    ("zdj", "Zdj"),  # raw low (最低价)
    ("price", "Price"),  # raw close (当日收盘价; 昨收为 zrspj/Zrspj)
)
REGIME_PCT = {
    "main_10": 0.10,
    "st_5": 0.05,
    "chinext_20": 0.20,
    "star_20": 0.20,
    "beijing_30": 0.30,
}

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MarketFact:
    """One symbol/day of point-in-time markets facts.

    ``suspended``/``sellable``/``buyable`` are ``None`` unless the snapshot
    proves them; callers must treat ``None`` as unknown and fail closed.
    """

    raw_open: float | None
    raw_high: float | None
    raw_low: float | None
    raw_close: float | None
    pre_close: float | None
    published_limit_up: float | None
    published_limit_down: float | None
    regime: str | None
    is_st: bool | None
    name: str | None
    suspended: bool | None = None
    sellable: bool | None = None
    buyable: bool | None = None
    signal_limit_up: bool | None = None
    signal_limit_down: bool | None = None


@dataclass(frozen=True)
class DailyTurnoverFact:
    """Exact-day reported turnover, semantically available after market close."""

    reported_turnover_pct: float
    available_at: datetime
    source_day: date
    availability_basis: str = "daily_market_close"


@dataclass(frozen=True)
class IntradayFloatSharesFact:
    """Lagged float shares known before the current trading session."""

    float_shares: float
    available_at: datetime
    source_day: date
    availability_basis: str = "previous_daily_market_close"


@dataclass(frozen=True)
class PinnedMarketFacts:
    generation: str
    manifest_sha256: str
    rows: Mapping[tuple[str, date], MarketFact]


def load_pinned_market_facts(
    canonical_manifest: Mapping[str, Any],
    symbols: list[str] | tuple[str, ...],
    market_days: list[date] | tuple[date, ...],
) -> PinnedMarketFacts:
    """Load markets facts pinned by the canonical manifest, never markets current."""
    reader = PublishedDailyMarketFactsReader.from_canonical_manifest(canonical_manifest)
    rows: dict[tuple[str, date], MarketFact] = {}
    try:
        for symbol in symbols:
            if not market_days:
                continue
            facts = reader.limit_band_facts(symbol, min(market_days), max(market_days))
            for day in market_days:
                fact = facts.get(day)
                if fact is not None:
                    rows[(symbol, day)] = fact
        return PinnedMarketFacts(reader.generation(), reader.manifest_sha256(), rows)
    finally:
        reader.close()


class PublishedDailyMarketFactsReader:
    """Read one published ``markets`` generation, never following ``current``."""

    _FIELDS = ("price", "ztj", "zrspj")

    def __init__(self, db_path: str, generation: str, manifest_bytes: bytes) -> None:
        self._path = db_path
        self._generation = generation
        self._manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            self._manifest = json.loads(manifest_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("markets manifest is invalid") from exc
        self._lock = threading.Lock()
        self._closed = False
        self._conn = connect_duckdb(db_path, read_only=True)
        try:
            with self._lock:
                tables = self._conn.execute("SHOW TABLES").fetchall()
                if not any(str(row[0]) == "daily_markets" for row in tables):
                    raise ValueError("published markets snapshot lacks daily_markets")
                columns = self._conn.execute("PRAGMA table_info('daily_markets')").fetchall()
            names = {str(row[1]).lower() for row in columns}
            self._has_float_shares = "ltgb" in names
            self._has_reported_turnover = "hslv" in names
            self._column_names = names
            for required in ("code", "asset_type"):
                if required not in names:
                    raise ValueError(f"daily_markets lacks required column {required}")
            if "trade_date" in names:
                self._date_col = "trade_date"
            elif "tdate" in names:
                self._date_col = "tdate"
            else:
                raise ValueError("daily_markets lacks trade_date/tdate")
            self._direct_fields = {field: field in names for field in self._FIELDS}
            self._has_payload_json = "payload_json" in names
            if not self._has_payload_json and not all(self._direct_fields.values()):
                missing = [f for f, present in self._direct_fields.items() if not present]
                raise ValueError(f"daily_markets lacks required fields: {missing}")
            self._quote_columns = {
                lower: (lower if lower in names else upper if upper in names else None)
                for lower, upper in _RAW_QUOTES
            }
            self._has_name = "name" in names
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    @classmethod
    def from_repository(cls, repo: Any) -> PublishedDailyMarketFactsReader:
        path = current_path("markets")
        if not path:
            raise FileNotFoundError("no published markets snapshot")
        db = Path(path)
        if db.is_symlink() or not db.is_file():
            raise FileNotFoundError(f"markets snapshot is not a regular file: {db}")
        manifest_path = db.parent / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise FileNotFoundError(f"markets manifest is missing: {manifest_path}")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("markets manifest is invalid") from exc
        generation = db.parent.name
        if not isinstance(manifest, dict) or manifest.get("generation") != generation:
            raise ValueError("markets manifest generation mismatch")
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise ValueError("markets manifest entries are missing")
        entry = next(
            (e for e in entries if isinstance(e, dict) and e.get("logical") == "markets"), None
        )
        if entry is None or Path(str(entry.get("file", ""))).name != db.name:
            raise ValueError("markets manifest does not pin snapshot file")
        return cls(str(db), generation, manifest_bytes)

    @classmethod
    def from_canonical_manifest(
        cls, canonical_manifest: Mapping[str, Any]
    ) -> PublishedDailyMarketFactsReader:
        """Resolve the immutable markets generation recorded by canonical manifest.

        The pin is ``canonical_manifest["source_generations"]["markets"]``: a
        generation string or a mapping with ``generation`` and optionally
        ``manifest_sha256``.  When the canonical manifest records a markets
        manifest hash, the resolved manifest must match it exactly (fail
        closed on any mismatch).  ``current`` is never consulted.
        """
        sources = canonical_manifest.get("source_generations")
        pinned = sources.get("markets") if isinstance(sources, Mapping) else None
        if isinstance(pinned, Mapping):
            generation = pinned.get("generation")
            expected_hash = pinned.get("manifest_sha256")
            if not isinstance(expected_hash, str) or not expected_hash:
                raise ValueError("canonical markets pin missing manifest_sha256")
        else:
            generation, expected_hash = pinned, None
        if not isinstance(generation, str) or not generation:
            raise ValueError("canonical markets generation pin missing")
        root = root_for("markets")
        if not root:
            raise FileNotFoundError("markets snapshot root unavailable")
        gen_dir = Path(root) / generation
        manifest_path = gen_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise FileNotFoundError("pinned markets generation unavailable")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("pinned markets manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("generation") != generation:
            raise ValueError("pinned markets manifest mismatch")
        entries = manifest.get("entries")
        entry = next(
            (e for e in entries or [] if isinstance(e, dict) and e.get("logical") == "markets"),
            None,
        )
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise ValueError("pinned markets entry missing")
        file_name = Path(entry["file"]).name
        db = gen_dir / file_name
        if db.is_symlink() or not db.is_file() or db.parent != gen_dir:
            raise FileNotFoundError("pinned markets db unavailable")
        resolved_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if expected_hash is not None and str(expected_hash) != resolved_hash:
            raise ValueError("markets manifest identity mismatch")
        reader = cls(str(db), generation, manifest_bytes)
        reader._pin_hash_expected = expected_hash if isinstance(expected_hash, str) else None
        reader._pin_hash_resolved = resolved_hash
        reader._pin_verified = expected_hash is not None and str(expected_hash) == resolved_hash
        reader._pin_verification_mode = (
            "manifest_sha256_match" if expected_hash is not None else "missing_expected_hash"
        )
        return reader

    # ------------------------------------------------------------------
    # identity / lifecycle
    # ------------------------------------------------------------------
    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def pin_identity_verified(self) -> bool:
        """Whether canonical generation/manifest identity is verified."""
        return bool(getattr(self, "_pin_verified", False))

    def pin_verification_mode(self) -> str:
        return getattr(self, "_pin_verification_mode", "unbound_reader")

    def pin_manifest_sha256(self) -> str:
        return getattr(self, "_pin_hash_resolved", "") or self._manifest_sha256

    def provider_id(self) -> str:
        return "fquant.published_markets"

    def created_at(self) -> str | None:
        value = self._manifest.get("created_at")
        return value if isinstance(value, str) else None

    # ------------------------------------------------------------------
    # value expressions
    # ------------------------------------------------------------------
    def _value_expr(self, field: str) -> str:
        direct = f'TRY_CAST("{field}" AS DOUBLE)'
        source_key = {"price": "Price", "ztj": "Ztj", "zrspj": "Zrspj"}[field]
        payload = (
            "TRY_CAST(NULLIF(COALESCE("
            f"payload_json->>'{field}', payload_json->>'{source_key}'"
            "), '') AS DOUBLE)"
        )
        if self._direct_fields[field] and self._has_payload_json:
            return f"COALESCE({direct}, {payload})"
        if self._direct_fields[field]:
            return direct
        return payload

    def _name_expr(self) -> str:
        direct = "NULLIF(CAST(\"name\" AS VARCHAR), '')"
        payload = "NULLIF(COALESCE(payload_json->>'name', payload_json->>'Name'), '')"
        if self._has_payload_json and "name" in self._column_names:
            return f"COALESCE({direct}, {payload})"
        return direct if "name" in self._column_names else payload

    def _quote_expr(self, lower: str, upper: str) -> str:
        direct_col = self._quote_columns[lower]
        direct = f'TRY_CAST("{direct_col}" AS DOUBLE)' if direct_col else "CAST(NULL AS DOUBLE)"
        payload = (
            f"TRY_CAST(NULLIF(COALESCE(payload_json->>'{lower}', "
            f"payload_json->>'{upper}'), '') AS DOUBLE)"
        )
        if direct_col and self._has_payload_json:
            return f"COALESCE({direct}, {payload})"
        if direct_col:
            return direct
        if self._has_payload_json:
            return payload
        return "CAST(NULL AS DOUBLE)"

    def _ztj_expr(self) -> str:
        return self._value_expr("ztj")

    def _query(self, sql: str, params: list[object]) -> list[tuple]:
        with self._lock:
            if self._closed:
                raise RuntimeError("research reader is closed")
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------
    def market_days(self, start: date, end: date) -> list[date]:
        rows = self._query(
            f'SELECT DISTINCT "{self._date_col}"::DATE FROM daily_markets '
            f'WHERE asset_type = 1 AND "{self._date_col}" BETWEEN ? AND ? '
            f"AND {self._value_expr('price')} > 0 ORDER BY 1",
            [start, end],
        )
        return [row[0] for row in rows if isinstance(row[0], date)]

    def universe(self, start: date, end: date) -> list[str]:
        rows = self._query(
            f"SELECT DISTINCT code FROM daily_markets WHERE asset_type = 1 "
            f'AND "{self._date_col}" BETWEEN ? AND ? AND {self._value_expr("price")} > 0 '
            "AND code IS NOT NULL ORDER BY code",
            [start, end],
        )
        return [code_to_symbol(str(row[0]), 1) for row in rows if row[0] is not None]

    @staticmethod
    def _regime(symbol: str, day: date) -> str | None:
        code = symbol_to_code(symbol)
        if not code.isdigit() or len(code) != 6:
            return None
        if code.startswith(("688", "689")):
            return "star_20" if day >= date(2019, 7, 22) else None
        if code.startswith(("300", "301")):
            return "chinext_20" if day >= date(2020, 8, 24) else "main_10"
        if code_to_symbol(code, 1).endswith(".BJ"):
            return "beijing_30" if day >= date(2021, 11, 15) else None
        return "main_10"

    def limit_regime_facts(
        self, symbol: str, start: date, end: date
    ) -> dict[date, dict[str, object]]:
        ztj, name = self._ztj_expr(), self._name_expr()
        rows = self._query(
            f'SELECT "{self._date_col}"::DATE, {ztj}, {name} FROM daily_markets '
            f'WHERE asset_type = 1 AND code = ? AND "{self._date_col}" BETWEEN ? AND ? '
            f"AND {ztj} > 0 AND {name} IS NOT NULL ORDER BY 1",
            [symbol_to_code(symbol), start, end],
        )
        result: dict[date, dict[str, object]] = {}
        for day, limit_price, stock_name in rows:
            text = str(stock_name).strip() if stock_name else ""
            if not isinstance(day, date) or limit_price is None or not text:
                continue
            base_regime = self._regime(symbol, day)
            if base_regime is None:
                continue
            is_st = "ST" in text.upper()
            result[day] = {
                "limit_up_price": float(limit_price),
                "name": text,
                "is_st": is_st,
                "regime": "st_5" if is_st else base_regime,
            }
        return result

    def limit_band_facts(self, symbol: str, start: date, end: date) -> dict[date, MarketFact]:
        """Raw quotes + published ztj + derived bands for one symbol.

        Bands need ``pre_close`` (zrspj, yesterday close) and a known regime;
        either missing leaves the band ``None``. Limit signals are derived only
        when the raw bar and its band both exist; absent evidence stays ``None``.
        """
        date_col = self._date_col
        selects = ", ".join(self._quote_expr(lo, up) for lo, up in _RAW_QUOTES)
        sql = (
            f'SELECT "{date_col}"::DATE, {selects}, {self._value_expr("zrspj")}, {self._ztj_expr()}, {self._name_expr()} '
            f"FROM daily_markets WHERE asset_type = 1 AND code = ? "
            f'AND "{date_col}" BETWEEN ? AND ? ORDER BY 1'
        )
        rows = self._query(sql, [symbol_to_code(symbol), start, end])
        regime_by_day = {
            day: self._regime(symbol, day) for day, *_ in rows if isinstance(day, date)
        }
        result: dict[date, MarketFact] = {}
        for day, raw_open, raw_high, raw_low, raw_close, pre_close_raw, ztj, stock_name in rows:
            if not isinstance(day, date):
                continue
            base_regime = regime_by_day.get(day)
            # Fail closed: an incomplete row is not provable point-in-time
            # evidence.  Emitting a half-fact (e.g. a band without its raw
            # bar or pre_close) would let consumers trade on guesses, so the
            # day is omitted entirely and ``get`` returns ``None``.
            if (
                raw_open is None
                or raw_high is None
                or raw_low is None
                or raw_close is None
                or pre_close_raw is None
                or ztj is None
                or base_regime is None
            ):
                continue
            text = str(stock_name).strip() if stock_name else ""
            if not text:
                continue
            is_st = "ST" in text.upper()
            regime = "st_5" if is_st else base_regime
            pre_close = float(pre_close_raw)
            upper = float(ztj)
            lower = round(pre_close * (1 - REGIME_PCT[regime]), 2)
            signal_up = (
                True
                if upper is not None and raw_high is not None and float(raw_high) >= upper - 0.005
                else (False if upper is not None and raw_high is not None else None)
            )
            signal_down = (
                True
                if lower is not None and raw_low is not None and float(raw_low) <= lower + 0.005
                else (False if lower is not None and raw_low is not None else None)
            )
            result[day] = MarketFact(
                raw_open=float(raw_open) if raw_open is not None else None,
                raw_high=float(raw_high) if raw_high is not None else None,
                raw_low=float(raw_low) if raw_low is not None else None,
                raw_close=float(raw_close) if raw_close is not None else None,
                pre_close=pre_close,
                published_limit_up=upper,
                published_limit_down=lower,
                regime=regime,
                is_st=is_st,
                name=text or None,
                signal_limit_up=signal_up,
                signal_limit_down=signal_down,
            )
        return result

    def limit_signals(
        self, symbol: str, start: date, end: date
    ) -> dict[date, dict[str, bool | None]]:
        """Derived ``signal_limit_up``/``signal_limit_down`` per day."""
        facts = self.limit_band_facts(symbol, start, end)
        out: dict[date, dict[str, bool | None]] = {}
        for day, fact in facts.items():
            up = down = None
            if fact.published_limit_up is not None and fact.raw_high is not None:
                up = float(fact.raw_high) >= fact.published_limit_up - 0.005
            if fact.published_limit_down is not None and fact.raw_low is not None:
                down = float(fact.raw_low) <= fact.published_limit_down + 0.005
            out[day] = {"signal_limit_up": up, "signal_limit_down": down}
        return out

    def get(self, symbol: str, day: date) -> MarketFact | None:
        """One day of facts for a symbol, or ``None`` when the row is absent.

        Consumers must treat missing/None evidence as unknown and fail
        closed; this reader never fabricates suspended/buyable/sellable.
        """
        facts = self.limit_band_facts(symbol, day, day)
        fact = facts.get(day)
        if fact is None:
            return None
        if fact.published_limit_up is not None and fact.raw_high is None:
            return None  # band without its raw bar is not provable evidence
        return fact

    @staticmethod
    def _market_close(day: date) -> datetime:
        return datetime.combine(day, time(15, 0), tzinfo=_SHANGHAI)

    def daily_turnover_fact(self, symbol: str, day: date) -> DailyTurnoverFact | None:
        """Return exact-day ``hslv`` for close-confirmed, T+1 research.

        ``hslv`` is a percentage-point market fact: ``0.47`` means ``0.47%``.
        The pinned daily row is therefore semantically available at that day's
        close, independently of when the immutable snapshot was later built.
        """
        if not self._has_reported_turnover:
            return None
        rows = self._query(
            f"SELECT TRY_CAST(hslv AS DOUBLE) FROM daily_markets "
            f'WHERE asset_type = 1 AND code = ? AND "{self._date_col}" = ? LIMIT 1',
            [symbol_to_code(symbol), day],
        )
        if not rows or rows[0][0] is None:
            return None
        value = float(rows[0][0])
        if not math.isfinite(value) or value < 0.0 or value > 100.0:
            return None
        return DailyTurnoverFact(
            reported_turnover_pct=value,
            available_at=self._market_close(day),
            source_day=day,
        )

    def intraday_float_shares_fact(self, symbol: str, day: date) -> IntradayFloatSharesFact | None:
        """Return the latest strictly-prior daily ``ltgb`` observation.

        Same-day ``daily_markets`` is a close-complete row and must never prove
        intraday availability.  Lagging the denominator by one observed market
        day makes every returned value available before the requested session.
        """
        if not self._has_float_shares:
            return None
        rows = self._query(
            f'SELECT "{self._date_col}", TRY_CAST(ltgb AS DOUBLE) '
            f"FROM daily_markets WHERE asset_type = 1 AND code = ? "
            f'AND "{self._date_col}" < ? AND TRY_CAST(ltgb AS DOUBLE) > 0 '
            f'ORDER BY "{self._date_col}" DESC LIMIT 1',
            [symbol_to_code(symbol), day],
        )
        if not rows:
            return None
        source_day, raw_value = rows[0]
        if not isinstance(source_day, date) or raw_value is None:
            return None
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0.0:
            return None
        return IntradayFloatSharesFact(
            float_shares=value,
            available_at=self._market_close(source_day),
            source_day=source_day,
        )

    def escape_risk_facts(
        self, symbols: list[str] | tuple[str, ...], day: date
    ) -> dict[str, tuple[MarketFact, IntradayFloatSharesFact | None]]:
        """Batch exact-day bands with strictly-prior float-share facts."""
        normalized = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not normalized:
            return {}
        codes = [symbol_to_code(symbol) for symbol in normalized]
        placeholders = ",".join("?" for _ in codes)
        selects = ", ".join(self._quote_expr(lo, up) for lo, up in _RAW_QUOTES)
        rows = self._query(
            f"SELECT code, {selects}, {self._value_expr('zrspj')}, "
            f"{self._ztj_expr()}, {self._name_expr()} "
            "FROM daily_markets WHERE asset_type = 1 "
            f'AND "{self._date_col}" = ? AND code IN ({placeholders})',
            [day, *codes],
        )
        prior_by_code: dict[str, tuple[date, float]] = {}
        if self._has_float_shares:
            prior_rows = self._query(
                f'SELECT code, "{self._date_col}", TRY_CAST(ltgb AS DOUBLE) '
                "FROM daily_markets WHERE asset_type = 1 "
                f'AND "{self._date_col}" < ? AND code IN ({placeholders}) '
                "AND TRY_CAST(ltgb AS DOUBLE) > 0 "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY code "
                f'ORDER BY "{self._date_col}" DESC) = 1',
                [day, *codes],
            )
            for code, source_day, raw_value in prior_rows:
                if isinstance(source_day, date) and raw_value is not None:
                    value = float(raw_value)
                    if math.isfinite(value) and value > 0.0:
                        prior_by_code[str(code)] = (source_day, value)

        result: dict[str, tuple[MarketFact, IntradayFloatSharesFact | None]] = {}
        for (
            code,
            raw_open,
            raw_high,
            raw_low,
            raw_close,
            pre_close_raw,
            ztj,
            stock_name,
        ) in rows:
            symbol = code_to_symbol(str(code), 1)
            base_regime = self._regime(symbol, day)
            if (
                base_regime is None
                or raw_open is None
                or raw_high is None
                or raw_low is None
                or raw_close is None
                or pre_close_raw is None
                or ztj is None
            ):
                continue
            text = str(stock_name).strip() if stock_name else ""
            if not text:
                continue
            is_st = "ST" in text.upper()
            regime = "st_5" if is_st else base_regime
            pre_close = float(pre_close_raw)
            upper = float(ztj)
            lower = round(pre_close * (1 - REGIME_PCT[regime]), 2)
            fact = MarketFact(
                raw_open=float(raw_open),
                raw_high=float(raw_high),
                raw_low=float(raw_low),
                raw_close=float(raw_close),
                pre_close=pre_close,
                published_limit_up=upper,
                published_limit_down=lower,
                regime=regime,
                is_st=is_st,
                name=text or None,
                signal_limit_up=float(raw_high) >= upper - 0.005,
                signal_limit_down=float(raw_low) <= lower + 0.005,
            )
            prior = prior_by_code.get(str(code))
            turnover = (
                IntradayFloatSharesFact(
                    float_shares=prior[1],
                    available_at=self._market_close(prior[0]),
                    source_day=prior[0],
                )
                if prior is not None
                else None
            )
            result[symbol] = (fact, turnover)
        return result

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
