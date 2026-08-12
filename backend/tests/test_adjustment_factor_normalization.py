from __future__ import annotations

from datetime import date

import polars as pl
import pytest


def test_cumulative_factors_become_event_ratios_without_compounding_twice():
    try:
        from app.data_providers.normalizer import cumulative_to_event_factors
    except ImportError:
        pytest.fail("cumulative_to_event_factors is not implemented")

    cumulative = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * 4,
            "trade_date": [
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 6, 10),
                date(2025, 6, 10),
            ],
            "adj_factor": [1.0, 1.0, 1.1, 1.21],
        }
    )

    result = cumulative_to_event_factors(cumulative)

    assert result["trade_date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 6, 10),
        date(2025, 6, 10),
    ]
    assert result["ex_factor"].to_list() == pytest.approx([1.0, 1.1, 1.1])
