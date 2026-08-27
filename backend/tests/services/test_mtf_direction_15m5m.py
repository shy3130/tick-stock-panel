from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
import math
import pytest
from pydantic import ValidationError
from app.services.mtf_direction_15m5m import MTFDirectionEvaluateIn, MinuteBar, SessionSpec, TradingTermForbidden, evaluate_mtf_direction, validate_result_key


def params():
    return MTFDirectionEvaluateIn(start=date(2026, 1, 1), end=date(2026, 1, 5), oos_start=date(2026, 1, 3), symbols=["600000.SH", "600000.SH"])


def test_missing_reader_is_unavailable():
    result = evaluate_mtf_direction(params(), reader=None)
    assert result["status"] == "unavailable"
    assert result["reason"] == "minute_reader_unavailable"
    assert result["symbols"] == ["600000.SH"]


def test_extra_request_fields_and_required_split_are_rejected():
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(start=date(2026, 1, 1), end=date(2026, 1, 2), symbols=["600000.SH"], action="buy")
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(start=date(2026, 1, 1), end=date(2026, 1, 2), symbols=["600000.SH"])
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(start=date(2026, 1, 1), end=date(2026, 1, 2), oos_start=date(2026, 1, 1), symbols=["600000.SH"])


def test_invalid_window_and_symbol_are_rejected():
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(start=date(2026, 1, 2), end=date(2026, 1, 1), oos_start=date(2026, 1, 1), symbols=["600000.SH"])
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(start=date(2026, 1, 1), end=date(2026, 1, 2), oos_start=date(2026, 1, 2), symbols=["000001"])


def test_result_key_trading_terms_are_forbidden():
    with pytest.raises(TradingTermForbidden):
        validate_result_key("stop_signal")


class _Reader:
    def __init__(self):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(5)]

    def catalog_manifest(self):
        return {"generation": "minute-generation", "schema": "ordered-1m-v1"}

    def manifest_sha256(self):
        return "a" * 64

    def generation(self):
        return "minute-generation"

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def session(self, symbol, day):
        return SessionSpec(symbol, day, time(9, 30), time(15, 0))

    def minute_bars(self, symbol, day):
        stamps = []
        current = datetime.combine(day, time(9, 31))
        while current.time() <= time(11, 30):
            stamps.append(current)
            current += timedelta(minutes=1)
        current = datetime.combine(day, time(13, 1))
        while current.time() <= time(15, 0):
            stamps.append(current)
            current += timedelta(minutes=1)
        bars = []
        previous = 10.0
        for index, stamp in enumerate(stamps):
            close = 10.0 + index * 0.002 + math.sin(index / 18) * 0.35
            bars.append(MinuteBar(symbol, stamp, previous, max(previous, close) + 0.03, min(previous, close) - 0.03, close, 100.0 + index))
            previous = close
        return bars

    def sealed_cutoff(self):
        return datetime(2026, 1, 5, 15, 0)

    def close(self):
        pass


def test_complete_true_ohlcv_reader_runs_research_layers():
    result = evaluate_mtf_direction(params(), reader=_Reader())
    assert result["status"] == "ok"
    assert result["direction_labelling_pending"] is False
    assert result["provenance"]["generation"] == "minute-generation"
    assert result["provenance"]["manifest_sha256"] == "a" * 64
    assert result["research"]["purge_report"]["1"]["effective"] >= 0
    assert "verdict" in result["research"]

def test_provider_bar_and_session_protocols_are_structural():
    class ForeignReader(_Reader):
        def session(self, symbol, day):
            return SimpleNamespace(
                symbol=symbol,
                day=day,
                open_time=time(9, 30),
                close_time=time(15, 0),
            )

        def minute_bars(self, symbol, day):
            return [
                SimpleNamespace(
                    symbol=bar.symbol,
                    ts=bar.ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
                for bar in super().minute_bars(symbol, day)
            ]

    result = evaluate_mtf_direction(params(), reader=ForeignReader())
    assert result["status"] == "ok"

def test_sparse_true_trade_minutes_still_require_all_48_five_minute_windows():
    class SparseReader(_Reader):
        def minute_bars(self, symbol, day):
            return [
                bar
                for bar in super().minute_bars(symbol, day)
                if bar.ts.time() not in {time(14, 58), time(14, 59)}
            ]

    result = evaluate_mtf_direction(params(), reader=SparseReader())
    assert result["status"] == "ok"
    integrity = next(item for item in result["evidence"] if item["key"] == "ohlcv_integrity")
    assert integrity["detail"]["sparse_true_trade"] is True

    class MissingWindowReader(_Reader):
        def minute_bars(self, symbol, day):
            return [
                bar
                for bar in super().minute_bars(symbol, day)
                if not time(10, 1) <= bar.ts.time() <= time(10, 5)
            ]

    unavailable = evaluate_mtf_direction(params(), reader=MissingWindowReader())
    assert unavailable["status"] == "unavailable"
    assert unavailable["reason"] == "source_integrity_violation"

def test_sparse_close_bounds_and_canonical_membership_are_fail_closed():
    class ShortReader(_Reader):
        def minute_bars(self, symbol, day):
            return super().minute_bars(symbol, day)[:-1]
    class EarlyReader(_Reader):
        def minute_bars(self, symbol, day):
            bars = super().minute_bars(symbol, day)
            first = bars[0]
            return [MinuteBar(first.symbol, first.ts - timedelta(minutes=1), first.open, first.high, first.low, first.close, first.volume), *bars[1:]]
    for candidate in (ShortReader(), EarlyReader()):
        result = evaluate_mtf_direction(params(), reader=candidate)
        assert result["status"] == "unavailable"
        assert result["reason"] == "source_integrity_violation"


def test_purge_report_and_common_baseline_contract_are_present():
    result = evaluate_mtf_direction(params(), reader=_Reader())
    assert result["status"] == "ok"
    for horizon in ("1", "2"):
        report = result["research"]["purge_report"][horizon]
        assert report["raw"] == report["cross_boundary"] + report["overlap"] + report["effective"]
        assert result["research"]["common_set"][horizon]["size"] <= report["effective_oos"]
        for method in ("factor", "unconditional", "momentum_5", "sma5"):
            assert "wilson_lower" in result["research"]["methods"][method][horizon]
