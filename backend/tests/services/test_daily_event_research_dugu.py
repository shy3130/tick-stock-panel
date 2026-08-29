from datetime import date, timedelta

from app.services.daily_event_research.dugu_trend import DuguTrendConfig, DuguTrendDetector
from app.services.hold_firm_patterns.models import Bar
from app.services.daily_event_research.models import CensorReason

SYMBOL = "000001.SZ"


def make_bars(count=260, dip_at=220, dip=0.35):
    start = date(2024, 1, 1)
    closes = [10.0 + 0.05 * i for i in range(count)]
    if dip_at < count - 1:
        closes[dip_at] -= dip
        closes[dip_at + 1] -= dip * 0.5
    bars = []
    for i, close in enumerate(closes):
        low = close - (dip if i == dip_at else 0.01)
        bars.append(
            Bar(
                SYMBOL,
                start + timedelta(days=i),
                close,
                close + 0.02,
                low,
                close,
                close,
                close + 0.02,
                low,
                close,
                100.0,
                1000.0,
            )
        )
    return tuple(bars)


def run(bars, **kwargs):
    detector = DuguTrendDetector(DuguTrendConfig(**kwargs))
    return detector.detect(SYMBOL, bars, tuple(bar.date for bar in bars))


def test_truncation_is_prefix_invariant():
    bars = make_bars()
    full = run(bars, require_m3=True)
    cut = bars[:240]
    truncated = run(cut, require_m3=True)
    assert tuple(item for item in full if item.signal_date <= cut[-1].date) == truncated


def test_ma200_warmup_is_explicitly_censored_when_trigger_exists():
    detections = run(make_bars(count=150, dip_at=120), require_m3=False)
    assert detections
    assert any(item.censor is CensorReason.WARMUP_INCOMPLETE for item in detections)


def test_t1_t2_t3_evidence_is_recorded():
    detections = run(make_bars())
    evidenced = [item for item in detections if item.evidence is not None]
    assert evidenced
    values = evidenced[-1].evidence.values
    assert values["t1"] is True
    assert values["t2"] is True
    assert values["t3"] is True


def test_m3_switch_uses_frozen_twenty_day_return_cap_without_lookahead():
    bars = make_bars()
    on = run(bars, require_m3=True)
    evidenced = [item for item in on if item.evidence is not None]
    assert evidenced
    for item in evidenced:
        values = item.evidence.values
        assert values["m3_required"] is True
        assert values["m3_max_return"] == 0.30
        assert values["m3_pass"] is (values["m3_return_20d"] <= 0.30)


def test_fixed_and_atr_bands_are_distinct_registered_modes():
    fixed = run(make_bars(), band_mode="fixed")
    atr = run(make_bars(), band_mode="atr")
    assert fixed or atr
    if fixed and atr:
        assert {item.evidence.values["band_mode"] for item in fixed if item.evidence} <= {"fixed"}
        assert {item.evidence.values["band_mode"] for item in atr if item.evidence} <= {"atr"}


def test_missing_calendar_day_is_not_filled():
    bars = make_bars()
    missing = bars[:219] + bars[220:]
    detections = run(missing)
    assert all(item.signal_date != bars[220].date for item in detections)
