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
