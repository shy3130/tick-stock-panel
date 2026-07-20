import polars as pl

from app.api.screener import _one_word_limit_expr


def test_one_word_limit_requires_main_status_and_equal_ohlc() -> None:
    rows = pl.DataFrame({
        "status": ["limit_up", "limit_up", "broken", "limit_up"],
        "open": [11.0, 10.5, 11.0, 0.0],
        "high": [11.0, 11.0, 11.0, 0.0],
        "low": [11.0, 10.5, 11.0, 0.0],
        "close": [11.0, 11.0, 11.0, 0.0],
    })

    result = rows.with_columns(
        _one_word_limit_expr("limit_up", rows.columns).alias("is_one_word")
    )

    assert result.get_column("is_one_word").to_list() == [True, False, False, False]


def test_one_word_limit_supports_limit_down() -> None:
    rows = pl.DataFrame({
        "status": ["limit_down", "recovery"],
        "open": [9.0, 9.0],
        "high": [9.0, 9.0],
        "low": [9.0, 9.0],
        "close": [9.0, 9.0],
    })

    result = rows.with_columns(
        _one_word_limit_expr("limit_down", rows.columns).alias("is_one_word")
    )

    assert result.get_column("is_one_word").to_list() == [True, False]
