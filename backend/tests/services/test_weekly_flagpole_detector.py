from datetime import date, timedelta

from app.services.swing_zigzag import KIND_HIGH, KIND_LOW, ZigzagPivot
from app.services.weekly_flagpole import detector
from app.services.weekly_flagpole.weekly import WeeklyBar


def wb(i, o, c, low=None, high=None, v=100, complete=True):
    d = date(2026, 1, 2) + timedelta(days=7 * i)
    return WeeklyBar("x", d, d, d, o, high or max(o, c), low or min(o, c), c, v, 5, complete)


def test_incomplete_week_never_confirms():
    bars = [
        wb(0, 10, 11),
        wb(1, 11, 12),
        wb(2, 12, 11, 10, 12),
        wb(3, 11, 13, 10.8, 13, complete=False),
    ]
    events, _, _ = detector.detect_symbol_events(
        symbol="x",
        weekly_bars=bars,
        rows=[],
        calendar=[],
        regime_facts={},
        event_start=date(2020, 1, 1),
        event_end=date(2030, 1, 1),
    )
    assert all(e["confirm_week_key"] != bars[-1].week_key for e in events)


def test_no_data_is_safe():
    assert (
        detector.detect_symbol_events(
            symbol="x",
            weekly_bars=[],
            rows=[],
            calendar=[],
            regime_facts={},
            event_start=date(2020, 1, 1),
            event_end=date(2030, 1, 1),
        )[0]
        == []
    )


def test_f3_links_new_pole_within_thirteen_weeks(monkeypatch):
    bars = [
        wb(0, 10, 11, 9, 11),
        wb(1, 11, 12, 10, 12),
        wb(2, 12, 11, 10, 12),
        wb(3, 11, 9, 8, 11),
        wb(4, 9, 10, 8.5, 10),
        wb(5, 10, 11, 9.5, 11),
    ]
    monkeypatch.setattr(
        detector,
        "confirmed_zigzag",
        lambda *args: [ZigzagPivot(0, 9, KIND_LOW, 1), ZigzagPivot(1, 12, KIND_HIGH, 2)],
    )
    _, _, diag = detector.detect_symbol_events(
        symbol="x",
        weekly_bars=bars,
        rows=[],
        calendar=[],
        regime_facts={},
        event_start=date(2020, 1, 1),
        event_end=date(2030, 1, 1),
    )
    assert (
        diag["failures"] >= 1 and diag["re_established"] >= 1 and diag["re_establishment_rate"] > 0
    )
