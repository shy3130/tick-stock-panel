"""Full-market adapter tests for the N-shape pullback-depth factor.

Locks the adapter seam: the COMPLETE cohort reaches
``evaluate_n_shape_pullback_depth`` exactly once, and the single verdict keeps
all five arms (A/B/C, unstratified, C-plus-golden-phoenix) with independent verdicts, volume
-overlap evidence and sealed-reader provenance. Missing dedicated reader is
explicitly unavailable — no fallback.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services.full_market_adapters import n_depth as n_depth_module
from app.services.full_market_adapters.n_depth import NDepthAdapter, NDepthRequest
from app.services.full_market_research import FactorAdapter, RunnerContext
from app.services.n_shape_pullback_depth import evaluate_n_shape_pullback_depth

FACTOR_MODULE = n_depth_module
ALL_ARMS = frozenset({"A", "B", "C", "unstratified", "bucket_c_golden_phoenix"})
COHORT = ["000001.SZ", "000002.SZ", "600000.SH", "600519.SH"]


def _row(
    day: date,
    close: float,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    volume: float = 1_000.0,
):
    return {
        "date": day,
        "raw_open": open_ if open_ is not None else close,
        "close": close,
        "raw_high": high,
        "raw_low": low,
        "raw_close": close,
        "volume": volume,
    }


def _confirmed_rows() -> tuple[list[dict], list[date]]:
    """Confirmed N-pattern: breakout on day 5 emits one bucket-A event."""
    start = date(2024, 1, 2)
    values = [
        (100.0, 101.0, 100.0),
        (110.0, 111.0, 105.0),
        (120.0, 120.0, 110.0),
        (115.0, 116.0, 112.0),
        (108.0, 114.0, 108.0),
        (121.0, 122.0, 109.0),
    ]
    rows = [
        _row(start + timedelta(days=i), close, high=high, low=low)
        for i, (close, high, low) in enumerate(values)
    ]
    for i in range(6, 40):
        close = 121.0 + i / 10
        rows.append(
            _row(
                start + timedelta(days=i),
                close,
                open_=close - 0.2,
                high=close + 0.5,
                low=close - 0.5,
            )
        )
    return rows, [row["date"] for row in rows]


class _Reader:
    """Minimal composite reader exposing the confirmed fixture for one symbol."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def manifest_sha256(self):
        return "a" * 64

    def source_provenance(self):
        return {
            "canonical": {"generation": "canonical-g1", "manifest_sha256": "b" * 64},
            "markets": {"generation": "markets-g1", "manifest_sha256": "c" * 64},
        }

    def generation(self):
        return "composite-g1"

    def provider_id(self):
        return "test"

    def market_days(self, start: date, end: date):
        return [row["date"] for row in self.rows if start <= row["date"] <= end]

    def universe(self, start: date, end: date):
        return ["000001.SZ"]

    def daily_bars(self, symbol: str, start: date, end: date):
        return pl.DataFrame([row for row in self.rows if start <= row["date"] <= end])

    def limit_regime_facts(self, symbol: str, start: date, end: date):
        return {}


class _PreloadReader(_Reader):
    """Reader exposing a callable preload_panel that records invocations."""

    def __init__(self, rows: list[dict], log: list):
        super().__init__(rows)
        self.log = log

    def preload_panel(self, start: date, end: date, *, symbols: list[str]):
        self.log.append(("preload", start, end, list(symbols)))


class _Repo:
    """Generation-pinned dedicated reader attribute holder."""

    def __init__(self, reader):
        self.n_shape_research_reader = reader


def _window(rows: list[dict]) -> tuple[date, date]:
    return rows[0]["date"], rows[5]["date"]


def test_adapter_satisfies_factor_protocol():
    adapter = NDepthAdapter()
    assert isinstance(adapter, FactorAdapter)
    assert adapter.name == "n-depth"


def test_build_request_carries_complete_cohort_unchanged():
    adapter = NDepthAdapter()
    request = adapter.build_request(
        date(2024, 1, 2),
        date(2024, 2, 15),
        list(COHORT),
        oos_start=date(2024, 2, 1),
        cost_bps=None,
    )
    assert isinstance(request, NDepthRequest)
    assert request.symbols == COHORT
    assert request.oos_start == date(2024, 2, 1)
    assert request.cost_bps == 20.0  # evaluator's preregistered round-trip cost

    explicit = adapter.build_request(
        date(2024, 1, 2),
        date(2024, 2, 15),
        list(COHORT),
        oos_start=None,
        cost_bps=35.0,
    )
    assert explicit.cost_bps == 35.0
    assert explicit.oos_start is None


def test_evaluate_calls_evaluator_exactly_once_with_full_cohort(monkeypatch):
    calls: list[dict] = []
    sentinel_verdict = {"status": "ok", "coverage": {"symbols_total": len(COHORT)}}
    dedicated_reader = _Reader([])

    def _spy(**kwargs):
        calls.append(kwargs)
        return sentinel_verdict

    monkeypatch.setattr(FACTOR_MODULE, "evaluate_n_shape_pullback_depth", _spy)
    adapter = NDepthAdapter()
    request = adapter.build_request(
        date(2024, 1, 2),
        date(2024, 2, 15),
        list(COHORT),
        oos_start=None,
        cost_bps=None,
    )
    context = RunnerContext(repo=_Repo(dedicated_reader), reader=dedicated_reader)
    verdict = adapter.evaluate(context, request)

    assert len(calls) == 1  # single evaluation, no batched verdict splicing
    assert calls[0]["symbols"] == COHORT  # complete cohort, unsplit
    assert calls[0]["reader"] is dedicated_reader  # runner-pinned composite reader
    assert calls[0]["cost_bps"] == 20.0
    assert verdict is sentinel_verdict


def test_single_verdict_keeps_all_arms_overlap_and_provenance():
    rows, _calendar = _confirmed_rows()
    start, end = _window(rows)
    adapter = NDepthAdapter()
    request = adapter.build_request(start, end, ["000001.SZ"], oos_start=None, cost_bps=None)
    context = RunnerContext(repo=_Repo(_Reader(rows)), reader=_Reader(rows))
    verdict = adapter.evaluate(context, request)

    assert verdict["status"] == "ok"
    populations = verdict["research"]["populations"]
    assert set(populations) == ALL_ARMS  # A/B/C + unstratified + C-plus-golden-phoenix
    for arm, population in populations.items():
        assert isinstance(population["verdict"], str), arm
        assert population["verdict"] in {
            "accepted",
            "rejected",
            "unavailable_insufficient_samples",
        }, arm

    # Each arm carries its own comparator and incremental verdict inputs.
    assert populations["bucket_c_golden_phoenix"]["incremental_10d_vs"] == "C"
    assert populations["A"]["incremental_10d_vs"] == "unstratified"

    # Volume-overlap evidence survives on the emitted event.
    event = verdict["events"][0]
    assert event["bucket"] == "A"
    assert "golden_phoenix" in event
    assert "golden_phoenix_available" in event
    assert set(event["volume_ratios"]) == {"pullback_vs_high", "pullback_vs_pre20"}

    # Sealed-generation provenance is preserved verbatim.
    provenance = verdict["provenance"]
    assert provenance["reader"]["generation"] == "composite-g1"
    assert provenance["reader"]["manifest_sha256"] == "a" * 64
    assert provenance["reader"]["provider_id"] == "test"
    assert provenance["sources"]["canonical"]["generation"] == "canonical-g1"

    # serialize/coverage seam.
    assert adapter.serialize_verdict(verdict) is verdict
    coverage = adapter.extract_coverage(verdict)
    assert coverage == {
        "symbols_total": 1,
        "events": 1,
        "censored": 0,
        "structure_failures": 0,
    }
    # Real evaluator reference: the adapter must stay on this exact entrypoint.
    direct = evaluate_n_shape_pullback_depth(
        start=start, end=end, symbols=["000001.SZ"], reader=_Reader(rows)
    )
    assert direct["research"]["populations"].keys() == populations.keys()


def test_missing_dedicated_reader_is_explicitly_unavailable():
    adapter = NDepthAdapter()
    request = adapter.build_request(
        date(2024, 1, 2),
        date(2024, 1, 31),
        ["000001.SZ"],
        oos_start=None,
        cost_bps=None,
    )
    context = RunnerContext(repo=SimpleNamespace(), reader=object())
    verdict = adapter.evaluate(context, request)

    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["n_shape_research_reader_missing"]
    assert verdict["events"] == []
    assert verdict["research"] is None
    assert adapter.extract_coverage(verdict) is None


def test_preload_panel_called_once_before_evaluator_with_warmup_window(monkeypatch):
    log: list = []
    start = date(2024, 1, 2)
    end = date(2024, 2, 15)
    reader = _PreloadReader([], log)
    sentinel_verdict = {"status": "ok"}

    def _spy(**kwargs):
        log.append(("evaluate", kwargs))
        return sentinel_verdict

    monkeypatch.setattr(FACTOR_MODULE, "evaluate_n_shape_pullback_depth", _spy)
    adapter = NDepthAdapter()
    request = adapter.build_request(start, end, list(COHORT), oos_start=None, cost_bps=None)
    verdict = adapter.evaluate(RunnerContext(repo=_Repo(reader), reader=reader), request)

    preloads = [entry for entry in log if entry[0] == "preload"]
    assert preloads == [("preload", start - timedelta(days=400), end + timedelta(days=180), COHORT)]
    assert log[0][0] == "preload" and log[1][0] == "evaluate"
    assert len(log) == 2  # exactly one preload + exactly one evaluator call
    assert verdict is sentinel_verdict
