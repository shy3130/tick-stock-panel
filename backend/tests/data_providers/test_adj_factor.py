import pytest
import polars as pl

from app.data_providers.fquant.adj_factor import compute_ex_factor_from_xdxr
from app.indicators.pipeline import compute_enriched


def test_cash_dividend_factor_matches_pipeline_event_contract():
    results = compute_ex_factor_from_xdxr(
        [
            {
                "symbol": "600988.SH",
                "trade_date": "2026-05-27",
                "category": 1,
                "fenhong": 3.2,
                "fenshu": 0,
                "songzhuangu": 0,
                "peigu": 0,
            }
        ],
        {"2026-05-26": 38.17},
    )

    assert results == [
        {
            "symbol": "600988.SH",
            "trade_date": "2026-05-27",
            "ex_factor": pytest.approx(38.17 / (38.17 - 0.32), rel=1e-6),
        }
    ]

    raw = pl.DataFrame(
        [
            {
                "symbol": "600988.SH",
                "date": "2026-05-26",
                "open": 37.0,
                "high": 38.5,
                "low": 36.8,
                "close": 38.17,
                "volume": 1000,
            },
            {
                "symbol": "600988.SH",
                "date": "2026-05-27",
                "open": 35.7,
                "high": 36.0,
                "low": 35.2,
                "close": 35.74,
                "volume": 1200,
            },
        ]
    ).with_columns(pl.col("date").str.strptime(pl.Date))
    factors = pl.DataFrame(results).with_columns(pl.col("trade_date").str.strptime(pl.Date))

    enriched = compute_enriched(raw, factors=factors)
    rows = {
        row["date"].isoformat(): row
        for row in enriched.select(["date", "close", "raw_close"]).to_dicts()
    }

    assert rows["2026-05-26"]["raw_close"] == pytest.approx(38.17)
    assert rows["2026-05-26"]["close"] == pytest.approx(37.85, abs=0.01)
    assert rows["2026-05-26"]["close"] < 40
    assert rows["2026-05-27"]["close"] == pytest.approx(35.74)


def test_non_price_capital_change_event_does_not_emit_factor():
    results = compute_ex_factor_from_xdxr(
        [
            {
                "symbol": "600519.SH",
                "trade_date": "2026-05-28",
                "category": 5,
                "fenhong": 0,
                "fenshu": 0,
                "songzhuangu": 0,
                "peigu": 0,
            }
        ],
        {"2026-05-27": 1443.0},
    )

    assert results == []
