from datetime import date

import polars as pl

from app.api.kline import _minute_rows


def test_minute_rows_repairs_null_datetime():
    df = pl.DataFrame(
        {
            "symbol": ["00700.HK", "00700.HK"],
            "datetime": [None, None],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [100.0, 200.0],
            "amount": [100.0, 400.0],
        }
    )

    rows = _minute_rows(df, date(2026, 7, 3))

    assert rows[0]["datetime"] == "2026-07-03 09:31:00"
    assert rows[1]["datetime"] == "2026-07-03 09:32:00"
