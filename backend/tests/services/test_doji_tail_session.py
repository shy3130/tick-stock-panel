from datetime import date, timedelta
from types import SimpleNamespace

from app.services.doji_patterns.tail_session import TailSessionDetector
from app.services.hold_firm_patterns.models import Bar, CensorReason

SYMBOL = "600000.SH"


def _bar(day, opening=10.0, close=10.1):
    return Bar(
        SYMBOL,
        day,
        opening,
        max(opening, close) + 0.2,
        min(opening, close) - 0.2,
        close,
        opening,
        max(opening, close) + 0.2,
        min(opening, close) - 0.2,
        close,
        100.0,
        1000.0,
    )


class _Facts:
    def __init__(self, bars):
        self.days = {(SYMBOL, bar.date) for bar in bars}

    def row(self, symbol, day):
        return object() if (symbol, day) in self.days else None


class _Bundle:
    def __init__(self, day, opening, closing, high, low, tail_volume=1.0, unavailable=None):
        minutes = []
        for index in range(240):
            price = opening if index < 209 else closing
            minutes.append(
                SimpleNamespace(
                    minute_index=index,
                    close=price,
                    high=price,
                    low=price,
                    volume_shares=tail_volume if index >= 210 else 10.0,
                )
            )
        minutes[209].close = opening
        for index in range(210, 240):
            minutes[index].high = high
            minutes[index].low = low
        self.rows = {} if unavailable else {(SYMBOL, day): SimpleNamespace(minutes=minutes)}
        self.unavailable = {(SYMBOL, day): "intraday_rows_missing"} if unavailable else {}


def test_tail_three_shapes_are_frozen():
    day = date(2026, 1, 2)
    next_day = day + timedelta(days=1)
    bars = [_bar(day), _bar(next_day)]
    facts = _Facts(bars)
    cases = (
        ("bare_yang", 10.0, 11.0, 11.0, 10.0, 1.0),
        ("bare_yin", 10.0, 9.0, 10.0, 9.0, 1.0),
        ("shrinking_doji", 10.0, 10.0, 10.2, 9.8, 0.01),
    )
    for expected, opening, closing, high, low, volume in cases:
        result = TailSessionDetector(
            source=_Bundle(day, opening, closing, high, low, volume)
        ).detect(SYMBOL, bars, facts, [day, next_day])[0]
        assert result.evidence is not None
        assert result.evidence.values["shape"] == expected


def test_missing_minutes_fail_closed():
    day = date(2026, 1, 2)
    next_day = day + timedelta(days=1)
    result = TailSessionDetector(source=_Bundle(day, 10, 10, 10, 10, unavailable=True)).detect(
        SYMBOL, [_bar(day), _bar(next_day)], _Facts([_bar(day), _bar(next_day)]), [day, next_day]
    )[0]
    assert result.censor is CensorReason.MINUTE_DATA_INCOMPLETE
    assert result.evidence is None


def test_missing_confirmation_day_is_censored():
    day = date(2026, 1, 2)
    bar = _bar(day)
    result = TailSessionDetector(source=_Bundle(day, 10, 11, 11, 10)).detect(
        SYMBOL, [bar], _Facts([bar]), [day, day + timedelta(days=1)]
    )[0]
    assert result.censor is CensorReason.SELECTION_WINDOW_INCOMPLETE
