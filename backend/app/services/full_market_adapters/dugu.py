"""Full-market offline adapter for the frozen dugu-trend daily-event scan.

Performance contract (offline runner): BEFORE the frozen 32-cell scan the
adapter issues ONE ``preload_panel`` call (``start-400d .. end+120d``, complete
cohort) so canonical per-symbol frames are materialized once, and freezes ONE
``PinnedMarketFacts`` bundle for the complete cohort over that calendar. All 32
cells reuse both; per-symbol reader queries happen exactly once — never per
cell-by-symbol. Preload or facts-prefetch failure resolves to the explicit
top-level ``unavailable`` envelope; there is no fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.data_providers.fquant.daily_market_research import (
    PinnedMarketFacts,
    PublishedDailyMarketFactsReader,
)
from app.services.daily_event_research.dugu_trend import (
    DUGU_SCAN_SCHEMA,
    dugu_scan_cell_id,
    iter_dugu_scan_grid,
)
from app.services.daily_event_research.evaluation import evaluate_daily_events
from app.services.daily_event_research.models import COST_BPS_DEFAULT
from app.services.full_market_adapters.pinning import source_reader_matches
from app.services.full_market_research import RunnerContext
from app.services.research_sealed_data import PublishedCanonicalDailyReader

DUGU_FULL_MARKET_SCHEMA = "tickflow.research.full-market-dugu-scan.v1"
PRELOAD_LOOKBACK_DAYS = 400
PRELOAD_FORWARD_DAYS = 120
_READER_ERRORS = (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class DuguCellRequest:
    """Duck-typed evaluator request without the interactive API cohort cap."""

    detector_id: str
    variant: str
    band_mode: str
    require_m3: bool
    alignment_days: int
    symbols: list[str]
    start: date
    oos_start: date
    end: date
    horizon_days: int
    cost_bps: float


@dataclass(frozen=True, slots=True)
class DuguScanRequest:
    """Immutable full-market request for one frozen dugu scan-grid run."""

    start: date
    end: date
    symbols: list[str]
    oos_start: date
    cost_bps: float


class DuguTrendAdapter:
    """Full-market dugu-trend research: every frozen grid cell, one cohort."""

    name = "dugu-trend"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> DuguScanRequest:
        if oos_start is None:
            raise ValueError("dugu_oos_start_required")
        return DuguScanRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start,
            cost_bps=COST_BPS_DEFAULT if cost_bps is None else cost_bps,
        )

    def evaluate(self, context: RunnerContext, request: DuguScanRequest) -> dict[str, Any]:
        echo = self._request_echo(request)
        canonical = PublishedCanonicalDailyReader.from_repository(context.repo)
        if canonical is None:
            return self._unavailable(echo, "unavailable_canonical_reader")
        if not source_reader_matches(context, "canonical", canonical):
            return self._unavailable(echo, "pinned_canonical_generation_mismatch")
        try:
            facts = PublishedDailyMarketFactsReader.from_canonical_manifest(canonical.manifest())
        except (OSError, RuntimeError, TypeError, ValueError):
            return self._unavailable(echo, "unavailable_market_facts")
        if not source_reader_matches(context, "markets", facts):
            close = getattr(facts, "close", None)
            if callable(close):
                close()
            return self._unavailable(echo, "pinned_markets_generation_mismatch")

        preload_start = request.start - timedelta(days=PRELOAD_LOOKBACK_DAYS)
        preload_end = request.end + timedelta(days=PRELOAD_FORWARD_DAYS)
        grid = iter_dugu_scan_grid()
        cells: dict[str, Any] = {}
        try:
            try:
                canonical.preload_panel(preload_start, preload_end, symbols=list(request.symbols))
            except _READER_ERRORS:
                return self._unavailable(echo, "unavailable_canonical_reader")
            try:
                bundle = self._prefetch_market_facts(
                    canonical, facts, request.symbols, preload_start, preload_end
                )
            except _READER_ERRORS:
                return self._unavailable(echo, "unavailable_market_facts")
            for config in grid:
                cell_request = DuguCellRequest(
                    detector_id="dugu_trend",
                    variant=config.variant,
                    band_mode=config.band_mode,
                    require_m3=config.require_m3,
                    alignment_days=config.alignment_days,
                    symbols=list(request.symbols),
                    start=request.start,
                    oos_start=request.oos_start,
                    end=request.end,
                    horizon_days=20,
                    cost_bps=request.cost_bps,
                )
                response = evaluate_daily_events(cell_request, canonical, bundle)
                cells[dugu_scan_cell_id(config)] = response.model_dump(mode="json")
        finally:
            close = getattr(facts, "close", None)
            if callable(close):
                close()
        return {
            "schema": DUGU_FULL_MARKET_SCHEMA,
            "status": "ok",
            "request": echo,
            "scan_grid": {
                "schema": DUGU_SCAN_SCHEMA,
                "cell_count": len(grid),
                "cell_ids": sorted(cells),
            },
            "cells": cells,
        }

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return verdict

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        cells = verdict.get("cells")
        if not isinstance(cells, dict):
            return None
        return {
            cell_id: cell.get("coverage")
            for cell_id, cell in sorted(cells.items())
            if isinstance(cell, dict)
        }

    @staticmethod
    def _prefetch_market_facts(
        canonical: Any,
        facts: Any,
        symbols: list[str],
        preload_start: date,
        preload_end: date,
    ) -> PinnedMarketFacts:
        """Freeze exact market facts once for the whole cohort and calendar.

        Mirrors the freeze branch of
        ``hold_firm_patterns.adapters.pinned_market_facts_source``: one
        ``limit_band_facts`` query per symbol over the calendar span, then one
        immutable bundle. Passing the bundle (not the raw reader) to the
        evaluator keeps every cell off the per-symbol reader path.
        """
        calendar = canonical.market_days(preload_start, preload_end)
        rows: dict[tuple[str, date], object] = {}
        if calendar:
            fact_start, fact_end = min(calendar), max(calendar)
            load = facts.limit_band_facts
            for symbol in symbols:
                for day, fact in load(symbol, fact_start, fact_end).items():
                    rows[(symbol, day)] = fact
        return PinnedMarketFacts(
            generation=str(facts.generation()),
            manifest_sha256=str(facts.manifest_sha256()),
            rows=rows,
        )

    def _request_echo(self, request: DuguScanRequest) -> dict[str, Any]:
        return {
            "factor": self.name,
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
            "oos_start": request.oos_start.isoformat(),
            "cost_bps": request.cost_bps,
            "symbols": len(request.symbols),
            "scan_grid_schema": DUGU_SCAN_SCHEMA,
        }

    def _unavailable(self, echo: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "schema": DUGU_FULL_MARKET_SCHEMA,
            "status": "unavailable",
            "request": echo,
            "unavailable_reasons": [reason],
            "scan_grid": {
                "schema": DUGU_SCAN_SCHEMA,
                "cell_count": len(iter_dugu_scan_grid()),
                "cell_ids": [],
            },
            "cells": {},
        }
