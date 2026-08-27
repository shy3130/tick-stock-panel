"""Immutable fstore markets reader for point-in-time N-shape research."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

from app.data_providers.fquant.generation import current_path
from app.data_providers.fquant.symbols import code_to_symbol, symbol_to_code
from app.storage.duckdb_runtime import connect_duckdb


class PublishedDailyMarketFactsReader:
    """Read one published ``markets`` generation, never following ``current``."""

    _FIELDS = ("price", "ztj")

    def __init__(self, db_path: str, generation: str, manifest_bytes: bytes) -> None:
        self._path = db_path
        self._generation = generation
        self._manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
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
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    @classmethod
    def from_repository(cls, repo: Any) -> "PublishedDailyMarketFactsReader":
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

    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def provider_id(self) -> str:
        return "fquant.published_markets"

    def _value_expr(self, field: str) -> str:
        direct = f'TRY_CAST("{field}" AS DOUBLE)'
        source_key = {"price": "Price", "ztj": "Ztj"}[field]
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

    def _query(self, sql: str, params: list[object]) -> list[tuple]:
        with self._lock:
            if self._closed:
                raise RuntimeError("research reader is closed")
            return self._conn.execute(sql, params).fetchall()

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
            return "star_20"
        if code.startswith(("300", "301")):
            return "chinext_20" if day >= date(2020, 8, 24) else "main_10"
        if code_to_symbol(code, 1).endswith(".BJ"):
            return "beijing_30"
        return "main_10"

    def limit_regime_facts(
        self, symbol: str, start: date, end: date
    ) -> dict[date, dict[str, object]]:
        ztj, name = self._value_expr("ztj"), self._name_expr()
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
            is_st = "ST" in text.upper()
            regime = "st_5" if is_st else self._regime(symbol, day)
            if regime is not None:
                result[day] = {
                    "limit_up_price": float(limit_price),
                    "name": text,
                    "is_st": is_st,
                    "regime": regime,
                }
        return result

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
