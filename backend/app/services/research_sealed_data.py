"""Immutable adapters for auditable research over published canonical history."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.services.canonical_history import resolve_published_history


class PublishedCanonicalDailyReader:
    """Pin one published canonical generation for the lifetime of a research run.

    The reader never follows ``current.json`` after construction and never merges the
    mutable local overlay. Missing columns remain missing so callers can fail closed.
    """

    def __init__(self, repo: Any) -> None:
        published = resolve_published_history(Path(repo._external_enriched_root))
        if published is None:
            raise RuntimeError("canonical_history_not_published")
        manifest, generation_dir = published
        manifest_path = generation_dir / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        generation = manifest.get("generation")
        columns = manifest.get("columns")
        if not isinstance(generation, str) or not generation:
            raise RuntimeError("canonical_generation_invalid")
        if not isinstance(columns, list) or not all(isinstance(value, str) for value in columns):
            raise RuntimeError("canonical_columns_invalid")

        days: list[date] = []
        for entry in generation_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("date="):
                continue
            try:
                days.append(date.fromisoformat(entry.name.removeprefix("date=")))
            except ValueError:
                continue
        if not days:
            raise RuntimeError("canonical_calendar_empty")

        self._repo = repo
        self._manifest = dict(manifest)
        self._generation_dir = generation_dir
        self._generation = generation
        self._columns = tuple(columns)
        self._manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        self._market_days = tuple(sorted(set(days)))
        self._preloaded_panel: pl.DataFrame | None = None
        self._preloaded_ranges: dict[str, tuple[int, int]] = {}
        self._preloaded_start: date | None = None
        self._preloaded_end: date | None = None

    @classmethod
    def from_repository(cls, repo: Any) -> PublishedCanonicalDailyReader | None:
        try:
            return cls(repo)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._manifest_hash

    def version(self) -> str:
        return f"canonical:{self._generation}:{self._manifest_hash[:16]}"

    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest)

    def columns(self) -> tuple[str, ...]:
        return self._columns

    def has_columns(self, *columns: str) -> bool:
        return all(column in self._columns for column in columns)

    def market_days(self, start: date, end: date) -> list[date]:
        if start > end:
            raise ValueError("start must be <= end")
        return [value for value in self._market_days if start <= value <= end]

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        if start > end:
            raise ValueError("start must be <= end")
        normalized_symbol = symbol.strip().upper()
        if (
            self._preloaded_panel is not None
            and set(self._columns).issubset(self._preloaded_panel.columns)
            and self._preloaded_start is not None
            and self._preloaded_end is not None
            and self._preloaded_start <= start
            and end <= self._preloaded_end
        ):
            location = self._preloaded_ranges.get(normalized_symbol)
            if location is None:
                return self._preloaded_panel.head(0)
            offset, length = location
            frame = self._preloaded_panel.slice(offset, length)
            return frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        sources = self._repo._external_partition_sources(
            self._generation_dir,
            start=start,
            end=end,
        )
        if sources == ():
            return pl.DataFrame()
        parquet_source: str | tuple[str, ...]
        if sources is None:
            parquet_source = str(self._generation_dir / "**" / "*.parquet")
        else:
            parquet_source = sources
        return self._repo._scan_unique_enriched(
            parquet_source,
            start=start,
            end=end,
            columns=list(self._columns),
            symbols=[symbol],
            layout_cache_key=str(self._generation_dir),
        )

    def daily_panel(
        self,
        start: date,
        end: date,
        *,
        symbols: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """Scan one pinned OHLCV panel once for offline full-market research."""
        if start > end:
            raise ValueError("start must be <= end")
        selected_columns = list(self._columns) if columns is None else list(dict.fromkeys(columns))
        missing = sorted(set(selected_columns) - set(self._columns))
        if missing:
            raise ValueError(f"canonical generation lacks requested columns: {missing}")
        sources = self._repo._external_partition_sources(
            self._generation_dir,
            start=start,
            end=end,
        )
        if sources == ():
            return pl.DataFrame()
        parquet_source: str | tuple[str, ...]
        if sources is None:
            parquet_source = str(self._generation_dir / "**" / "*.parquet")
        else:
            parquet_source = sources
        normalized_symbols = (
            sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
            if symbols is not None
            else None
        )
        return self._repo._scan_unique_enriched(
            parquet_source,
            start=start,
            end=end,
            columns=selected_columns,
            symbols=normalized_symbols,
            layout_cache_key=str(self._generation_dir),
        )

    def preload_panel(
        self,
        start: date,
        end: date,
        *,
        symbols: list[str],
        columns: list[str] | None = None,
    ) -> int:
        """Materialize one column-pruned scan and zero-copy symbol ranges."""
        selected_columns = set(self._columns if columns is None else columns)
        missing_keys = sorted({"symbol", "date"} - selected_columns)
        if missing_keys:
            raise ValueError(f"preloaded canonical panel lacks key columns: {missing_keys}")
        panel = self.daily_panel(start, end, symbols=symbols, columns=columns)
        if not panel.is_empty() and "symbol" in panel.columns:
            panel = panel.sort(["symbol", "date"])
        ranges: dict[str, tuple[int, int]] = {}
        offset = 0
        if not panel.is_empty() and "symbol" in panel.columns:
            for symbol, length in panel.group_by("symbol", maintain_order=True).len().iter_rows():
                normalized = str(symbol).strip().upper()
                count = int(length)
                ranges[normalized] = (offset, count)
                offset += count
        self._preloaded_panel = panel
        self._preloaded_ranges = ranges
        self._preloaded_start = start
        self._preloaded_end = end
        return len(ranges)

    def daily_closes(self, start: date, end: date) -> pl.DataFrame:
        """Batch canonical close/volume panel over all symbols (exact-day research).

        Column-pruned whole-universe scan of the pinned generation, deduplicated
        with the repository's deterministic ``unique(symbol, date, keep='last')``
        policy.  Callers must prove ``close``/``volume`` availability via
        :meth:`has_columns` before relying on the volume column.
        """
        if start > end:
            raise ValueError("start must be <= end")
        wanted = [
            column for column in ("symbol", "date", "close", "volume") if column in self._columns
        ]
        if "symbol" not in wanted or "date" not in wanted or "close" not in wanted:
            raise ValueError("canonical generation lacks symbol/date/close columns")
        sources = self._repo._external_partition_sources(
            self._generation_dir,
            start=start,
            end=end,
        )
        if sources == ():
            return pl.DataFrame()
        parquet_source: str | tuple[str, ...]
        if sources is None:
            parquet_source = str(self._generation_dir / "**" / "*.parquet")
        else:
            parquet_source = sources
        return self._repo._scan_unique_enriched(
            parquet_source,
            start=start,
            end=end,
            columns=wanted,
            layout_cache_key=str(self._generation_dir),
        )
