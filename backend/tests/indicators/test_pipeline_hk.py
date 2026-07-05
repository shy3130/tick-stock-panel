"""HK symbols must not produce A-share price-limit signals."""
import polars as pl

from app.indicators.pipeline import compute_enriched

_LIMIT_COLS = [
    "signal_limit_up",
    "signal_limit_down",
    "consecutive_limit_ups",
    "consecutive_limit_downs",
    "signal_broken_limit_up",
]


def _raw(symbol: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol, symbol],
            "date": ["2026-06-29", "2026-06-30"],
            "open": [100.0, 100.0],
            "high": [100.0, 110.0],
            "low": [100.0, 100.0],
            "close": [100.0, 110.0],
            "volume": [1_000, 1_000],
            "amount": [100_000.0, 110_000.0],
        }
    )


def _instruments(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [symbol], "name": ["Sample"], "float_shares": [1_000_000.0]})


def test_hk_has_no_limit_signals():
    out = compute_enriched(_raw("00700.HK"), instruments=_instruments("00700.HK"), asset_type="hk")
    for col in _LIMIT_COLS:
        assert col not in out.columns, f"HK should not produce {col}"
    assert "ma5" in out.columns
    assert "macd_dif" in out.columns
    assert out["turnover_rate"].to_list() == [0.1, 0.1]


def test_a_share_still_computes_limit_signals():
    out = compute_enriched(_raw("600519.SH"), instruments=_instruments("600519.SH"), asset_type="stock")
    assert "signal_limit_up" in out.columns
    assert bool(out["signal_limit_up"].fill_null(False).to_list()[-1]) is True
