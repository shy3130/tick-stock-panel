from datetime import date

import numpy as np
import polars as pl
import pytest
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.research.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)


def sparse_panel():
    return pl.DataFrame(
        [
            dict(
                symbol=symbol,
                date=date(2025, 1, day),
                open=10.0,
                high=10.0,
                low=10.0,
                close=10.0,
                volume=1000.0,
            )
            for symbol, day in (("A", 2), ("A", 6), ("B", 2), ("B", 3), ("B", 6))
        ]
    )


def test_sparse_index_and_delay_use_valid_bars_per_asset():
    market = build_market_data_matrix(sparse_panel())
    before = market.nbytes
    index = market.valid_bars
    assert market.valid_bars is index
    assert index.offsets.tolist() == [0, 2, 5]
    assert market.nbytes == before + index.nbytes
    signals = make_signal_matrix(
        market.shape, entry=np.array([[True, True], [False, False], [False, False]])
    )
    execution = build_market_matrix_from_signals(market, signals, entry_delay_bars=1)
    assert execution.entry.tolist() == [[False, False], [False, True], [True, False]]
    assert execution.entry_signal_time[2, 0] == 0
    assert execution.entry_signal_time[1, 1] == 0


def test_price_limit_rules_are_explicit_and_read_only():
    with pytest.raises(TiveloError) as error:
        build_market_data_matrix(sparse_panel(), field_columns={"price_limit_pct"})
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    calls = []

    def rules(target, trading_dates, symbols, names, *, valid):
        calls.append((trading_dates, symbols, names))
        target[:] = np.where(valid, 0.1, np.nan)
        return target

    market = build_market_data_matrix(
        sparse_panel(), field_columns={"price_limit_pct"}, price_limit_provider=rules
    )
    assert len(calls) == 1
    assert market.field("price_limit_pct")[0, 0] == pytest.approx(0.1)
    assert np.isnan(market.field("price_limit_pct")[1, 0])
    with pytest.raises(ValueError, match="read-only"):
        market.field("price_limit_pct")[0, 0] = 0.2


def test_signal_shape_mismatch_is_rejected_before_execution():
    market = build_market_data_matrix(sparse_panel())
    with pytest.raises(ValueError, match="shape"):
        build_market_matrix_from_signals(market, make_signal_matrix((1, 1)))
