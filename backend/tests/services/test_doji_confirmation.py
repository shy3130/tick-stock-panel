from datetime import date, timedelta

from app.services.doji_patterns.confirmation import ConfirmationDetector
from app.services.hold_firm_patterns.models import Bar, CensorReason, MarketFactsRow


class F:
    def __init__(self, b):
        self.r = {
            (x.symbol, x.date): MarketFactsRow(
                x.symbol,
                x.date,
                x.quote_open_raw,
                x.quote_high_raw,
                x.quote_low_raw,
                x.quote_close_raw,
                10,
                20,
                1,
                "n",
                False,
                "x",
            )
            for x in b
        }

    def row(self, s, d):
        return self.r.get((s, d))


def b(d, o, c, h, low):
    return Bar("600000.SH", d, o, h, low, c, o, h, low, c, 100, 1000)


def test_confirmation_direction_and_missing_day_censor():
    ds = [date(2026, 1, 1) + timedelta(i) for i in range(4)]
    bs = [
        b(ds[0], 10, 10, 10.5, 9.5),
        b(ds[1], 10, 10.8, 11, 9.8),
        b(ds[2], 10, 10, 10.2, 9.8),
        b(ds[3], 10, 9.2, 10.1, 9),
    ]
    out = ConfirmationDetector().detect("600000.SH", bs, F(bs), ds)
    assert out[0].evidence.qualified and out[0].evidence.values["confirm_direction"] == "bullish"
    assert out[1].evidence.qualified and out[1].evidence.values["confirm_direction"] == "bearish"
    missing = ConfirmationDetector().detect("600000.SH", bs[:1], F(bs[:1]), ds)
    assert missing[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE
