from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.hold_firm_patterns.first_yin import FirstYinDetector
from app.services.hold_firm_patterns.models import (
    FACTOR_IDS,
    Bar,
    CensorReason,
    LandmarkKind,
    MarketFactsRow,
)

SYMBOL = "600000.SH"
BASE = date(2024, 1, 2)


def day(offset: int) -> date:
    return BASE + timedelta(days=offset)


def make_bar(
    d: date,
    *,
    raw_open: float,
    raw_close: float,
    adj_open: float,
    adj_close: float,
    volume: float,
    symbol: str = SYMBOL,
) -> Bar:
    return Bar(
        symbol=symbol,
        date=d,
        research_open_adj=adj_open,
        research_high_adj=max(adj_open, adj_close),
        research_low_adj=min(adj_open, adj_close),
        research_close_adj=adj_close,
        quote_open_raw=raw_open,
        quote_high_raw=max(raw_open, raw_close),
        quote_low_raw=min(raw_open, raw_close),
        quote_close_raw=raw_close,
        volume=volume,
        amount=volume * raw_close,
    )


class FakeFacts:
    def __init__(self, rows: list[MarketFactsRow]):
        self.rows = {(row.symbol, row.date): row for row in rows}
        self.identity_calls = 0

    def identity(self):
        self.identity_calls += 1
        raise AssertionError("detector must not read reader identity")

    def row(self, symbol: str, d: date):
        return self.rows.get((symbol, d))


def build(
    specs: list[tuple[str, dict[str, object]]],
    *,
    ratio: float = 0.10,
    is_st: bool = False,
    facts_missing: set[int] | None = None,
) -> tuple[list[Bar], FakeFacts, list[date]]:
    upper = round(10.0 * (1.0 + ratio), 2)
    lower = round(10.0 * (1.0 - ratio), 2)
    missing = facts_missing or set()
    bars: list[Bar] = []
    rows: list[MarketFactsRow] = []
    calendar = [day(index) for index in range(len(specs))]
    for index, (kind, options) in enumerate(specs):
        d = day(index)
        if kind == "no_bar":
            continue
        if kind == "up":
            raw_open, raw_close = 10.0, upper
            adj_close, adj_open = 11.0, 10.5
            if options.get("one_price", False):
                raw_open = upper
            else:
                raw_open = float(options.get("raw_open", raw_open))
            raw_close = float(options.get("raw_close", raw_close))
        elif kind == "yang":
            raw_open, raw_close = upper, round(upper * 1.02, 2)
            adj_close, adj_open = 12.5, 12.0
        elif kind == "yin":
            raw_open, raw_close = round(upper * 1.02, 2), round(upper * 0.97, 2)
            adj_close, adj_open = 12.0, 12.4
        else:
            raw_open, raw_close = round(10.0 * 0.98, 2), 10.0
            adj_close, adj_open = 10.0, 9.8
        raw_open = float(options.get("raw_open", raw_open))
        raw_close = float(options.get("raw_close", raw_close))
        adj_open = float(options.get("adj_open", adj_open))
        adj_close = float(options.get("adj_close", adj_close))
        volume = float(options.get("volume", 1000.0))
        bar = make_bar(
            d,
            raw_open=raw_open,
            raw_close=raw_close,
            adj_open=adj_open,
            adj_close=adj_close,
            volume=volume,
        )
        bars.append(bar)
        if index not in missing and kind != "no_facts":
            rows.append(
                MarketFactsRow(
                    symbol=SYMBOL,
                    date=d,
                    quote_open_raw=bar.quote_open_raw,
                    quote_high_raw=bar.quote_high_raw,
                    quote_low_raw=bar.quote_low_raw,
                    quote_close_raw=bar.quote_close_raw,
                    pre_close=10.0,
                    published_limit_up=float(options.get("limit_up", upper)),
                    published_limit_down=lower,
                    regime="normal",
                    is_st=bool(options.get("is_st", is_st)),
                    name="测试股",
                )
            )
    return bars, FakeFacts(rows), calendar


def qualified_specs(
    *, yin_volume: float = 500.0, complement_volume: float = 1000.0, yin_close: float = 12.0
) -> list[tuple[str, dict[str, object]]]:
    return (
        [("plain", {"adj_close": 10.0}) for _ in range(4)]
        + [("up", {"adj_close": 11.0}), ("up", {"adj_close": 11.5}), ("up", {"adj_close": 12.0})]
        + [("yang", {"volume": 800.0})]
        + [("yin", {"volume": yin_volume, "adj_close": yin_close})]
        + [("plain", {"volume": complement_volume, "adj_open": 11.6, "adj_close": 12.0})]
    )


def detect(specs, **kwargs):
    bars, facts, calendar = build(specs, **kwargs)
    return FirstYinDetector().detect(SYMBOL, bars, facts, calendar), facts


def test_shrink_and_expand_complements_qualify():
    result, _ = detect(qualified_specs())
    evidence = result[0].evidence
    assert evidence and evidence.qualified
    assert evidence.values["volume_state"] == "shrink"
    assert evidence.values["yin_volume_ratio"] == pytest.approx(0.5)
    assert evidence.values["complement_volume_ratio"] == pytest.approx(2.0)
    assert result[0].landmark.kind is LandmarkKind.FIRST_YIN_NEXT_CLOSE
    assert result[0].landmark.anchor_date == day(8)
    assert result[0].landmark.landmark_date == day(9)

    result, _ = detect(qualified_specs(yin_volume=1600.0, complement_volume=1000.0))
    evidence = result[0].evidence
    assert evidence and evidence.qualified
    assert evidence.values["volume_state"] == "expand"
    assert evidence.values["complement_volume_ratio"] == pytest.approx(0.625)


def test_middle_volume_and_failed_complements_are_not_selected():
    result, _ = detect(qualified_specs(yin_volume=1000.0, complement_volume=2000.0))
    assert result[0].evidence and not result[0].evidence.qualified
    assert result[0].evidence.values["volume_state"] == "middle"
    assert not result[0].evidence.values["complement_pass"]

    result, _ = detect(qualified_specs(complement_volume=700.0))
    assert result[0].evidence and not result[0].evidence.qualified
    result, _ = detect(qualified_specs(yin_volume=1600.0, complement_volume=1200.0))
    assert result[0].evidence and not result[0].evidence.qualified


def test_ma5_is_recomputed_and_equality_is_inclusive():
    result, _ = detect(qualified_specs(yin_close=11.0))
    assert result[0].evidence and not result[0].evidence.values["ma5_held"]

    specs = qualified_specs(yin_close=10.0)
    for index in (4, 5, 6, 7, 8):
        kind, options = specs[index]
        options["adj_close"] = 10.0
        options["adj_open"] = 9.5 if kind != "yin" else 10.6
    result, _ = detect(specs)
    assert result[0].evidence and result[0].evidence.values["ma5_held"]


def test_first_yin_delay_and_adjusted_candle_direction():
    specs = qualified_specs()
    specs[7] = ("yin", {"volume": 500.0})
    specs[8] = ("yin", {"volume": 1000.0})
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].anchor_date == day(7)

    specs = qualified_specs()
    specs.extend(
        [
            ("yang", {"adj_open": 12.0, "adj_close": 12.5}),
            ("yin", {"volume": 500.0}),
            ("plain", {"volume": 1000.0, "adj_open": 11.6, "adj_close": 12.0}),
        ]
    )
    for index in range(7, 11):
        specs[index] = ("yang", {"adj_open": 12.0, "adj_close": 12.5})
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].anchor_date == day(11)

    specs = qualified_specs()
    specs.extend(
        [
            ("yang", {"adj_open": 12.5, "adj_close": 12.5}),
            ("yang", {"adj_open": 12.5, "adj_close": 12.5}),
        ]
    )
    for index in range(7, 12):
        specs[index] = ("yang", {"adj_open": 12.5, "adj_close": 12.5})
    specs.extend([("yin", {"volume": 500.0}), ("plain", {"volume": 1000.0})])
    result, _ = detect(specs)
    assert result == ()

    specs = qualified_specs()
    specs[7] = ("yang", {"raw_open": 11.5, "raw_close": 11.0, "adj_open": 12.0, "adj_close": 12.5})
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].anchor_date == day(8)


def test_short_or_nonconsecutive_limit_up_runs_do_not_form_events():
    specs = [("plain", {}) for _ in range(4)] + [
        ("up", {}),
        ("up", {}),
        ("yang", {}),
        ("yin", {}),
        ("plain", {}),
    ]
    result, _ = detect(specs)
    assert result == ()

    specs = (
        [("plain", {}) for _ in range(4)]
        + [("up", {}), ("up", {}), ("yang", {})]
        + [
            ("up", {}),
            ("up", {}),
            ("up", {}),
            ("yang", {}),
            ("yin", {"volume": 500.0}),
            ("plain", {"volume": 1000.0}),
        ]
    )
    result, _ = detect(specs)
    assert len(result) == 1
    assert result[0].evidence.values["limit_up_streak_dates"] == [
        day(7).isoformat(),
        day(8).isoformat(),
        day(9).isoformat(),
    ]


def test_missing_facts_bars_and_incomplete_landmark_are_censored():
    specs = qualified_specs()
    specs[7] = ("no_bar", {})
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE

    specs = qualified_specs()
    result, _ = detect(specs, facts_missing={7})
    assert len(result) == 1 and result[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE

    specs = [("up", {}) for _ in range(3)]
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE

    specs = [("plain", {}) for _ in range(4)] + [
        ("up", {}),
        ("up", {}),
        ("up", {}),
        ("yang", {}),
        ("yin", {"volume": 500.0}),
    ]
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE


def test_warmup_and_band_widths_st_and_tolerance():
    specs = [
        ("up", {}),
        ("up", {}),
        ("up", {}),
        ("yin", {"volume": 500.0}),
        ("plain", {"volume": 1000.0}),
    ]
    result, _ = detect(specs)
    assert len(result) == 1 and result[0].censor is CensorReason.WARMUP_INCOMPLETE

    for ratio, st, upper in (
        (0.10, False, 11.0),
        (0.20, False, 12.0),
        (0.30, False, 13.0),
        (0.05, True, 10.5),
    ):
        specs = (
            [("plain", {}) for _ in range(4)]
            + [
                ("up", {"one_price": True}),
                ("up", {"one_price": True}),
                ("up", {"one_price": True}),
            ]
            + [("yang", {}), ("yin", {"volume": 1000.0}), ("plain", {"volume": 1000.0})]
        )
        result, _ = detect(specs, ratio=ratio, is_st=st)
        assert len(result) == 1 and result[0].evidence
        values = result[0].evidence.values
        assert values["limit_up_streak_days"] == 3
        assert values["limit_up_days"][0]["published_limit_up"] == upper
        assert values["limit_up_days"][0]["one_price_at_upper"] is True
        assert values["limit_up_days"][0]["is_st"] is st

    for close, expected in ((10.996, 1), (10.994, 0), (11.01, 0)):
        specs = qualified_specs()
        for index in (4, 5, 6):
            specs[index] = ("up", {"raw_close": close})
        result, _ = detect(specs)
        assert len(result) == expected


def test_evidence_is_machine_readable_and_detector_is_pure():
    result, facts = detect(qualified_specs())
    assert isinstance(result, tuple)

    evidence = result[0].evidence
    assert evidence and isinstance(evidence.values["thresholds"]["yin_shrink_max"], float)
    values = evidence.values
    assert isinstance(values["limit_up_streak_days"], int)
    assert isinstance(values["yin_volume_ratio"], float)
    assert isinstance(values["complement_volume_ratio"], float)
    assert isinstance(values["ma5_adj"], float)
    assert isinstance(values["ma5_held"], bool)
    assert isinstance(values["limit_up_days"], list)
    assert all(isinstance(item["one_price_at_upper"], bool) for item in values["limit_up_days"])
    assert facts.identity_calls == 0

    detector = FirstYinDetector()
    assert detector.factor_id == FACTOR_IDS[0] == "first_yin_complement"
    bars, facts, calendar = build(qualified_specs())
    assert detector.detect(SYMBOL, bars, facts, calendar) == detector.detect(
        SYMBOL, bars, facts, calendar
    )


def test_completed_event_does_not_change_when_future_bars_are_appended():
    specs = qualified_specs()
    bars, facts, calendar = build(specs)
    detector = FirstYinDetector()
    before = detector.detect(SYMBOL, bars, facts, calendar)
    future_specs = specs + [("plain", {"volume": 1000.0}) for _ in range(4)]
    after_bars, after_facts, after_calendar = build(future_specs)
    after = detector.detect(SYMBOL, after_bars, after_facts, after_calendar)
    assert after == before
