from datetime import date

import pytest
from pydantic import ValidationError

from app.services.mtf_direction_15m5m import (
    MTFDirectionEvaluateIn,
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
