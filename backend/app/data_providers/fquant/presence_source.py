"""Narrow fquant-layer queries for pinned presence-source DuckDB files.

Universe presence history needs two files pinned by one source manifest:

- fstore DuckDB: the A-share ``trade_date`` calendar
- markets DuckDB: exact-day stock rows from ``daily_markets``

This module owns the only read-only queries for those files. It closes every
connection deterministically and maps source failures to
``PresenceSourceError``. The service layer must reach it through
``FQuantProvider.read_presence_pinned_source`` rather than opening DuckDB
itself.

The caller supplies absolute paths from one ``PresenceSourcePin``. This module
does not resolve manifests or ``current.json`` and holds no persistent handles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

_CODE_RE = re.compile(r"^\d{6}$")
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


class PresenceSourceError(RuntimeError):
    """The pinned presence source is unavailable or inconsistent."""


@dataclass(frozen=True, slots=True)
class PresencePinnedSourceData:
    """Source facts required to build a presence schema-v2 generation."""

    market_days: tuple[date, ...]
    coverage_start: date
    coverage_end: date
    symbols_by_day: dict[date, set[str]]


def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _read_market_calendar(conn: Any) -> tuple[date, ...]:
    rows = conn.execute(
        "SELECT tdate FROM trade_date WHERE isopen = 3 AND mkt = 'A股' ORDER BY tdate"
    ).fetchall()
    out = []
    for r in rows:
        d = _as_date(r[0])
        if d is None or (out and d <= out[-1]):
            raise PresenceSourceError("invalid market calendar")
        out.append(d)
    if not out:
        raise PresenceSourceError("empty market calendar")
    return tuple(out)


def _read_daily_markets(conn: Any) -> tuple[date, date, dict[date, set[str]]]:
    from app.data_providers.fquant.symbols import code_to_symbol

    rows = conn.execute(
        "SELECT code, trade_date FROM daily_markets WHERE asset_type = 1"
    ).fetchall()
    if not rows:
        raise PresenceSourceError("empty coverage")
    seen = set()
    out: dict[date, set[str]] = {}
    for code0, day0 in rows:
        day = _as_date(day0)
        code = "" if code0 is None else str(code0)
        if day is None or not _CODE_RE.fullmatch(code):
            raise PresenceSourceError("invalid code/date")
        key = (code, day)
        if key in seen:
            raise PresenceSourceError("duplicate key")
        seen.add(key)
        sym = code_to_symbol(code, 1)
        if not _SYMBOL_RE.fullmatch(sym):
            raise PresenceSourceError("invalid mapped symbol")
        out.setdefault(day, set()).add(sym)
    return min(out), max(out), out


def read_presence_pinned_source(*, markets_path: str, fstore_path: str) -> PresencePinnedSourceData:
    """Read both pinned source files with deterministic connection cleanup.

    Opening, querying, and source-integrity failures raise
    ``PresenceSourceError``.
    """
    from app.storage.duckdb_runtime import connect_duckdb

    try:
        mc = connect_duckdb(markets_path, read_only=True)
    except Exception as e:
        raise PresenceSourceError(f"markets source open failed: {markets_path}") from e
    try:
        fc = connect_duckdb(fstore_path, read_only=True)
    except Exception as e:
        mc.close()
        raise PresenceSourceError(f"fstore source open failed: {fstore_path}") from e
    try:
        market_days = _read_market_calendar(fc)
        coverage_start, coverage_end, symbols_by_day = _read_daily_markets(mc)
    except PresenceSourceError:
        raise
    except Exception as e:
        raise PresenceSourceError("pinned presence source query failed") from e
    finally:
        fc.close()
        mc.close()
    return PresencePinnedSourceData(market_days, coverage_start, coverage_end, symbols_by_day)
