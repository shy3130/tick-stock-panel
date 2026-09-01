from datetime import date

from app.services.weekly_flagpole.weekly import aggregate_weekly_bars


def row(day, o, h, low, c, v=1):
    return {"date": day, "raw_open": o, "raw_high": h, "raw_low": low, "raw_close": c, "volume": v}


def test_cross_year_aggregation_and_ohlcv():
    rows = [
        row(date(2025, 12, 29), 10, 11, 9, 10.5, 2),
        row(date(2025, 12, 31), 10.5, 12, 10, 11, 3),
        row(date(2026, 1, 2), 11, 13, 10.5, 12, 4),
    ]
    out = aggregate_weekly_bars(
        symbol="x", rows=rows, market_days=[r["date"] for r in rows], window_end=date(2026, 1, 2)
    )
    assert len(out) == 1 and out[0].week_key == date(2026, 1, 2)
    assert (
        out[0].open == 10
        and out[0].close == 12
        and out[0].high == 13
        and out[0].low == 9
        and out[0].volume == 9
    )


def test_suspension_partial_and_trailing_incomplete():
    days = [date(2026, 2, d) for d in (2, 3, 4, 5, 6, 9, 10, 11)]
    out = aggregate_weekly_bars(
        symbol="x",
        rows=[row(days[0], 1, 2, 0.9, 1.5), row(days[3], 1.5, 2.2, 1.4, 2)],
        market_days=days,
        window_end=date(2026, 2, 10),
    )
    assert out[0].bar_days == 2 and out[0].complete
    assert out[-1].complete is False
