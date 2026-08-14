"""K 线形态识别测试 —— 覆盖各形态构造、边界/NaN、少量样本、确定性、禁交易语义字段。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators.patterns import PATTERN_DIRECTIONS, detect_candlestick_patterns

FORBIDDEN_KEYS = {
    "target_price", "target", "stop_loss", "stop", "entry_price", "entry",
    "exit_price", "exit", "action", "position", "order", "buy", "sell",
    "hold", "recommendation", "advice", "qty", "quantity",
}
VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}


# ── 辅助 ────────────────────────────────────────────────────

def _frame(rows, *, with_date=True):
    """rows: list of (open, high, low, close); 构造带日期的 DataFrame。"""
    n = len(rows)
    data: dict = {}
    if with_date:
        data["date"] = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    data["open"] = [r[0] for r in rows]
    data["high"] = [r[1] for r in rows]
    data["low"] = [r[2] for r in rows]
    data["close"] = [r[3] for r in rows]
    return pl.DataFrame(data)


def _flat(n, price=10.0, spread=0.5):
    """n 根横盘 K 线(带小实体,不构成十字星/锤子等单根形态)。"""
    return [(price - 0.1, price + spread, price - spread, price + 0.1)
            for _ in range(n)]


def _patterns_at(hits, idx):
    return {h["pattern"] for h in hits if h["index"] == idx}


def assert_clean(hits):
    """断言命中输出结构合法且不含交易语义字段。"""
    for h in hits:
        assert set(h) == {"pattern", "index", "date", "direction",
                          "confidence", "evidence"}, h
        assert h["pattern"] in PATTERN_DIRECTIONS, h
        assert h["direction"] in VALID_DIRECTIONS, h
        assert 0.0 <= h["confidence"] <= 1.0, h
        assert isinstance(h["index"], int), h
        assert isinstance(h["evidence"], dict), h
        keys = set(h["evidence"].keys())
        assert not (keys & FORBIDDEN_KEYS), f"forbidden evidence keys: {keys & FORBIDDEN_KEYS}"
        assert h["evidence"].get("trend") in VALID_DIRECTIONS | {"none", "up", "down"}, h


# ================================================================
# 1. 单根形态
# ================================================================

def test_doji_detected():
    rows = _flat(3) + [(10.0, 10.5, 9.5, 10.01)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "doji" in _patterns_at(hits, 3)
    assert_clean(hits)


def test_hammer_detected():
    rows = _flat(3) + [(10.0, 10.15, 9.0, 10.1)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "hammer" in _patterns_at(hits, 3)
    h = next(x for x in hits if x["pattern"] == "hammer")
    assert h["direction"] == "bullish"
    assert_clean(hits)


def test_inverted_hammer_detected():
    rows = _flat(3) + [(10.0, 11.0, 9.95, 10.1)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "inverted_hammer" in _patterns_at(hits, 3)
    h = next(x for x in hits if x["pattern"] == "inverted_hammer")
    assert h["direction"] == "bullish"
    assert_clean(hits)


# ================================================================
# 2. 双根形态
# ================================================================

def test_engulfing_bullish_detected():
    rows = _flat(3) + [(11.0, 11.2, 9.8, 10.0), (9.5, 11.6, 9.4, 11.5)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "engulfing_bullish" in _patterns_at(hits, 4)
    assert_clean(hits)


def test_engulfing_bearish_detected():
    rows = _flat(3) + [(10.0, 11.2, 9.8, 11.0), (11.5, 11.6, 9.4, 9.5)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "engulfing_bearish" in _patterns_at(hits, 4)
    assert_clean(hits)


def test_harami_detected():
    rows = _flat(3) + [(12.0, 12.1, 9.9, 10.0), (11.0, 11.2, 10.3, 10.5)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "harami" in _patterns_at(hits, 4)
    h = next(x for x in hits if x["pattern"] == "harami")
    assert h["direction"] == "bullish"  # 前根阴
    assert_clean(hits)


def test_piercing_detected():
    rows = _flat(3) + [(12.0, 12.1, 10.0, 10.0), (9.5, 11.4, 9.4, 11.3)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "piercing" in _patterns_at(hits, 4)
    assert_clean(hits)


def test_dark_cloud_detected():
    rows = _flat(3) + [(10.0, 12.0, 9.9, 12.0), (12.6, 12.7, 10.6, 10.7)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "dark_cloud" in _patterns_at(hits, 4)
    assert_clean(hits)


def test_inside_bar_detected():
    rows = _flat(3) + [(9.0, 12.0, 9.0, 12.0), (10.5, 11.5, 10.0, 11.0)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "inside_bar" in _patterns_at(hits, 4)
    assert_clean(hits)


# ================================================================
# 3. 三根形态
# ================================================================

def test_morning_star_detected():
    rows = _flat(3) + [
        (12.0, 12.0, 10.0, 10.0),
        (9.8, 9.9, 9.6, 9.7),
        (9.6, 11.6, 9.5, 11.5),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "morning_star" in _patterns_at(hits, 5)
    assert_clean(hits)


def test_evening_star_detected():
    rows = _flat(3) + [
        (10.0, 12.0, 10.0, 12.0),
        (12.2, 12.4, 12.1, 12.3),
        (12.4, 12.5, 10.4, 10.5),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "evening_star" in _patterns_at(hits, 5)
    assert_clean(hits)


def test_three_white_soldiers_detected():
    rows = _flat(3) + [
        (10.0, 11.0, 10.0, 11.0),
        (10.5, 12.0, 10.5, 12.0),
        (11.5, 13.0, 11.5, 13.0),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "three_white_soldiers" in _patterns_at(hits, 5)
    assert_clean(hits)


def test_three_black_crows_detected():
    rows = _flat(3) + [
        (13.0, 13.0, 12.0, 12.0),
        (12.5, 12.5, 11.0, 11.0),
        (11.5, 11.5, 10.0, 10.0),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "three_black_crows" in _patterns_at(hits, 5)
    assert_clean(hits)


# ================================================================
# 4. 突破
# ================================================================

def test_breakout_bullish_detected():
    rows = _flat(20, price=10.0, spread=0.5) + [(10.0, 11.2, 9.9, 11.0)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "breakout" in _patterns_at(hits, 20)
    h = next(x for x in hits if x["pattern"] == "breakout")
    assert h["direction"] == "bullish"
    assert_clean(hits)


def test_breakout_bearish_detected():
    rows = _flat(20, price=10.0, spread=0.5) + [(10.0, 10.1, 8.8, 9.0)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert "breakout" in _patterns_at(hits, 20)
    h = next(x for x in hits if x["pattern"] == "breakout")
    assert h["direction"] == "bearish"
    assert_clean(hits)


# ================================================================
# 5. 边界 / 异常
# ================================================================

def test_empty_frame_returns_empty():
    assert detect_candlestick_patterns(pl.DataFrame({
        "open": [], "high": [], "low": [], "close": [],
    })) == []


def test_missing_ohlc_column_returns_empty():
    df = pl.DataFrame({"date": [date(2026, 1, 1)], "high": [10.0], "low": [9.0]})
    assert detect_candlestick_patterns(df) == []


def test_single_bar_no_multibar_but_may_doji():
    rows = [(10.0, 10.5, 9.5, 10.01)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert_clean(hits)
    multi = {"engulfing_bullish", "engulfing_bearish", "harami", "piercing",
             "dark_cloud", "morning_star", "evening_star",
             "three_white_soldiers", "three_black_crows", "inside_bar", "breakout"}
    assert not ({h["pattern"] for h in hits} & multi)


def test_nan_rows_are_skipped():
    rows = _flat(3) + [
        (11.0, 11.2, 9.8, 10.0),
        (float("nan"), 11.6, 9.4, 11.5),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert_clean(hits)
    for h in hits:
        assert h["index"] != 4


def test_inf_rows_are_skipped():
    rows = _flat(3) + [
        (11.0, 11.2, 9.8, 10.0),
        (9.5, float("inf"), 9.4, 11.5),
    ]
    hits = detect_candlestick_patterns(_frame(rows))
    assert_clean(hits)
    for h in hits:
        assert h["index"] != 4


def test_few_samples_no_spurious_patterns():
    # 2 根正常横盘,不构成任何形态
    rows = _flat(2)
    hits = detect_candlestick_patterns(_frame(rows))
    assert_clean(hits)
    assert hits == []


def test_lookback_limits_scan_window():
    # 十字星在第 0 根,lookback=1 时只看最后一根 → 前面的形态不命中
    rows = [(10.0, 10.5, 9.5, 10.01)] + _flat(5)
    hits = detect_candlestick_patterns(_frame(rows), lookback=1)
    assert "doji" not in {h["pattern"] for h in hits}


# ================================================================
# 6. 确定性
# ================================================================

def test_deterministic_same_input_same_output():
    rows = _flat(20) + [
        (10.0, 10.5, 9.0, 10.1),
        (11.0, 11.2, 9.8, 10.0),
        (9.5, 11.6, 9.4, 11.5),
    ]
    df = _frame(rows)
    a = detect_candlestick_patterns(df)
    b = detect_candlestick_patterns(df)
    assert a == b


def test_ordering_by_index_then_pattern():
    rows = _flat(3) + [(10.0, 10.5, 9.0, 10.1)] + _flat(3)
    hits = detect_candlestick_patterns(_frame(rows))
    keys = [(h["index"], h["pattern"]) for h in hits]
    assert keys == sorted(keys)


# ================================================================
# 7. 输出契约
# ================================================================

def test_date_field_extracted():
    rows = _flat(3) + [(10.0, 10.5, 9.5, 10.01)]
    hits = detect_candlestick_patterns(_frame(rows))
    d = next(x for x in hits if x["pattern"] == "doji")
    assert d["date"] == "2026-01-04"


def test_no_date_column_date_is_none():
    rows = _flat(3) + [(10.0, 10.5, 9.5, 10.01)]
    df = _frame(rows, with_date=False)
    hits = detect_candlestick_patterns(df)
    assert all(h["date"] is None for h in hits)


def test_evidence_contains_structural_fields():
    rows = _flat(3) + [(10.0, 10.15, 9.0, 10.1)]
    hits = detect_candlestick_patterns(_frame(rows))
    h = next(x for x in hits if x["pattern"] == "hammer")
    ev = h["evidence"]
    assert "lower_shadow_body_ratio" in ev
    assert "body" in ev
    assert ev["lower_shadow_body_ratio"] >= 2.0


def test_all_fourteen_patterns_detectable():
    """逐一构造并确认 14 类形态均可被识别。"""
    cases = {
        "doji": (3, [(10.0, 10.5, 9.5, 10.01)]),
        "hammer": (3, [(10.0, 10.15, 9.0, 10.1)]),
        "inverted_hammer": (3, [(10.0, 11.0, 9.95, 10.1)]),
        "engulfing_bullish": (3, [(11.0, 11.2, 9.8, 10.0), (9.5, 11.6, 9.4, 11.5)]),
        "engulfing_bearish": (3, [(10.0, 11.2, 9.8, 11.0), (11.5, 11.6, 9.4, 9.5)]),
        "harami": (3, [(12.0, 12.1, 9.9, 10.0), (11.0, 11.2, 10.3, 10.5)]),
        "piercing": (3, [(12.0, 12.1, 10.0, 10.0), (9.5, 11.4, 9.4, 11.3)]),
        "dark_cloud": (3, [(10.0, 12.0, 9.9, 12.0), (12.6, 12.7, 10.6, 10.7)]),
        "inside_bar": (3, [(9.0, 12.0, 9.0, 12.0), (10.5, 11.5, 10.0, 11.0)]),
        "morning_star": (3, [(12.0, 12.0, 10.0, 10.0), (9.8, 9.9, 9.6, 9.7), (9.6, 11.6, 9.5, 11.5)]),
        "evening_star": (3, [(10.0, 12.0, 10.0, 12.0), (12.2, 12.4, 12.1, 12.3), (12.4, 12.5, 10.4, 10.5)]),
        "three_white_soldiers": (3, [(10.0, 11.0, 10.0, 11.0), (10.5, 12.0, 10.5, 12.0), (11.5, 13.0, 11.5, 13.0)]),
        "three_black_crows": (3, [(13.0, 13.0, 12.0, 12.0), (12.5, 12.5, 11.0, 11.0), (11.5, 11.5, 10.0, 10.0)]),
    }
    for name, (pad, bars) in cases.items():
        hits = detect_candlestick_patterns(_frame(_flat(pad) + list(bars)))
        assert any(h["pattern"] == name for h in hits), f"{name} not detected"
    rows = _flat(20, price=10.0, spread=0.5) + [(10.0, 11.2, 9.9, 11.0)]
    hits = detect_candlestick_patterns(_frame(rows))
    assert any(h["pattern"] == "breakout" for h in hits), "breakout not detected"
