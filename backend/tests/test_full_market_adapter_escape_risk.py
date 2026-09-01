"""Full-market escape-risk adapter contract tests.

Locks the four adapter-level guarantees for the offline full-market runner:
the COMPLETE cohort reaches the evaluator exactly once, the adapter closes the
intraday reader it opened (and never closes runner-owned readers), S10 keeps
the strict prior-close float-share availability censor, and a missing/failing
intraday reader fails closed instead of degrading to a daily-only verdict.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.fquant.daily_market_research import IntradayFloatSharesFact
from app.data_providers.fquant.escape_risk_intraday import (
    EscapeRiskIntradayBundle,
    IntradayDay,
    IntradayMinute,
)
from app.services.full_market_adapters import escape_risk as adapter_mod
from app.services.full_market_research import RunnerContext
from app.services.volume_breakout import DEFAULT_OOS_START

SHANGHAI = ZoneInfo("Asia/Shanghai")
COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "volume",
    "amount",
]
SIGNAL_DAY = date(2024, 3, 11)
FLOAT_SHARES = 100_000_000.0


def make_rows(symbols):
    rows = []
    start = date(2023, 1, 1)
    for symbol in symbols:
        for index in range(500):
            close = 10.0 + 0.02 * index
            rows.append(
                {
                    "symbol": symbol,
                    "date": start + timedelta(days=index),
                    "open": close,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "raw_open": close,
                    "raw_high": close + 0.1,
                    "raw_low": close - 0.1,
                    "raw_close": close,
                    "volume": 100.0 + index,
                    "amount": (100.0 + index) * close,
                }
            )
    return rows


class Canonical:
    """Same duck-type the production evaluator consumes via PinnedCanonicalDailyReader."""

    def __init__(self, rows, preload_error=None):
        self.rows = rows
        self.days = sorted({row["date"] for row in rows})
        self.preload_calls = []
        self.preload_error = preload_error

    @classmethod
    def factory_for(cls, canonical):
        class _Factory:
            @staticmethod
            def from_repository(repo):
                return canonical

        return _Factory

    def has_columns(self, *columns):
        return all(column in COLUMNS for column in columns)

    def generation(self):
        return "20240801T000000Z-test"

    def manifest_sha256(self):
        return "a" * 64

    def manifest(self):
        return {
            "source_generations": {"markets": "markets-v1"},
            "columns": COLUMNS,
        }

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def preload_panel(self, start, end, *, symbols):
        self.preload_calls.append((start, end, list(symbols)))
        if self.preload_error is not None:
            raise self.preload_error
        return len(symbols)

    def daily_bars(self, symbol, start, end):
        return pl.DataFrame(
            [row for row in self.rows if row["symbol"] == symbol and start <= row["date"] <= end]
        )


class CompositeReader:
    """Runner-owned pinned composite reader; the adapter must never close it."""

    def __init__(self):
        self.closed = False

    def generation(self):
        return "composite-gen"

    def manifest_sha256(self):
        return "a" * 64

    def source_provenance(self):
        return {
            "canonical": {
                "generation": "20240801T000000Z-test",
                "manifest_sha256": "a" * 64,
            }
        }

    def universe(self, start, end):
        return ["000001.SZ"]

    def close(self):
        self.closed = True


class Repo:
    def __init__(self, reader):
        self.n_shape_research_reader = reader
        self.closed = False

    def close(self):
        self.closed = True


class FakeIntradayReader:
    def __init__(self, symbols, days, *, available_at=True, load_error=None):
        self.symbols = tuple(symbols)
        self.days = tuple(days)
        self.available_at = available_at
        self.load_error = load_error
        self.load_calls = []
        self.closed = False

    def load(self, symbols):
        self.load_calls.append(list(symbols))
        if self.load_error is not None:
            raise self.load_error
        rows = {}
        stamp = (
            datetime.combine(self.days[0], time(0, 0), tzinfo=SHANGHAI)
            if self.available_at is True
            else None
        )
        for symbol in self.symbols:
            for day in self.days:
                rows[(symbol, day)] = make_intraday_day(symbol, day, stamp)
        return EscapeRiskIntradayBundle(rows=rows, unavailable={})

    def run_manifest(self):
        return {
            "provider": "fake.catalog_pinned",
            "days": [day.isoformat() for day in self.days],
        }

    def close(self):
        self.closed = True


def make_intraday_day(symbol, day, available_at):
    minutes = tuple(
        IntradayMinute(
            minute_index=index,
            timestamp=(datetime.combine(day, time(9, 31)) + timedelta(minutes=index)).replace(
                tzinfo=SHANGHAI
            ),
            close=10.0,
            high=10.1,
            low=9.9,
            volume_shares=100,
            amount=1000.0,
            cumulative_vwap=10.0,
        )
        for index in range(240)
    )
    return IntradayDay(
        symbol=symbol,
        trade_date=day,
        minutes=minutes,
        open_price=10.0,
        pre_close=9.98,
        published_limit_up=10.98,
        published_limit_down=8.98,
        turnover=(
            None
            if available_at is None
            else IntradayFloatSharesFact(
                float_shares=FLOAT_SHARES,
                available_at=available_at,
                source_day=day - timedelta(days=1),
            )
        ),
    )


def intraday_days_for(canonical, end):
    """The 6 catalog days production evaluates: signal day + 5 history days."""
    window = canonical.market_days(end - timedelta(days=30), end)
    return tuple(window[-6:])


def install_provider(monkeypatch, provider):
    monkeypatch.setattr(
        adapter_mod, "get_active_provider_name", lambda capability=None: "fquant_local"
    )
    monkeypatch.setattr(adapter_mod, "get_provider", lambda name="fquant_local": provider)


def make_context():
    composite = CompositeReader()
    repo = Repo(composite)
    return RunnerContext(repo=repo, reader=composite), repo, composite


def make_verdict(monkeypatch, symbols, *, available_at=True, load_error=None, preload_error=None):
    canonical = Canonical(make_rows(symbols), preload_error=preload_error)
    monkeypatch.setattr(
        adapter_mod, "PublishedCanonicalDailyReader", Canonical.factory_for(canonical)
    )
    days = intraday_days_for(canonical, SIGNAL_DAY)
    reader = FakeIntradayReader(symbols, days, available_at=available_at, load_error=load_error)
    captured = {}

    def opener(manifest, market_days):
        captured["manifest"] = manifest
        captured["days"] = list(market_days)
        return reader

    install_provider(monkeypatch, SimpleNamespace(open_escape_risk_intraday_reader=opener))
    adapter = adapter_mod.EscapeRiskAdapter()
    context, repo, composite = make_context()
    request = adapter.build_request(
        SIGNAL_DAY, SIGNAL_DAY, list(symbols), oos_start=None, cost_bps=None
    )
    verdict = adapter.evaluate(context, request)
    return {
        "verdict": verdict,
        "reader": reader,
        "captured": captured,
        "canonical": canonical,
        "repo": repo,
        "composite": composite,
        "adapter": adapter,
    }


def s10_entry(verdict):
    return next(signal for signal in verdict["report"]["signals"] if signal["signal_id"] == "s10")


def test_build_request_embeds_full_cohort_with_defaults():
    adapter = adapter_mod.EscapeRiskAdapter()
    cohort = ["000001.SZ", "600000.SH", "300750.SZ"]
    request = adapter.build_request(SIGNAL_DAY, SIGNAL_DAY, cohort, oos_start=None, cost_bps=None)
    assert request.symbols == cohort
    assert request.oos_start == DEFAULT_OOS_START
    assert request.cost_bps == 10.0
    explicit = adapter.build_request(
        SIGNAL_DAY,
        SIGNAL_DAY,
        cohort,
        oos_start=date(2024, 1, 1),
        cost_bps=25.0,
    )
    assert explicit.oos_start == date(2024, 1, 1)
    assert explicit.cost_bps == 25.0


def test_full_cohort_evaluated_in_single_pass(monkeypatch):
    cohort = ["000001.SZ", "600000.SH"]
    outcome = make_verdict(monkeypatch, cohort, available_at=True)
    verdict, reader, captured = (
        outcome["verdict"],
        outcome["reader"],
        outcome["captured"],
    )
    assert verdict["status"] == "ok", verdict
    # Exactly one evaluator hand-off carrying the COMPLETE cohort.
    assert reader.load_calls == [list(cohort)]
    assert verdict["request"]["symbols"] == list(cohort)
    # Catalog reader pinned to the canonical manifest, days covering the signal
    # window plus the 5 history days, never beyond the requested end.
    assert captured["manifest"] == {
        "source_generations": {"markets": "markets-v1"},
        "columns": COLUMNS,
    }
    assert len(captured["days"]) >= 6
    assert captured["days"][-1] == SIGNAL_DAY
    assert reader.closed is True


def test_adapter_closes_intraday_reader_and_never_closes_runner_readers(monkeypatch):
    outcome = make_verdict(monkeypatch, ["000001.SZ"], available_at=True)
    assert outcome["verdict"]["status"] == "ok"
    assert outcome["reader"].closed is True
    assert outcome["composite"].closed is False
    assert outcome["repo"].closed is False


def test_s10_early_ltgb_unavailable_at_censored(monkeypatch):
    # available_at provenance missing -> S10 strictly censored, no detection.
    outcome = make_verdict(monkeypatch, ["000001.SZ"], available_at=False)
    verdict = outcome["verdict"]
    assert verdict["status"] == "ok", verdict
    entry = s10_entry(verdict)
    assert "censor_pit_fact_missing" in entry["censor_codes"]
    assert entry["verdict"] == "unavailable_no_qualified_events"
    # Contrast: provenance present -> the strict PIT censor stays off.
    valid = make_verdict(monkeypatch, ["000001.SZ"], available_at=True)
    assert "censor_pit_fact_missing" not in s10_entry(valid["verdict"])["censor_codes"]


def test_missing_intraday_reader_fails_closed(monkeypatch):
    canonical = Canonical(make_rows(["000001.SZ"]))
    monkeypatch.setattr(
        adapter_mod, "PublishedCanonicalDailyReader", Canonical.factory_for(canonical)
    )
    adapter = adapter_mod.EscapeRiskAdapter()
    context, _, _ = make_context()
    request = adapter.build_request(
        SIGNAL_DAY, SIGNAL_DAY, ["000001.SZ"], oos_start=None, cost_bps=None
    )
    # Provider without the intraday opener.
    install_provider(monkeypatch, SimpleNamespace())
    verdict = adapter.evaluate(context, request)
    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["unavailable_intraday_reader"]
    # Opener probes unavailable -> explicit None.
    install_provider(
        monkeypatch,
        SimpleNamespace(open_escape_risk_intraday_reader=lambda manifest, days: None),
    )
    verdict = adapter.evaluate(context, request)
    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["unavailable_intraday_reader"]


def test_missing_canonical_reader_fails_closed(monkeypatch):
    monkeypatch.setattr(
        adapter_mod,
        "PublishedCanonicalDailyReader",
        Canonical.factory_for(None),
    )
    install_provider(monkeypatch, SimpleNamespace())
    adapter = adapter_mod.EscapeRiskAdapter()
    context, _, _ = make_context()
    request = adapter.build_request(
        SIGNAL_DAY, SIGNAL_DAY, ["000001.SZ"], oos_start=None, cost_bps=None
    )
    verdict = adapter.evaluate(context, request)
    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["unavailable_canonical_reader"]


def test_intraday_load_failure_fails_closed_and_still_closes(monkeypatch):
    outcome = make_verdict(
        monkeypatch,
        ["000001.SZ"],
        available_at=True,
        load_error=RuntimeError("catalog reader exploded"),
    )
    verdict, reader = outcome["verdict"], outcome["reader"]
    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == [
        "unavailable_intraday_reader:unavailable_reader:catalog reader exploded"
    ]
    assert reader.closed is True
    assert outcome["composite"].closed is False


def test_preload_panel_receives_full_cohort_once(monkeypatch):
    cohort = ["000001.SZ", "600000.SH"]
    outcome = make_verdict(monkeypatch, cohort, available_at=True)
    calls = outcome["canonical"].preload_calls
    assert calls == [
        (
            SIGNAL_DAY - timedelta(days=400),
            SIGNAL_DAY + timedelta(days=120),
            cohort,
        )
    ]
    assert outcome["reader"].load_calls == [cohort]


def test_preload_failure_fails_closed_without_intraday_fallback(monkeypatch):
    outcome = make_verdict(
        monkeypatch,
        ["000001.SZ"],
        preload_error=RuntimeError("preload exploded"),
    )
    verdict = outcome["verdict"]
    assert verdict["status"] == "unavailable"
    assert verdict["unavailable_reasons"] == ["unavailable_preload_panel_failed"]
    assert verdict["preload_error"] == "preload exploded"
    assert outcome["reader"].load_calls == []
    assert outcome["reader"].closed is False
