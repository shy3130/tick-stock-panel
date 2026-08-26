from datetime import date

import polars as pl
import pytest

from app.services.n_shape_golden_phoenix import (
    FORWARD_HORIZONS,
    LOW_POSITION_MAX,
    VOLUME_BREAKOUT_RATIO,
    evaluate_n_shape,
    limit_up_price,
)


def test_missing_generation_or_pit_is_unavailable():
    result = evaluate_n_shape(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        symbols=["600000.SH"],
        pinned_reader=None,
        pit_provider=None,
    )
    assert result["status"] == "unavailable"
    assert set(result["unavailable_reasons"]) == {
        "generation_pinned_reader_missing",
        "pit_regime_st_missing",
    }
    assert result["events"] == []


def test_frozen_parameter_contract():
    assert LOW_POSITION_MAX == 0.35
    assert VOLUME_BREAKOUT_RATIO == 1.5
    assert FORWARD_HORIZONS == (1, 5, 10, 20)
    assert limit_up_price(10.00, 0.10) == 11.00


def test_missing_raw_columns_are_censored():
    from app.services.n_shape_golden_phoenix import _bars_to_dicts

    rows, censor = _bars_to_dicts(
        pl.DataFrame([{"date": date(2026, 1, 1), "raw_close": 1.0}]),
        "600000.SH",
    )
    assert rows == []
    assert censor["code"] == "raw_field_missing"


def test_trading_semantics_in_evidence_are_rejected():
    from app.services.n_shape_golden_phoenix import assert_no_trading_tokens

    with pytest.raises(ValueError):
        assert_no_trading_tokens("target_price")
