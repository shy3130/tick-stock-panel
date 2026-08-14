import polars as pl
import pytest

from app.indicators import advanced


def frame(n=80):
    return pl.DataFrame({
        "date": list(range(n)),
        "open": [10 + i * .1 for i in range(n)],
        "high": [10.5 + i * .1 for i in range(n)],
        "low": [9.5 + i * .1 for i in range(n)],
        "close": [10 + i * .1 for i in range(n)],
        "volume": [1000 + i * 3 for i in range(n)],
    })


@pytest.mark.parametrize("name,column", [
    ("supertrend", "supertrend"), ("kama", "kama"), ("cmf", "cmf"),
    ("aroon", "aroon_up"), ("cmo", "cmo"), ("force_index", "force_index"),
    ("dema", "dema"), ("tema", "tema"), ("hull_ma", "hull_ma"),
    ("choppiness_index", "choppiness_index"), ("elder_ray", "elder_bull_power"),
    ("chaikin_osc", "chaikin_osc"), ("mass_index", "mass_index"),
    ("ulcer_index", "ulcer_index"), ("coppock_curve", "coppock_curve"),
])
def test_advanced_indicator_adds_column(name, column):
    out = getattr(advanced, name)(frame())
    assert column in out.columns
    assert out.height == 80


def test_indicators_are_finite_after_warmup():
    df = frame()
    for fn in advanced.INDICATORS.values():
        out = fn(df)
        for c in set(out.columns) - set(df.columns):
            assert out.get_column(c).drop_nulls().is_finite().all()
