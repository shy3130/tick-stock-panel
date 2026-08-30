from datetime import date, timedelta

from app.services.doji_patterns.gravestone import GravestoneDetector
from app.services.hold_firm_patterns.models import Bar, MarketFactsRow


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


def test_gravestone_requires_prior_gain_and_shape():
    ds = [date(2026, 1, 1) + timedelta(i) for i in range(22)]
    bs = [b(d, 10, 10, 10.2, 9.8) for d in ds[:20]] + [
        b(ds[20], 11, 11.05, 12.6, 10.95),
        b(ds[21], 10, 10.4, 10.5, 9.9),
    ]
    out = GravestoneDetector().detect("600000.SH", bs, F(bs), ds)
    item = next(x for x in out if x.anchor_date == ds[20])
    assert item.evidence.qualified
    assert not any(x.anchor_date == ds[21] for x in out)
