"""Full-market adapter for the single-yang-no-break increment research.

Binds the runner's pinned composite provenance (``context.reader``) to the
exact first-board readers already exposed by the repository: the
generation-pinned canonical daily reader plus the market facts reader pinned
by that same canonical manifest. The COMPLETE PIT cohort is handed to
``evaluate_single_yang_increment`` exactly once — no batching, no fallback.

Fail-closed contract: when the specialized readers are missing, when their
sealed generations do not match the runner's pinned composite, or when the
OOS boundary is unusable, the adapter returns an explicit ``unavailable``
verdict. Missing exact PIT limit facts fail closed inside the evaluator
(baseline arm and overall verdict become unavailable); this adapter never
substitutes approximate facts.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.single_yang_no_break import (
    DEFAULT_HOLD_HORIZONS,
    INCREMENT_RESEARCH_ID,
    SINGLE_YANG_DEFINITION,
    SingleYangCompositeReader,
    evaluate_single_yang_increment,
)

ADAPTER_NAME = "single-yang-no-break"
DEFAULT_COST_BPS = 10.0
PRELOAD_COLUMNS = (
    "symbol",
    "date",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "open",
    "close",
    "raw_volume",
    "limit_up_price",
    "generation",
)


@dataclass(frozen=True)
class SingleYangFullMarketRequest:
    """JSON-safe echoable request for one full-market increment evaluation."""

    start: date
    end: date
    symbols: list[str]
    oos_start: date | None
    cost_bps: float
    hold_horizons: tuple[int, ...] = DEFAULT_HOLD_HORIZONS

    def echo(self) -> dict[str, Any]:
        return {
            "research_id": INCREMENT_RESEARCH_ID,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols),
            "oos_start": self.oos_start.isoformat() if self.oos_start is not None else None,
            "cost_bps": self.cost_bps,
            "hold_horizons": list(self.hold_horizons),
        }


def _identity_mismatch(pin: dict[str, Any], canonical: Any, markets: Any) -> str | None:
    """Return a fail-closed reason unless both readers match the pinned composite."""
    canonical_pin = pin.get("canonical")
    markets_pin = pin.get("markets")
    if not isinstance(canonical_pin, dict) or not isinstance(markets_pin, dict):
        return "pinned_composite_provenance_incomplete"
    if (canonical_pin.get("generation"), canonical_pin.get("manifest_sha256")) != (
        canonical.generation(),
        canonical.manifest_sha256(),
    ):
        return "canonical_generation_mismatch_with_pinned_reader"
    if (markets_pin.get("generation"), markets_pin.get("manifest_sha256")) != (
        markets.generation(),
        markets.manifest_sha256(),
    ):
        return "markets_generation_mismatch_with_pinned_reader"
    return None


def _close_quietly(reader: Any) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        with suppress(OSError, RuntimeError, TypeError, ValueError):
            close()


class SingleYangFullMarketAdapter:
    """Run ``evaluate_single_yang_increment`` once over the full pinned cohort."""

    name = ADAPTER_NAME

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> SingleYangFullMarketRequest:
        if parameters is not None:
            reject_unsupported_parameters(parameters, {"start", "end", "oos_start", "cost_bps"})
            start, end = parameters["start"], parameters["end"]
            oos_start, cost_bps = parameters["oos_start"], parameters["cost_bps"]
        return SingleYangFullMarketRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start,
            cost_bps=cost_bps if cost_bps is not None else DEFAULT_COST_BPS,
        )

    def evaluate(
        self, context: RunnerContext, request: SingleYangFullMarketRequest
    ) -> dict[str, Any]:
        pinned = getattr(context, "reader", None)
        repo = getattr(context, "repo", None)
        if pinned is None or repo is None:
            return self._unavailable(request, ["full_market_pinned_context_missing"])
        pin_source = getattr(pinned, "source_provenance", None)
        pin = pin_source() if callable(pin_source) else None
        if not isinstance(pin, dict):
            return self._unavailable(request, ["pinned_composite_provenance_missing"])
        canonical = getattr(repo, "generation_pinned_daily_reader", None)
        if canonical is None:
            return self._unavailable(request, ["generation_pinned_reader_missing"])
        markets = getattr(repo, "generation_pinned_market_facts_reader", None)
        if markets is None:
            return self._unavailable(request, ["market_facts_reader_missing"])
        mismatch = _identity_mismatch(pin, canonical, markets)
        if mismatch is not None:
            _close_quietly(markets)
            return self._unavailable(request, [mismatch])
        if request.oos_start is None:
            _close_quietly(markets)
            return self._unavailable(request, ["oos_start_required"])
        if not request.start <= request.oos_start <= request.end:
            _close_quietly(markets)
            return self._unavailable(request, ["oos_start_outside_window"])
        if not request.symbols:
            _close_quietly(markets)
            return self._unavailable(request, ["cohort_empty"])
        preload = getattr(canonical, "preload_panel", None)
        if callable(preload):
            try:
                preload(
                    request.start - timedelta(days=30),
                    request.end + timedelta(days=140),
                    symbols=list(request.symbols),
                    columns=[
                        column for column in PRELOAD_COLUMNS if column in set(canonical.columns())
                    ],
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _close_quietly(markets)
                envelope = self._unavailable(request, ["preload_panel_failed"])
                envelope["preload_error"] = str(exc)
                return envelope
        composite = SingleYangCompositeReader(canonical, markets)
        try:
            verdict = evaluate_single_yang_increment(
                reader=composite,
                start=request.start,
                end=request.end,
                symbols=list(request.symbols),
                oos_start=request.oos_start,
                cost_bps=request.cost_bps,
                hold_horizons=list(request.hold_horizons),
            )
        finally:
            _close_quietly(markets)
        if verdict.get("status") != "ok" and "unavailable_reasons" not in verdict:
            verdict["unavailable_reasons"] = list(verdict.get("reasons", []))
        return {**verdict, "request": request.echo()}

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return verdict

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None

    @staticmethod
    def _unavailable(request: SingleYangFullMarketRequest, reasons: list[str]) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "research_id": INCREMENT_RESEARCH_ID,
            "reasons": list(reasons),
            "unavailable_reasons": list(reasons),
            "missing_columns": [],
            "definition": dict(SINGLE_YANG_DEFINITION),
            "provenance": {},
            "request": request.echo(),
        }


__all__ = [
    "ADAPTER_NAME",
    "DEFAULT_COST_BPS",
    "SingleYangFullMarketAdapter",
    "SingleYangFullMarketRequest",
]
