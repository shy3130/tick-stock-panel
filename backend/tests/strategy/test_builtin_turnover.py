import polars as pl

from app.strategy.builtin import high_turnover_surge


def test_high_turnover_surge_uses_percent_point_turnover():
    df = pl.DataFrame({
        "turnover_rate": [4.9, 5.1],
        "change_pct": [0.04, 0.04],
    })

    out = df.filter(high_turnover_surge.filter(df, {}))

    assert out["turnover_rate"].to_list() == [5.1]
