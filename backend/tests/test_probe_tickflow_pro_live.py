"""Fake-client coverage for TickFlow Pro live probe SDK namespaces."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.probe_tickflow_pro import run_dry_run, run_live


class _FakeKlines:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def batch(self, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), **kwargs})
        return [{"symbol": s, "bars": [{"c": 1.0}]} for s in symbols]


class _FakeQuotes:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, *, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), **kwargs})
        return [{"symbol": s, "last": 1.0} for s in symbols]


class _FakeTickFlow:
    def __init__(self) -> None:
        self.klines = _FakeKlines()
        self.quotes = _FakeQuotes()


def test_dry_run_status_is_dry_run_ok(tmp_path: Path) -> None:
    report = run_dry_run(tmp_path)
    assert report["status"] == "DRY_RUN_OK"
    assert "READY_FOR_LIVE" not in report["status"]


def test_run_live_uses_klines_batch_and_quotes_get(tmp_path: Path) -> None:
    fake = _FakeTickFlow()
    report = run_live(tmp_path, client=fake, endpoint="https://api.tickflow.org")
    assert report["status"] == "LIVE_OK"
    assert report["errors"] == []
    assert "klines.batch.1d" in report["samples"]
    assert "quotes.get" in report["samples"]
    assert fake.klines.calls and fake.klines.calls[0]["period"] == "1d"
    assert fake.klines.calls[0]["count"] == 5
    assert fake.quotes.calls and set(fake.quotes.calls[0]["symbols"]) == {
        "000403.SZ",
        "600489.SH",
        "300059.SZ",
    }


def test_run_live_records_partial_on_sdk_errors(tmp_path: Path) -> None:
    class _Bad:
        def batch(self, *a, **k):
            raise AttributeError("nope")

        def get(self, *a, **k):
            raise AttributeError("nope")

    fake = SimpleNamespace(klines=_Bad(), quotes=_Bad())
    report = run_live(tmp_path, client=fake, endpoint="https://api.tickflow.org")
    assert report["status"] == "LIVE_PARTIAL"
    assert any(e.startswith("klines.batch.1d: AttributeError") for e in report["errors"])
    assert any(e.startswith("quotes.get: AttributeError") for e in report["errors"])
