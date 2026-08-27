"""Pinned, immutable call-auction evidence for weak-to-strong research."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.data_providers.fquant import generation
from app.data_providers.fquant.symbols import exchange_of, split_symbol
from app.storage.duckdb_runtime import connect_duckdb

_GENERATION_RE = re.compile(r"^\d{8}T\d{6}$")


class CallAuctionIntegrityError(RuntimeError):
    """Published call-auction snapshot is unavailable or malformed."""


@dataclass(frozen=True, slots=True)
class PublishedCallAuctionFinal:
    tick_index: int
    event_time: str
    price: float
    matched_volume: float


class PublishedCallAuctionReader:
    """Pin one signal-year ``open`` auction generation for a reader lifetime."""

    def __init__(self, signal_year: int, *, connection: Any | None = None) -> None:
        if not isinstance(signal_year, int) or not 2000 <= signal_year <= 2100:
            raise ValueError("signal_year must be a four-digit year")
        self._year = signal_year
        self._logical = f"tdx_callauction_{signal_year}"
        root = generation.root_for(self._logical)
        if not root:
            raise CallAuctionIntegrityError("call-auction snapshot root unavailable")
        self._root = os.path.realpath(root)
        try:
            current_bytes = (Path(self._root) / "current.json").read_bytes()
            pointer = json.loads(current_bytes)
            gen = pointer["generation"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CallAuctionIntegrityError("invalid call-auction current.json") from exc
        if not isinstance(gen, str) or not _GENERATION_RE.fullmatch(gen):
            raise CallAuctionIntegrityError("invalid call-auction generation")
        generation_dir = Path(self._root) / gen
        manifest_path = generation_dir / "manifest.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, ValueError) as exc:
            raise CallAuctionIntegrityError("invalid call-auction manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("generation") != gen:
            raise CallAuctionIntegrityError("call-auction manifest generation mismatch")
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise CallAuctionIntegrityError("call-auction manifest entries missing")
        entry = next((item for item in entries if isinstance(item, dict) and item.get("logical") == self._logical), None)
        if entry is None or not isinstance(entry.get("file"), str):
            raise CallAuctionIntegrityError("call-auction database is not pinned to signal year")
        db_path = Path(os.path.realpath(generation_dir / entry["file"]))
        if generation_dir not in db_path.parents or not db_path.is_file():
            raise CallAuctionIntegrityError("call-auction database escapes generation")
        self._generation = gen
        self._manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self._manifest = manifest
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = connection if connection is not None else connect_duckdb(self._path, read_only=True)
        try:
            tables = self._conn.execute("SHOW TABLES").fetchall()
            if not any(str(row[0]) == "market_call_auction_results" for row in tables):
                raise CallAuctionIntegrityError("call-auction results table missing")
        except Exception:
            if connection is None:
                self._conn.close()
            self._closed = True
            raise

    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def provider_id(self) -> str:
        return "fquant.published_call_auction"

    def route(self) -> dict[str, str]:
        return {"logical": self._logical, "root_env": "FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION"}

    def coverage(self) -> dict[str, object]:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT min(trade_date)::TEXT, max(trade_date)::TEXT, count(DISTINCT code) FROM market_call_auction_results"
            ).fetchone()
        return {"first_day": row[0] if row else None, "last_day": row[1] if row else None, "symbols": int(row[2] or 0) if row else 0}

    def preopen_final(self, symbol: str, trade_date: date) -> PublishedCallAuctionFinal | None:
        code, suffix = split_symbol(symbol)
        if not suffix:
            suffix = exchange_of(code)
        if suffix not in {"SH", "SZ", "BJ"}:
            raise CallAuctionIntegrityError("unknown market for call-auction symbol")
        prefixed = f"{suffix.lower()}{code}"
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                """SELECT tick_index, event_time, price, volume
                   FROM market_call_auction_results
                   WHERE code = ? AND trade_date = ? AND session = 'open'
                   ORDER BY tick_index DESC""",
                [prefixed, trade_date],
            ).fetchall()
        if not rows:
            return None
        max_tick = rows[0][0]
        finals = [row for row in rows if row[0] == max_tick]
        if len(finals) != 1:
            return None
        tick_index, event_time, price, volume = finals[0]
        if tick_index is None or event_time is None or price is None or float(price) <= 0:
            return None
        try:
            parsed_time = datetime.strptime(str(event_time), "%H:%M:%S").time()
        except ValueError:
            return None
        if parsed_time != time(9, 25):
            return None
        return PublishedCallAuctionFinal(int(tick_index), str(event_time), float(price), float(volume or 0))

    def _ensure_open(self) -> None:
        if self._closed:
            raise CallAuctionIntegrityError("call-auction reader closed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


__all__ = ["CallAuctionIntegrityError", "PublishedCallAuctionFinal", "PublishedCallAuctionReader"]
