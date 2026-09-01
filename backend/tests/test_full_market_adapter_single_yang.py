from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import polars as pl

from app.services.full_market_adapters import single_yang as adapter_module
from app.services.full_market_research import RunnerContext
from app.services.single_yang_no_break import SingleYangCompositeReader

CANONICAL_GENERATION = "canonical-generation-1"
CANONICAL_MANIFEST = "a" * 64
MARKETS_GENERATION = "markets-generation-1"
MARKETS_MANIFEST = "b" * 64
DAYS = [date(2026, 1, 1) + timedelta(days=index) for index in range(30)]


@dataclass(frozen=True)
class _Fact:
    published_limit_up: float


class _Canonical:
    def __init__(
        self,
        generation: str = CANONICAL_GENERATION,
        manifest: str = CANONICAL_MANIFEST,
        preload_error: str | None = None,
    ):
        self._generation = generation
        self._manifest = manifest
        self._preload_error = preload_error
        self.preload_calls: list[tuple[date, date, list[str], list[str] | None]] = []

    def generation(self) -> str:
        return self._generation

    def manifest_sha256(self) -> str:
        return self._manifest

    def preload_panel(
        self,
        start: date,
        end: date,
        *,
        symbols: list[str],
        columns: list[str] | None = None,
    ) -> None:
        self.preload_calls.append((start, end, list(symbols), columns))
        if self._preload_error is not None:
            raise RuntimeError(self._preload_error)

    def columns(self) -> tuple[str, ...]:
        return (
            "date",
            "open",
            "high",
            "low",
            "close",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "raw_volume",
            "generation",
        )

    def market_days(self, start: date, end: date) -> list[date]:
        return [day for day in DAYS if start <= day <= end]

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        del start, end
        rows = []
        has_signal = symbol == "000001"
        for index, day in enumerate(DAYS):
            raw_open, raw_high, raw_low, raw_close = 10.0, 10.6, 10.0, 10.1
            volume = 1000.0
            if has_signal and index == 3:
                raw_close = 11.0
            if has_signal and index == 4:
                raw_open, raw_high, raw_low, raw_close, volume = 10.2, 10.6, 9.95, 10.5, 800.0
            if has_signal and 5 <= index <= 9:
                raw_low, raw_close, volume = 10.0, 10.1, 700.0
            if has_signal and index == 10:
                raw_low, raw_close, volume = 10.0, 10.1, 700.0
            rows.append(
                {
                    "date": day,
                    "open": raw_open,
                    "high": raw_high,
                    "low": raw_low,
                    "close": raw_close,
                    "raw_open": raw_open,
                    "raw_high": raw_high,
                    "raw_low": raw_low,
                    "raw_close": raw_close,
                    "raw_volume": volume,
                    "generation": self._generation,
                }
            )
        return pl.DataFrame(rows)


class _Markets:
    def __init__(self, empty: bool = False):
        self.empty = empty
        self.closed = False

    def generation(self) -> str:
        return MARKETS_GENERATION

    def manifest_sha256(self) -> str:
        return MARKETS_MANIFEST

    def limit_band_facts(self, symbol: str, start: date, end: date) -> dict[date, _Fact]:
        del symbol
        if self.empty:
            return {}
        return {day: _Fact(11.0) for day in DAYS if start <= day <= end}

    def close(self) -> None:
        self.closed = True


class _PinnedComposite:
    def generation(self) -> str:
        return f"canonical:{CANONICAL_GENERATION}|markets:{MARKETS_GENERATION}"

    def manifest_sha256(self) -> str:
        return "c" * 64

    def provider_id(self) -> str:
        return "fquant.published_n_shape_research"

    def source_provenance(self) -> dict[str, dict[str, str]]:
        return {
            "canonical": {
                "generation": CANONICAL_GENERATION,
                "manifest_sha256": CANONICAL_MANIFEST,
            },
            "markets": {
                "generation": MARKETS_GENERATION,
                "manifest_sha256": MARKETS_MANIFEST,
            },
        }

    def universe(self, start: date, end: date) -> list[str]:
        del start, end
        return ["000001", "000002", "000003"]


_MISSING = object()


class _Repo:
    def __init__(self, canonical: Any | None = None, markets: Any = _MISSING):
        self._canonical = canonical if canonical is not None else _Canonical()
        self._markets = _Markets() if markets is _MISSING else markets

    @property
    def generation_pinned_daily_reader(self) -> Any:
        return self._canonical

    @property
    def generation_pinned_market_facts_reader(self) -> Any:
        return self._markets


def _context(repo: _Repo | None = None) -> RunnerContext:
    return RunnerContext(repo=repo or _Repo(), reader=_PinnedComposite())


def _request(adapter: Any, *, oos_start: date | None = DAYS[10]):
    return adapter.build_request(
        DAYS[0],
        DAYS[-1],
        ["000001", "000002", "000003"],
        oos_start=oos_start,
        cost_bps=None,
    )


def test_full_cohort_is_evaluated_once_with_exact_composite(monkeypatch):
    adapter = adapter_module.SingleYangFullMarketAdapter()
    canonical = _Canonical()
    events: list[str] = []
    calls: list[dict[str, Any]] = []
    real = adapter_module.evaluate_single_yang_increment

    def spy(**kwargs: Any) -> dict[str, Any]:
        events.append("evaluate")
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(adapter_module, "evaluate_single_yang_increment", spy)
    original_preload = canonical.preload_panel

    def preload_spy(
        start: date,
        end: date,
        *,
        symbols: list[str],
        columns: list[str] | None = None,
    ) -> None:
        events.append("preload")
        original_preload(start, end, symbols=symbols, columns=columns)

    canonical.preload_panel = preload_spy
    verdict = adapter.evaluate(_context(_Repo(canonical=canonical)), _request(adapter))

    assert events == ["preload", "evaluate"]
    assert canonical.preload_calls == [
        (
            DAYS[0] - timedelta(days=30),
            DAYS[-1] + timedelta(days=140),
            ["000001", "000002", "000003"],
            [
                "date",
                "raw_open",
                "raw_high",
                "raw_low",
                "raw_close",
                "open",
                "close",
                "raw_volume",
                "generation",
            ],
        )
    ]
    assert len(calls) == 1
    assert calls[0]["symbols"] == ["000001", "000002", "000003"]
    assert isinstance(calls[0]["reader"], SingleYangCompositeReader)
    assert calls[0]["oos_start"] == DAYS[10]
    assert calls[0]["cost_bps"] == 10.0
    assert verdict["status"] == "ok"
    assert verdict["request"]["symbols"] == ["000001", "000002", "000003"]


def test_exact_limit_facts_drive_first_board_and_provenance():
    adapter = adapter_module.SingleYangFullMarketAdapter()
    verdict = adapter.evaluate(_context(), _request(adapter))

    assert verdict["status"] == "ok"
    assert verdict["provenance"]["limit_fact_column_present"] is True
    assert verdict["arms"]["baseline"]["status"] == "ok"
    assert verdict["coverage"]["baseline"]["first_boards_detected"] == 1
    assert verdict["coverage"]["pattern"]["events_detected"] == 1
    assert verdict["provenance"]["manifest_sha256"] == CANONICAL_MANIFEST
    assert verdict["provenance"]["sources"]["canonical"]["manifest_sha256"] == CANONICAL_MANIFEST
    assert verdict["provenance"]["sources"]["market_facts"]["manifest_sha256"] == MARKETS_MANIFEST


def test_missing_exact_limit_facts_keeps_overall_verdict_unavailable():
    adapter = adapter_module.SingleYangFullMarketAdapter()
    markets = _Markets(empty=True)
    verdict = adapter.evaluate(_context(_Repo(markets=markets)), _request(adapter))

    assert verdict["status"] == "ok"
    assert verdict["arms"]["baseline"]["status"] == "unavailable"
    assert verdict["verdict"]["value"] == "unavailable"
    assert verdict["coverage"]["baseline"]["first_boards_detected"] == 0


def test_manifest_mismatch_fails_closed_without_evaluator(monkeypatch):
    adapter = adapter_module.SingleYangFullMarketAdapter()
    calls: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(adapter_module, "evaluate_single_yang_increment", spy)
    verdict = adapter.evaluate(
        _context(_Repo(canonical=_Canonical(generation="different-generation"))),
        _request(adapter),
    )

    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["canonical_generation_mismatch_with_pinned_reader"]
    assert calls == []


def test_missing_market_reader_fails_closed():
    adapter = adapter_module.SingleYangFullMarketAdapter()
    verdict = adapter.evaluate(_context(_Repo(markets=None)), _request(adapter))

    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["market_facts_reader_missing"]


def test_oos_boundary_is_required_and_validated():
    adapter = adapter_module.SingleYangFullMarketAdapter()

    missing = adapter.evaluate(_context(), _request(adapter, oos_start=None))
    outside = adapter.evaluate(
        _context(),
        adapter.build_request(
            DAYS[0], DAYS[-1], ["000001"], oos_start=DAYS[-1] + timedelta(days=1), cost_bps=None
        ),
    )

    assert missing["unavailable_reasons"] == ["oos_start_required"]
    assert outside["unavailable_reasons"] == ["oos_start_outside_window"]


def test_preload_failure_is_explicit_unavailable(monkeypatch):
    adapter = adapter_module.SingleYangFullMarketAdapter()
    canonical = _Canonical(preload_error="panel scan exploded")
    calls: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(adapter_module, "evaluate_single_yang_increment", spy)
    verdict = adapter.evaluate(_context(_Repo(canonical=canonical)), _request(adapter))

    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["preload_panel_failed"]
    assert verdict["preload_error"] == "panel scan exploded"
    assert len(canonical.preload_calls) == 1
    assert calls == []
