from datetime import date, datetime, time, timedelta

import pytest
from pydantic import ValidationError
import math

from app.services.mtf_direction_15m5m import (
    MTFDirectionEvaluateIn,
    MinuteBar,
    SessionSpec,
    TradingTermForbidden,
    evaluate_mtf_direction,
    validate_result_key,
)


def params():
    return MTFDirectionEvaluateIn(
        start=date(2026, 1, 1), end=date(2026, 1, 5), symbols=["600000.SH", "600000.SH"]
    )


def test_missing_reader_is_unavailable():
    result = evaluate_mtf_direction(params(), reader=None)
    assert result["status"] == "unavailable"
    assert result["reason"] == "minute_reader_unavailable"
    assert result["symbols"] == ["600000.SH"]


def test_extra_request_fields_are_rejected():
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(
            start=date(2026, 1, 1), end=date(2026, 1, 2), symbols=["600000.SH"], action="buy"
        )


def test_invalid_window_and_symbol_are_rejected():
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(
            start=date(2026, 1, 2), end=date(2026, 1, 1), symbols=["600000.SH"]
        )
    with pytest.raises(ValidationError):
        MTFDirectionEvaluateIn(
            start=date(2026, 1, 1), end=date(2026, 1, 2), symbols=["000001"]
        )


def test_result_key_trading_terms_are_forbidden():
    with pytest.raises(TradingTermForbidden):
        validate_result_key("stop_signal")



class _Reader:
    def __init__(self):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(5)]

    def catalog_manifest(self):
        return {"generation": "minute-generation", "schema": "true_ohlcv_v1"}

    def generation(self):
        return "minute-generation"

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def session(self, symbol, day):
        return SessionSpec(symbol, day, time(9, 30), time(14, 29))

    def minute_bars(self, symbol, day):
        start = datetime.combine(day, time(9, 30))
        bars = []
        previous = 10.0
        for index in range(300):
            close = 10.0 + index * 0.002 + math.sin(index / 18) * 0.35
            bars.append(MinuteBar(
                symbol=symbol,
                ts=start + timedelta(minutes=index),
                open=previous,
                high=max(previous, close) + 0.03,
                low=min(previous, close) - 0.03,
                close=close,
                volume=100.0 + index,
            ))
            previous = close
        return bars

    def sealed_cutoff(self):
        return datetime(2026, 1, 5, 23, 59)


def test_complete_true_ohlcv_reader_runs_direction_engine_and_oos_layers():
    result = evaluate_mtf_direction(params(), reader=_Reader())
    assert result["status"] == "ok"
    assert result["direction_labelling_pending"] is False
    assert result["provenance"]["generation"] == "minute-generation"
    signals = result["direction"]["signals"]
    assert signals
    assert {row["segment"] for row in signals} == {"is", "oos"}
    assert all(row["confirmed_at"].startswith(row["day"]) for row in signals)
    assert result["direction"]["summary"]["segments"]["oos"]["signals"] > 0