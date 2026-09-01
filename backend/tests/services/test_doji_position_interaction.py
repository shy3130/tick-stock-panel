from datetime import date, timedelta

from app.services.doji_patterns.evaluation import _interaction_gate
from app.services.doji_patterns.position_interaction import DojiPositionDetector
from app.services.doji_patterns.statistics import BootstrapResult
from app.services.hold_firm_patterns.models import Bar, CensorReason, MarketFactsRow


class Facts:
    def __init__(self, rows):
        self.rows = {(x.symbol, x.date): x for x in rows}

    def row(self, s, d):
        return self.rows.get((s, d))

    def identity(self):
        raise AssertionError("detector must not read identity")


def mk(d, o, c, h, low):
    return Bar("600000.SH", d, o, h, low, c, o, h, low, c, 100, 1000)


def facts(days, bars):
    return Facts(
        [
            MarketFactsRow(
                "600000.SH",
                d,
                b.quote_open_raw,
                b.quote_high_raw,
                b.quote_low_raw,
                b.quote_close_raw,
                10,
                20,
                1,
                "normal",
                False,
                "x",
            )
            for d, b in zip(days, bars, strict=False)
        ]
    )


def test_position_four_cells_and_middle_exclusion():
    days = [date(2026, 1, 1) + timedelta(i) for i in range(24)]
    bars = [mk(d, 10, 10, 12, 8) for d in days[:19]]
    bars += [
        mk(days[19], 11.6, 11.6, 12.2, 11.2),
        mk(days[20], 11, 12, 12.2, 11),
        mk(days[21], 8.4, 8.4, 9, 8),
        mk(days[22], 8, 9, 9.2, 7.9),
        mk(days[23], 10, 10, 10.5, 9.5),
    ]
    out = DojiPositionDetector().detect("600000.SH", bars, facts(days, bars), days)
    by = {x.anchor_date: x for x in out}
    assert by[days[19]].evidence.qualified and by[days[19]].evidence.values["stratum"] == "high"
    assert not by[days[20]].evidence.qualified and by[days[20]].evidence.values["stratum"] == "high"
    assert by[days[21]].evidence.qualified and by[days[21]].evidence.values["stratum"] == "low"
    assert not by[days[22]].evidence.qualified and by[days[22]].evidence.values["stratum"] == "low"
    assert (
        not by[days[23]].evidence.qualified and by[days[23]].evidence.values["stratum"] == "middle"
    )


def test_position_window_short_is_censored():
    days = [date(2026, 1, 1) + timedelta(i) for i in range(3)]
    bars = [mk(d, 10, 10, 12, 8) for d in days]
    out = DojiPositionDetector().detect("600000.SH", bars, facts(days, bars), days)
    assert out and all(x.censor is CensorReason.SELECTION_WINDOW_INCOMPLETE for x in out)


def test_position_interaction_requires_oos_cell_and_bootstrap_gates():
    small = {"600000.SH": [0.01]}
    sufficient = {f"{index:06d}.SZ": [0.01, 0.02, 0.03] for index in range(10)}
    ready = BootstrapResult(0.0, -0.01, 0.01, 5000)
    failed_bootstrap = BootstrapResult(None, None, None, 0, 5000)

    assert not _interaction_gate((small, small, small, small), ready)
    assert _interaction_gate((sufficient, sufficient, sufficient, sufficient), ready)
    assert not _interaction_gate(
        (sufficient, sufficient, sufficient, sufficient),
        failed_bootstrap,
    )
