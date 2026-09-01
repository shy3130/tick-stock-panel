"""Composite sealed reader for N-shape research data."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import polars as pl

from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader
from app.services.research_sealed_data import PublishedCanonicalDailyReader


class PublishedNShapeResearchReader:
    """Pin canonical OHLCV and markets facts as one research source."""

    def __init__(self, repo: Any) -> None:
        canonical = PublishedCanonicalDailyReader.from_repository(repo)
        if canonical is None:
            raise RuntimeError("n_shape_canonical_source_unavailable")
        facts = PublishedDailyMarketFactsReader.from_canonical_manifest(canonical.manifest())
        if facts is None:
            raise RuntimeError("n_shape_market_facts_source_unavailable")
        self._canonical = canonical
        self._facts = facts
        self._generation = f"canonical:{canonical.generation()}|markets:{facts.generation()}"
        self._hash = hashlib.sha256(
            f"canonical:{canonical.manifest_sha256()}|markets:{facts.manifest_sha256()}".encode()
        ).hexdigest()

    @classmethod
    def from_repository(cls, repo: Any) -> PublishedNShapeResearchReader | None:
        try:
            return cls(repo)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._hash

    def provider_id(self) -> str:
        return "fquant.published_n_shape_research"

    def source_provenance(self) -> dict[str, dict[str, str]]:
        return {
            "canonical": {
                "generation": self._canonical.generation(),
                "manifest_sha256": self._canonical.manifest_sha256(),
            },
            "markets": {
                "generation": self._facts.generation(),
                "manifest_sha256": self._facts.manifest_sha256(),
            },
        }

    def market_days(self, start: date, end: date) -> list[date]:
        return self._canonical.market_days(start, end)

    def universe(self, start: date, end: date) -> list[str]:
        return self._facts.universe(start, end)

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        return self._canonical.daily_bars(symbol, start, end)

    def daily_panel(
        self,
        start: date,
        end: date,
        *,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """Read the canonical full-market OHLCV panel in one pinned scan."""
        return self._canonical.daily_panel(start, end, symbols=symbols)

    def preload_panel(
        self,
        start: date,
        end: date,
        *,
        symbols: list[str],
    ) -> int:
        """Cache a single canonical scan for repeated per-symbol evaluation."""
        return self._canonical.preload_panel(start, end, symbols=symbols)

    def daily_closes(self, start: date, end: date) -> pl.DataFrame:
        """Read the canonical full-market close panel in one pinned scan."""
        return self._canonical.daily_closes(start, end)

    def limit_regime_facts(
        self, symbol: str, start: date, end: date
    ) -> dict[date, dict[str, object]]:
        return self._facts.limit_regime_facts(symbol, start, end)

    def close(self) -> None:
        close = getattr(self._facts, "close", None)
        if callable(close):
            close()
