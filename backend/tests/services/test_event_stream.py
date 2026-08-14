"""异动事件识别测试 —— 覆盖正常构造、边界/NaN、去重/确定性、事件证据字段与禁交易语义。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.services.event_stream import (
    EVENT_TYPES,
    FORBIDDEN_EVIDENCE_KEYS,
    detect_events,
)

VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
REQUIRED_KEYS = {"symbol", "event_type", "occurred_at", "index",
                 "direction", "magnitude", "evidence", "source"}


# ── 辅助 ────────────────────────────────────────────────────

def _frame(rows, *, with_date=True, with_volume=True):
    """rows: list of (open, high, low, close[, volume])。"""
    n = len(rows)
    data: dict = {}
    if with_date:
        data["date"] = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    data["open"] = [r[0] for r in rows]
    data["high"] = [r[1] for r in rows]
    data["low"] = [r[2] for r in rows]
    data["close"] = [r[3] for r in rows]
    if with_volume:
        data["volume"] = [r[4] if len(r) > 4 else 1000 for r in rows]
    return pl.DataFrame(data)
def _flat(n, price=10.0, vol=1000):
    """n 根横盘 K 线(open=close=price、小振幅、平量,不触发任何异动)。"""
    return [(price, price + 0.02, price - 0.02, price, vol)
            for _ in range(n)]



def _types_at(events, idx):
    return {e["event_type"] for e in events if e["index"] == idx}


def assert_clean(events, symbol="600000"):
    """断言事件输出结构合法且不含交易语义字段。"""
    for e in events:
        assert set(e) == REQUIRED_KEYS, e
        assert e["event_type"] in EVENT_TYPES, e
        assert e["symbol"] == symbol, e
        assert e["direction"] in VALID_DIRECTIONS, e
        assert isinstance(e["index"], int), e
        assert np.isfinite(e["magnitude"]) and e["magnitude"] >= 0.0, e
        assert isinstance(e["evidence"], dict), e
        assert e["source"] == "local:ohlcv:detect_events", e
        ek = set(e["evidence"].keys())
        assert not (ek & FORBIDDEN_EVIDENCE_KEYS), f"forbidden: {ek & FORBIDDEN_EVIDENCE_KEYS}"


# ================================================================
# 1. price_spike
# ================================================================

def test_price_spike_bullish_detected():
    rows = _flat(3) + [(10.0, 10.8, 9.95, 10.6, 1000)]   # +6%
    events = detect_events(_frame(rows), "600000")
    assert "price_spike" in _types_at(events, 3)
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "price_spike")
    assert e["direction"] == "bullish"
    assert e["evidence"]["change_pct"] == pytest.approx(6.0, abs=0.01)
    assert e["magnitude"] == pytest.approx(6.0, abs=0.01)
    assert_clean(events)


def test_price_spike_bearish_detected():
    rows = _flat(3) + [(10.0, 10.0, 9.3, 9.4, 1000)]     # −6%
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "price_spike")
    assert e["direction"] == "bearish"
    assert e["evidence"]["change_pct"] == pytest.approx(-6.0, abs=0.01)
    assert_clean(events)


def test_price_spike_below_threshold_skipped():
    rows = _flat(3) + [(10.0, 10.4, 9.96, 10.3, 1000)]   # +3% < 5%
    events = detect_events(_frame(rows), "600000")
    assert "price_spike" not in _types_at(events, 3)


# ================================================================
# 2. volume_surge
# ================================================================

def test_volume_surge_detected():
    # 前 20 日均量 1000,第 21 日放量到 3000 → 量比 3×
    rows = _flat(21, vol=1000)
    rows[-1] = (10.0, 10.02, 9.98, 10.01, 3000)
    events = detect_events(_frame(rows), "000001")
    assert "volume_surge" in _types_at(events, 20)
    e = next(ev for ev in events if ev["index"] == 20 and ev["event_type"] == "volume_surge")
    assert e["evidence"]["volume_ratio"] == pytest.approx(3.0, abs=0.01)
    assert e["magnitude"] == pytest.approx(3.0, abs=0.01)
    assert_clean(events, "000001")


def test_volume_surge_below_ratio_skipped():
    rows = _flat(21, vol=1000)
    rows[-1] = (10.0, 10.02, 9.98, 10.01, 1500)   # 1.5× < 2×
    events = detect_events(_frame(rows), "000001")
    assert "volume_surge" not in _types_at(events, 20)


def test_volume_surge_missing_column_no_events():
    rows = _flat(21, vol=1000)
    rows[-1] = (10.0, 10.02, 9.98, 10.01, 9999)
    events = detect_events(_frame(rows, with_volume=False), "000001")
    assert not any(e["event_type"] == "volume_surge" for e in events)


# ================================================================
# 3. gap
# ================================================================

def test_gap_up_detected():
    rows = _flat(3) + [(10.3, 10.4, 10.2, 10.35, 1000)]  # open 10.3 vs prev 10.0 → +3%
    events = detect_events(_frame(rows), "600000")
    assert "gap" in _types_at(events, 3)
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "gap")
    assert e["direction"] == "bullish"
    assert e["evidence"]["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert_clean(events)


def test_gap_down_detected():
    rows = _flat(3) + [(9.7, 9.8, 9.6, 9.75, 1000)]      # open 9.7 vs prev 10.0 → −3%
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "gap")
    assert e["direction"] == "bearish"


def test_gap_below_threshold_skipped():
    rows = _flat(3) + [(10.1, 10.15, 10.0, 10.05, 1000)]  # +0.9% < 2%
    events = detect_events(_frame(rows), "600000")
    assert "gap" not in _types_at(events, 3)


# ================================================================
# 4. limit_move
# ================================================================

def test_limit_up_detected():
    # prev_close = 10.0 → 涨停价 round(10.0 * 1.1, 2) = 11.0
    rows = _flat(3) + [(10.5, 11.0, 10.5, 11.0, 1000)]
    events = detect_events(_frame(rows), "600000")
    assert "limit_move" in _types_at(events, 3)
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "limit_move")
    assert e["direction"] == "bullish"
    assert e["evidence"]["side"] == "up"
    assert e["evidence"]["limit_price"] == 11.0
    assert_clean(events)


def test_limit_down_detected():
    # prev_close = 10.0 → 跌停价 round(10.0 * 0.9, 2) = 9.0
    rows = _flat(3) + [(9.5, 9.5, 9.0, 9.0, 1000)]
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "limit_move")
    assert e["direction"] == "bearish"
    assert e["evidence"]["side"] == "down"


def test_limit_move_custom_pct_st_stock():
    # ST 5% 限制: prev_close = 10.00 → 涨停 10.50
    rows = _flat(3, price=10.0) + [(10.2, 10.5, 10.2, 10.5, 1000)]
    events = detect_events(_frame(rows), "ST001", limit_pct=5.0)
    e = next(ev for ev in events if ev["index"] == 3 and ev["event_type"] == "limit_move")
    assert e["evidence"]["limit_pct"] == 5.0
    assert e["evidence"]["limit_price"] == 10.5


def test_limit_move_not_at_limit_skipped():
    # prev_close = 10.0, 涨停 11.0, close = 10.80 → 未触及
    rows = _flat(3) + [(10.5, 10.85, 10.4, 10.8, 1000)]
    events = detect_events(_frame(rows), "600000")
    assert "limit_move" not in _types_at(events, 3)


# ================================================================
# 5. 多事件共存
# ================================================================

def test_multiple_events_same_bar():
    # 同时触发:涨停(10%)+ price_spike + gap up
    # prev_close = 10.0, open 10.3(+3% gap), close 11.0(涨停, +10%)
    rows = _flat(3, price=10.0) + [(10.3, 11.0, 10.2, 11.0, 5000)]
    events = detect_events(_frame(rows), "600000")
    types = _types_at(events, 3)
    assert {"limit_move", "price_spike", "gap"} <= types
    assert_clean(events)


# ================================================================
# 6. 边界 / 异常
# ================================================================

def test_empty_frame_returns_empty():
    assert detect_events(pl.DataFrame({
        "open": [], "high": [], "low": [], "close": [],
    }), "600000") == []


def test_single_bar_returns_empty():
    # 所有事件都需要前收,单根无法判定
    rows = [(10.0, 11.0, 9.0, 10.8, 5000)]
    assert detect_events(_frame(rows), "600000") == []


def test_missing_ohlc_column_returns_empty():
    df = pl.DataFrame({"date": [date(2026, 1, 1)], "high": [10.0], "low": [9.0]})
    assert detect_events(df, "600000") == []


def test_nan_rows_skipped():
    rows = _flat(5)
    # 第 4 根:close 为 NaN → 当日无效,不产生事件
    rows[4] = (np.nan, 10.5, 9.5, 11.0, 1000)
    events = detect_events(_frame(rows), "600000")
    assert "price_spike" not in _types_at(events, 4)
    assert "gap" not in _types_at(events, 4)


def test_nan_prev_close_skipped():
    # 前一根 close 无效 → 即使当日有效也不判定(需要前收)
    rows = _flat(2)
    rows.append((np.nan, 10.0, 9.0, 10.0, 1000))   # idx 2 invalid
    rows.append((10.5, 11.0, 10.4, 10.9, 1000))     # idx 3: prev invalid
    events = detect_events(_frame(rows), "600000")
    assert "price_spike" not in _types_at(events, 3)


def test_inf_rows_skipped():
    rows = _flat(5)
    rows[4] = (10.0, float("inf"), 9.5, 11.0, 1000)
    events = detect_events(_frame(rows), "600000")
    assert "price_spike" not in _types_at(events, 4)


def test_zero_volume_avg_no_event():
    # 前 20 日成交量为 0 → 均量 0,不触发(除零保护)
    rows = [(10.0, 10.05, 9.95, 10.01, 0) for _ in range(20)]
    rows.append((10.0, 10.02, 9.98, 10.01, 5000))
    events = detect_events(_frame(rows), "600000")
    assert "volume_surge" not in _types_at(events, 20)


def test_lookback_limits_scan_window():
    # 异动在 idx 1(prev_close=10.0 → +6%),lookback=2 时只扫最后 2 根 → 不触发
    rows = _flat(1) + [(10.0, 10.8, 9.95, 10.6, 1000)] + _flat(5)
    events = detect_events(_frame(rows), "600000", lookback=2)
    assert "price_spike" not in _types_at(events, 1)
    # 全量扫描时应检测到
    assert "price_spike" in _types_at(detect_events(_frame(rows), "600000"), 1)


def test_threshold_boundary_exact_hit():
    # change_pct 恰好等于阈值 5.0% → 触发(>=)
    # prev_close 10.0 → close 10.5 = +5.0%
    rows = _flat(3, price=10.0) + [(10.0, 10.6, 9.95, 10.5, 1000)]
    events = detect_events(_frame(rows), "600000", price_spike_pct=5.0)
    assert "price_spike" in _types_at(events, 3)


def test_threshold_boundary_just_below():
    # change_pct 4.99% → 不触发
    rows = _flat(3, price=10.0) + [(10.0, 10.6, 9.95, 10.499, 1000)]
    events = detect_events(_frame(rows), "600000", price_spike_pct=5.0)
    assert "price_spike" not in _types_at(events, 3)


# ================================================================
# 7. 去重 / 确定性
# ================================================================

def test_deterministic_same_input_same_output():
    rows = _flat(20) + [(10.3, 11.0, 10.2, 11.0, 5000), (10.0, 10.4, 9.3, 9.4, 6000)]
    frame = _frame(rows)
    a = detect_events(frame, "600000")
    b = detect_events(frame, "600000")
    assert a == b


def test_no_duplicate_events():
    # 同一 index + event_type 最多出现一次
    rows = _flat(3, price=10.0) + [(10.3, 11.0, 10.2, 11.0, 5000)]
    events = detect_events(_frame(rows), "600000")
    seen = set()
    for e in events:
        key = (e["index"], e["event_type"])
        assert key not in seen, f"duplicate: {key}"
        seen.add(key)


def test_ordering_by_index_then_type():
    rows = _flat(3, price=10.0) + [(10.3, 11.0, 10.2, 11.0, 5000)]
    events = detect_events(_frame(rows), "600000")
    keys = [(e["index"], e["event_type"]) for e in events]
    assert keys == sorted(keys)


# ================================================================
# 8. 输出契约 / 证据字段
# ================================================================

def test_date_field_extracted():
    rows = _flat(3) + [(10.0, 10.8, 9.95, 10.6, 1000)]
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["event_type"] == "price_spike")
    assert e["occurred_at"] == "2026-01-04"


def test_no_date_column_occurred_at_none():
    rows = _flat(3) + [(10.0, 10.8, 9.95, 10.6, 1000)]
    events = detect_events(_frame(rows, with_date=False), "600000")
    assert all(e["occurred_at"] is None for e in events)


def test_price_spike_evidence_fields():
    rows = _flat(3) + [(10.0, 10.8, 9.95, 10.6, 1000)]
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["event_type"] == "price_spike")
    ev = e["evidence"]
    assert "change_pct" in ev
    assert "close" in ev
    assert "prev_close" in ev
    assert ev["close"] == 10.6
    assert ev["prev_close"] == pytest.approx(10.0, abs=0.01)


def test_volume_surge_evidence_fields():
    rows = _flat(21, vol=1000)
    rows[-1] = (10.0, 10.02, 9.98, 10.01, 3000)
    events = detect_events(_frame(rows), "000001")
    e = next(ev for ev in events if ev["event_type"] == "volume_surge")
    ev = e["evidence"]
    assert {"volume", "avg_volume", "volume_ratio", "window"} <= set(ev.keys())
    assert ev["window"] == 20


def test_gap_evidence_fields():
    rows = _flat(3) + [(10.3, 10.4, 10.2, 10.35, 1000)]
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["event_type"] == "gap")
    ev = e["evidence"]
    assert {"gap_pct", "open", "prev_close"} <= set(ev.keys())


def test_limit_move_evidence_fields():
    rows = _flat(3, price=10.0) + [(10.5, 11.0, 10.5, 11.0, 1000)]
    events = detect_events(_frame(rows), "600000")
    e = next(ev for ev in events if ev["event_type"] == "limit_move")
    ev = e["evidence"]
    assert {"side", "limit_pct", "limit_price", "close", "prev_close", "change_pct"} <= set(ev.keys())
    assert ev["side"] in ("up", "down")


def test_no_trading_semantic_fields():
    rows = _flat(3, price=10.0) + [(10.3, 11.0, 10.2, 11.0, 5000), (10.0, 10.4, 9.3, 9.4, 6000)]
    events = detect_events(_frame(rows), "600000")
    assert_clean(events)
    # 确保整个输出 JSON 序列化中不出现禁用词
    import json
    blob = json.dumps(events, ensure_ascii=False)
    for word in ("target_price", "stop_loss", "buy", "sell", "order", "recommendation"):
        assert word not in blob


def test_all_four_event_types_detectable():
    """逐一构造并确认 4 类事件均可被识别。"""
    # price_spike
    r1 = _flat(3, price=10.0) + [(10.0, 10.8, 9.95, 10.6, 1000)]
    assert any(e["event_type"] == "price_spike" for e in detect_events(_frame(r1), "T"))
    # volume_surge
    r2 = _flat(21, vol=1000)
    r2[-1] = (10.0, 10.02, 9.98, 10.01, 3000)
    assert any(e["event_type"] == "volume_surge" for e in detect_events(_frame(r2), "T"))
    # gap
    r3 = _flat(3) + [(10.3, 10.4, 10.2, 10.35, 1000)]
    assert any(e["event_type"] == "gap" for e in detect_events(_frame(r3), "T"))
    # limit_move
    r4 = _flat(3, price=10.0) + [(10.5, 11.0, 10.5, 11.0, 1000)]
    assert any(e["event_type"] == "limit_move" for e in detect_events(_frame(r4), "T"))
