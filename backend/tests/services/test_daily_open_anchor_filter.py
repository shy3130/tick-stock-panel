"""Issue #30 service contract tests — one case per coding-review finding."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

import app.data_providers.fquant.daily_market_research as dmr
import app.services.daily_open_anchor_filter as svc
from app.services.daily_open_anchor_filter import (
    FACTOR_ID,
    LedgerEntry,
    SymbolSeries,
    UnavailableError,
    _FactsView,
    _golden_dead_crosses,
    _segment_stats,
    _terminal_from_result,
    assess_daily_open_anchor_capability,
    evaluate_daily_open_anchor,
    rebuild_limit_bands,
    random_anchor_index,
)

SYM = "600000.SH"
PIN = "mkt-gen"
SHA = "b" * 64
SIGNAL_DAY = date(2026, 2, 6)  # days[23] below; golden cross occurs on signal day


def _days(count: int) -> list[date]:
    return [date(2026, 1, 14) + timedelta(days=i) for i in range(count)]


def _fake_canonical(days: list[date], rows: list[dict], *, manifest: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        generation=lambda: "canon-gen",
        manifest_sha256=lambda: "a" * 64,
        manifest=lambda: manifest
        if manifest is not None
        else {"source_generations": {"markets": {"generation": PIN, "manifest_sha256": SHA}}},
        market_days=lambda start, end: [d for d in days if start <= d <= end],
        daily_bars=lambda symbol, start, end: pl.DataFrame(
            [row for row in rows if start <= row["date"] <= end]
        ),
        columns=lambda: (
            "date",
            "open",
            "high",
            "low",
            "close",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
        ),
    )


def _rows(days: list[date], *, bullish_index: int | None = None, raw_open_none_index: int | None = None) -> list[dict]:
    rows = []
    for index, day in enumerate(days):
        close = 12.0 if index >= 23 else 10.0
        open_ = 9.5 if index == bullish_index else 10.0
        raw_open = None if index == raw_open_none_index else open_
        rows.append(
            {
                "symbol": SYM,
                "date": day,
                "open": open_,
                "high": max(open_, close) + 0.1,
                "low": min(open_, close) - 0.1,
                "close": close,
                "raw_open": raw_open,
                "raw_high": max(open_, close) + 0.1,
                "raw_low": min(open_, close) - 0.1,
                "raw_close": close,
            }
        )
    return rows


def _facts_rows(days: list[date], *, pre_close: float | None = 10.0) -> dict:
    return {
        (SYM, day): SimpleNamespace(
            pre_close=pre_close,
            published_limit_up=11.0,
            regime="main_10",
            is_st=False,
            name="",
            suspended=None,
            raw_open=None,
            raw_high=None,
            raw_low=None,
            raw_close=None,
        )
        for day in days
    }


def _patch_facts(monkeypatch: pytest.MonkeyPatch, rows: dict) -> None:
    monkeypatch.setattr(svc, "_resolve_markets_pin", lambda canonical: (PIN, SHA))
    monkeypatch.setattr(svc, "_load_market_fact_rows", lambda canonical, symbols, days: rows)


def _trade(signal_day: date) -> SimpleNamespace:
    return SimpleNamespace(
        entry_signal_date=signal_day,
        exit_reason="signal",
        entry_price=12.0,
        exit_price=12.1,
        pnl_pct=0.008,
        mae_pct=-0.01,
        mfe_pct=0.02,
        blocked_exit_days=0,
    )


def _patch_engine(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    def fake_engine(panel, entries, exits, config):
        calls.append(panel)
        return SimpleNamespace(trades=[_trade(SIGNAL_DAY)], stats={"execution": {}})

    monkeypatch.setattr(svc, "_run_engine", fake_engine)


def test_definition_and_capability_fail_closed_without_reader():
    result = assess_daily_open_anchor_capability(None)
    assert result["factor_id"] == FACTOR_ID
    assert result["available"] is False
    assert "canonical_reader_missing" in result["reasons"]
    assert result["data_gates"]["intraday"].startswith("unavailable")


def test_limit_bands_use_half_up_and_regime_ratios():
    assert rebuild_limit_bands("main_10", 10.005) == (11.01, 9.0)
    assert rebuild_limit_bands("st_5", 10.0) == (10.5, 9.5)
    assert rebuild_limit_bands("chinext_20", 10.0) == (12.0, 8.0)
    assert rebuild_limit_bands(None, 10.0) == (None, None)


def test_random_anchor_seed_is_stable_and_uses_all_arms():
    first = random_anchor_index(SYM, SIGNAL_DAY, 20)
    second = random_anchor_index(SYM, SIGNAL_DAY, 20)
    assert first == second
    assert set(("none", "original", "inverted", "random")) == set(svc.ARMS)


def test_ma_crosses_require_warmup_and_have_expected_direction():
    closes = [10.0] * 20 + [11.0, 12.0, 13.0, 12.0, 10.0, 9.0]
    golden, dead = _golden_dead_crosses(closes)
    assert all(index >= 20 for index in golden | dead)


# --- F1: markets pre_close missing must fail closed, never fall back to canonical ---


def test_f1_band_raises_without_pre_close_even_when_canonical_has_prev_raw_close():
    days = _days(2)
    series = SymbolSeries(symbol=SYM)
    for index, day in enumerate(days):
        series.day_index[day] = index
        series.raw_close[day] = 10.0  # canonical prev close exists but must NOT be used
    view = _FactsView(
        {
            (SYM, days[1]): SimpleNamespace(
                pre_close=None, published_limit_up=11.0, regime="main_10"
            )
        }
    )
    with pytest.raises(UnavailableError) as excinfo:
        view.band(SYM, days[1])
    assert excinfo.value.reason == "limit_band_facts_incomplete"
    assert excinfo.value.detail["field"] == "pre_close"


def test_f1_band_rebuilds_from_markets_pre_close_and_cross_checks():
    days = _days(2)
    view = _FactsView(
        {
            (SYM, days[1]): SimpleNamespace(
                pre_close=10.0, published_limit_up=11.0, regime="main_10"
            )
        }
    )
    assert view.band(SYM, days[1]) == (11.0, 9.0)


# --- F2: blocked-exit days recorded from single-candidate execution counters ---


def test_f2_terminal_ledger_records_blocked_days_from_execution_counts():
    candidate = svc.Candidate(symbol=SYM, signal_date=SIGNAL_DAY, segment="oos", day_position=24)
    candidate.precheck = svc.PRECHECK_OK
    result = SimpleNamespace(
        trades=[],
        stats={"execution": {"sell_suspended": 3, "sell_limit_down": 2}},
    )
    entry = _terminal_from_result(result, candidate, "none", SIGNAL_DAY, 1)
    assert entry.terminal_status == "blocked"
    assert entry.terminal_reason == "sell_suspended"
    assert entry.blocked_exit_days == 5


# --- F3: every filtered arm owns a unique not_retained ledger row ---


def test_f3_filtered_arm_gets_ledger_row_and_virtual_outcome(monkeypatch):
    days = _days(41)
    canonical = _fake_canonical(days, _rows(days, bullish_index=22))
    _patch_facts(monkeypatch, _facts_rows([day for day in days[23:]]))
    calls: list = []
    _patch_engine(monkeypatch, calls)
    payload = evaluate_daily_open_anchor(canonical, SIGNAL_DAY, SIGNAL_DAY, SIGNAL_DAY, [SYM])
    assert payload["status"] == "ok"
    original_rows = [row for row in payload["execution_ledger"] if row["arm"] == "original"]
    assert len(original_rows) == 1
    assert original_rows[0]["terminal_status"] == "not_retained"
    assert original_rows[0]["terminal_reason"] == "arm_filtered"
    assert original_rows[0]["filter_retained"] is False
    stats = payload["arms"]["original"]["segments"]["oos"]["stats"]
    assert stats["n_filtered"] == 1
    event = payload["events"][0]
    assert event["arms"]["original"]["virtual_outcome"]["source"] == "none_arm"


# --- F4: loader open failures map to unavailable, not 503 ---


def test_f4_markets_open_failure_becomes_unavailable(monkeypatch):
    class Boom:
        @classmethod
        def from_canonical_manifest(cls, manifest):
            raise FileNotFoundError("markets generation dir missing")

    monkeypatch.setattr(dmr, "PublishedDailyMarketFactsReader", Boom)
    days = _days(41)
    canonical = _fake_canonical(days, _rows(days))
    with pytest.raises(UnavailableError) as excinfo:
        svc._resolve_markets_pin(canonical)
    assert excinfo.value.reason == "markets_generation_unopenable"
    capability = assess_daily_open_anchor_capability(canonical)
    assert capability["available"] is False
    assert "markets_generation_unopenable" in capability["reasons"]


# --- F5: capability validates and discloses the markets facts gate ---


def test_f5_capability_reports_missing_pin_as_unavailable():
    days = _days(41)
    canonical = _fake_canonical(days, _rows(days), manifest={})
    capability = assess_daily_open_anchor_capability(canonical)
    assert capability["available"] is False
    assert "markets_pin_missing" in capability["reasons"]
    assert capability["markets_facts"]["opened"] is False


def test_f5_capability_discloses_opened_pin(monkeypatch):
    class FakeReader:
        _column_names = {"code", "asset_type", "trade_date", "price", "ztj", "zrspj", "jrkpj", "zgj", "zdj", "zspj"}
        _has_payload_json = False
        _quote_columns = {"jrkpj": "jrkpj", "zgj": "zgj", "zdj": "zdj", "price": "price"}
        _direct_fields = {"price": True, "ztj": True}

        @classmethod
        def from_canonical_manifest(cls, manifest):
            return SimpleNamespace(
                generation=lambda: PIN,
                pin_manifest_sha256=lambda: SHA,
                pin_identity_verified=lambda: True,
                _column_names=cls._column_names,
                _has_payload_json=cls._has_payload_json,
                _quote_columns=cls._quote_columns,
                _direct_fields=cls._direct_fields,
                close=lambda: None,
            )

    monkeypatch.setattr(dmr, "PublishedDailyMarketFactsReader", FakeReader)
    days = _days(41)
    canonical = _fake_canonical(days, _rows(days))
    capability = assess_daily_open_anchor_capability(canonical)
    assert capability["available"] is True
    assert capability["markets_facts"]["opened"] is True
    assert capability["markets_facts"]["pin"] == PIN
    assert "pre_close" in capability["markets_facts"]["provides"]


# --- F6: n_retained counts filter decisions; executed stays separate ---


def test_f6_retained_counts_filter_decision_not_precheck_success():
    def entry(**overrides):
        base = dict(
            symbol=SYM,
            signal_date=SIGNAL_DAY,
            arm="none",
            segment="oos",
            planned_execution_date=None,
            filter_retained=True,
            precheck=svc.PRECHECK_OK,
            terminal_status="blocked",
            terminal_reason="buy_limit_up",
        )
        base.update(overrides)
        return LedgerEntry(**base)

    entries = [
        entry(),
        entry(arm="original", filter_retained=False, terminal_status="not_retained", terminal_reason="arm_filtered"),
        entry(
            arm="inverted",
            terminal_status="traded",
            terminal_reason=None,
            exit_reason="signal",
            pnl_pct=0.01,
            engine_entry_index=1,
        ),
    ]
    stats = _segment_stats(3, entries)
    assert stats["n_retained"] == 2  # includes retained-but-blocked
    assert stats["n_filtered"] == 1
    assert stats["n_candidates_executed"] == 1
    assert stats["blocked_counts"] == {"buy_limit_up": 1}


# --- F7: markets pin validated even without signals ---


def test_f7_no_signals_still_validates_markets_pin():
    days = _days(41)
    rows = _rows(days)
    for row in rows:
        row["close"] = 10.0
        row["raw_close"] = 10.0
    canonical = _fake_canonical(days, rows, manifest={})
    with pytest.raises(UnavailableError) as excinfo:
        evaluate_daily_open_anchor(canonical, days[30], days[30], days[30], [SYM])
    assert excinfo.value.reason == "markets_pin_missing"


def test_f7_no_signals_still_returns_markets_provenance(monkeypatch):
    days = _days(41)
    rows = _rows(days)
    for row in rows:
        row["close"] = 10.0
        row["raw_close"] = 10.0
    canonical = _fake_canonical(days, rows)
    _patch_facts(monkeypatch, {})
    payload = evaluate_daily_open_anchor(canonical, days[30], days[30], days[30], [SYM])
    assert payload["status"] == "ok"
    assert payload["provenance"]["markets_generation"] == PIN
    assert payload["provenance"]["markets_manifest_sha256"] == SHA


# --- F8: invalid raw_open at T+1 censors the candidate, not the symbol ---


def test_f8_invalid_raw_open_censors_candidate_keeps_signal_universe(monkeypatch):
    days = _days(40)
    canonical = _fake_canonical(days, _rows(days, raw_open_none_index=24))
    _patch_facts(monkeypatch, _facts_rows([day for day in days[24:]]))
    calls: list = []
    _patch_engine(monkeypatch, calls)
    payload = evaluate_daily_open_anchor(canonical, days[23], days[23], days[23], [SYM])
    assert payload["status"] == "ok"
    assert payload["censored"] == []
    event = payload["events"][0]
    assert event["precheck"] == "censored:invalid_open"
    assert payload["arms"]["none"]["segments"]["oos"]["stats"]["n_signals"] == 1
    none_rows = [row for row in payload["execution_ledger"] if row["arm"] == "none"]
    assert none_rows[0]["terminal_reason"] == "invalid_open"
    assert calls == []


# --- F9: anchor-unavailable events keep the none baseline arm executable ---


def test_f9_anchor_unavailable_keeps_none_arm_and_marks_three_arms(monkeypatch):
    days = _days(41)
    canonical = _fake_canonical(days, _rows(days))
    _patch_facts(monkeypatch, _facts_rows([day for day in days[23:]]))
    calls: list = []
    _patch_engine(monkeypatch, calls)
    payload = evaluate_daily_open_anchor(canonical, SIGNAL_DAY, SIGNAL_DAY, SIGNAL_DAY, [SYM])
    assert payload["status"] == "ok"
    assert len(calls) == 1  # only the none baseline reached the engine
    ledger = payload["execution_ledger"]
    none_rows = [row for row in ledger if row["arm"] == "none"]
    assert none_rows[0]["terminal_status"] == "traded"
    for arm in ("original", "inverted", "random"):
        rows = [row for row in ledger if row["arm"] == arm]
        assert rows[0]["terminal_status"] == "censored"
        assert rows[0]["terminal_reason"] == "anchor_unavailable"
        assert rows[0]["filter_retained"] is None
    event = payload["events"][0]
    assert event["anchor"] is None
    assert event["arms"]["none"]["terminal_status"] == "traded"
    assert payload["arms"]["original"]["segments"]["oos"]["stats"]["censored_counts"] == {
        "anchor_unavailable": 1
    }
