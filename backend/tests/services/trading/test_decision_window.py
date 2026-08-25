"""决策窗口 / 冷却期 / 重复抑制 — 纯函数门禁测试。

覆盖: 窗口内/外、冷却中/结束、重复键、时区、跨日边界、缺失时间、无效配置 fail-closed。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.trading.decision_window import (
    DEFAULT_COOLDOWN_MINUTES,
    is_cooldown_satisfied,
    is_duplicate_suppressed,
    is_window_open,
    evaluate_decision_window,
    next_window_open,
    DecisionWindowError,
)

SH = ZoneInfo("Asia/Shanghai")          # UTC+8
UTC = timezone.utc


def _dt(y, mo, d, h, mi, tz=SH) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# ══════════════════════════════════════════════════════════
# 1. 决策窗口 is_window_open
# ══════════════════════════════════════════════════════════
def test_window_open_inside():
    """收盘后窗口内 → open=True。"""
    win = [{"name": "after_close", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"}]
    now = _dt(2026, 8, 14, 16, 30)
    r = is_window_open(win, now=now)
    assert r["open"] is True
    assert r["matched"] == "after_close"
    assert r["next_open_at"] is None


def test_window_closed_outside_returns_next_open():
    """窗口外 → open=False, next_open_at 指向当天开盘。"""
    win = [{"name": "after_close", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"}]
    now = _dt(2026, 8, 14, 10, 0)  # 盘中
    r = is_window_open(win, now=now)
    assert r["open"] is False
    assert r["matched"] is None
    assert r["next_open_at"] == _dt(2026, 8, 14, 15, 5).isoformat()


def test_window_multiple_match_first_wins():
    """多个窗口, 命中第一个。"""
    win = [
        {"name": "pre_open", "start": "08:30", "end": "09:15", "tz": "Asia/Shanghai"},
        {"name": "after_close", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"},
    ]
    now = _dt(2026, 8, 14, 9, 0)
    r = is_window_open(win, now=now)
    assert r["open"] is True
    assert r["matched"] == "pre_open"


def test_window_empty_means_always_open():
    """空窗口列表 → 无限制, 永远开放。"""
    r = is_window_open([], now=_dt(2026, 8, 14, 3, 0))
    assert r["open"] is True
    assert r["next_open_at"] is None


def test_window_none_means_always_open():
    r = is_window_open(None, now=_dt(2026, 8, 14, 3, 0))
    assert r["open"] is True


def test_window_cross_day_overnight_inside():
    """跨午夜窗口 (22:00-02:00): 23:00 在内。"""
    win = [{"name": "night", "start": "22:00", "end": "02:00", "tz": "Asia/Shanghai"}]
    now = _dt(2026, 8, 14, 23, 30)
    r = is_window_open(win, now=now)
    assert r["open"] is True
    assert r["matched"] == "night"


def test_window_cross_day_overnight_after_midnight():
    """跨午夜窗口: 01:00 (次日) 在内。"""
    win = [{"name": "night", "start": "22:00", "end": "02:00", "tz": "Asia/Shanghai"}]
    now = _dt(2026, 8, 15, 1, 0)
    r = is_window_open(win, now=now)
    assert r["open"] is True
    assert r["matched"] == "night"


def test_window_cross_day_outside():
    """跨午夜窗口: 10:00 在外。"""
    win = [{"name": "night", "start": "22:00", "end": "02:00", "tz": "Asia/Shanghai"}]
    now = _dt(2026, 8, 14, 10, 0)
    r = is_window_open(win, now=now)
    assert r["open"] is False


def test_window_boundary_start_inclusive():
    """窗口起点闭区间: 15:05 恰好开放。"""
    win = [{"name": "w", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"}]
    r = is_window_open(win, now=_dt(2026, 8, 14, 15, 5))
    assert r["open"] is True


def test_window_boundary_end_exclusive():
    """窗口终点开区间: 23:00 恰好关闭。"""
    win = [{"name": "w", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"}]
    r = is_window_open(win, now=_dt(2026, 8, 14, 23, 0))
    assert r["open"] is False
    # next_open 应是次日 15:05
    assert r["next_open_at"] == _dt(2026, 8, 15, 15, 5).isoformat()


def test_window_tz_conversion():
    """不同时区窗口: UTC 窗口在 Shanghai 时间换算后命中。"""
    # UTC 07:00-09:00 = SH 15:00-17:00
    win = [{"name": "utc_w", "start": "07:00", "end": "09:00", "tz": "UTC"}]
    now = _dt(2026, 8, 14, 16, 0, SH)  # = UTC 08:00
    r = is_window_open(win, now=now)
    assert r["open"] is True
    assert r["matched"] == "utc_w"


def test_window_naive_now_localized():
    """naive now 按默认时区本地化, 不报错。"""
    win = [{"name": "w", "start": "15:00", "end": "16:00", "tz": "Asia/Shanghai"}]
    naive = datetime(2026, 8, 14, 15, 30)  # naive
    r = is_window_open(win, now=naive, default_tz="Asia/Shanghai")
    assert r["open"] is True


def test_window_bad_entries_dropped():
    """坏窗口项被丢弃, 剩余有效项正常判定。"""
    win = [
        {"name": "", "start": "15:00", "end": "16:00"},            # 空名 → 丢弃
        {"name": "bad", "start": "25:00", "end": "26:00"},         # 非法时间 → 丢弃
        {"name": "good", "start": "10:00", "end": "11:00"},        # 有效
        "not-a-dict",                                              # 非法 → 丢弃
    ]
    r = is_window_open(win, now=_dt(2026, 8, 14, 10, 30))
    assert r["open"] is True
    assert r["matched"] == "good"


def test_window_zero_length_dropped():
    """start==end 的零长度窗口无效。"""
    win = [{"name": "z", "start": "12:00", "end": "12:00"}]
    r = is_window_open(win, now=_dt(2026, 8, 14, 12, 0))
    assert r["open"] is True  # 全部丢弃 → 无限制


# ══════════════════════════════════════════════════════════
# 2. next_window_open
# ══════════════════════════════════════════════════════════
def test_next_window_open_already_inside_returns_now():
    win = [{"name": "w", "start": "15:00", "end": "16:00", "tz": "Asia/Shanghai"}]
    nxt = next_window_open(win, _dt(2026, 8, 14, 15, 30))
    assert nxt == _dt(2026, 8, 14, 15, 30)


def test_next_window_open_before_today_open():
    win = [{"name": "w", "start": "15:00", "end": "16:00", "tz": "Asia/Shanghai"}]
    nxt = next_window_open(win, _dt(2026, 8, 14, 9, 0))
    assert nxt == _dt(2026, 8, 14, 15, 0)


def test_next_window_open_after_today_rolls_tomorrow():
    win = [{"name": "w", "start": "15:00", "end": "16:00", "tz": "Asia/Shanghai"}]
    nxt = next_window_open(win, _dt(2026, 8, 14, 20, 0))
    assert nxt == _dt(2026, 8, 15, 15, 0)


def test_next_window_open_overnight_window():
    """跨午夜窗口, now 在窗口外白天 → 当晚开放。"""
    win = [{"name": "night", "start": "22:00", "end": "02:00", "tz": "Asia/Shanghai"}]
    nxt = next_window_open(win, _dt(2026, 8, 14, 10, 0))
    assert nxt == _dt(2026, 8, 14, 22, 0)


def test_next_window_open_empty_returns_after():
    assert next_window_open([], _dt(2026, 8, 14, 3, 0)) == _dt(2026, 8, 14, 3, 0)


# ══════════════════════════════════════════════════════════
# 3. 冷却期 is_cooldown_satisfied
# ══════════════════════════════════════════════════════════
def test_cooldown_ended():
    """间隔 >= 冷却期 → satisfied=True。"""
    started = _dt(2026, 8, 14, 15, 0)
    now = _dt(2026, 8, 14, 15, 11)  # 11 分钟 >= 10
    r = is_cooldown_satisfied(started, 10, now=now)
    assert r["satisfied"] is True
    assert r["next_allowed_at"] is None
    assert r["elapsed_minutes"] >= 10


def test_cooldown_cooling():
    """间隔 < 冷却期 → satisfied=False, next_allowed_at = started + cooldown。"""
    started = _dt(2026, 8, 14, 15, 0)
    now = _dt(2026, 8, 14, 15, 3)  # 3 分钟 < 10
    r = is_cooldown_satisfied(started, 10, now=now)
    assert r["satisfied"] is False
    assert r["remaining_minutes"] == pytest.approx(7.0)
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 15, 10).isoformat()


def test_cooldown_exact_boundary_satisfied():
    """恰好到冷却期 → satisfied=True (>=)。"""
    started = _dt(2026, 8, 14, 15, 0)
    now = _dt(2026, 8, 14, 15, 10)  # 恰好 10 分钟
    r = is_cooldown_satisfied(started, 10, now=now)
    assert r["satisfied"] is True


def test_cooldown_missing_started_fail_closed():
    """started_at 缺失 → fail-closed (拒绝)。"""
    r = is_cooldown_satisfied(None, 10, now=_dt(2026, 8, 14, 16, 0))
    assert r["satisfied"] is False
    assert r["next_allowed_at"] is None
    assert "started_at" in r["detail"]


def test_cooldown_invalid_zero_fail_closed():
    """冷却期 <= 0 → fail-closed。"""
    r = is_cooldown_satisfied(_dt(2026, 8, 14, 15, 0), 0, now=_dt(2026, 8, 14, 16, 0))
    assert r["satisfied"] is False
    assert r["evidence"]["config_invalid"] is True


def test_cooldown_invalid_negative_fail_closed():
    r = is_cooldown_satisfied(_dt(2026, 8, 14, 15, 0), -5, now=_dt(2026, 8, 14, 16, 0))
    assert r["satisfied"] is False
    assert r["evidence"]["config_invalid"] is True


def test_cooldown_nan_fail_closed():
    nan = float("nan")
    r = is_cooldown_satisfied(_dt(2026, 8, 14, 15, 0), nan, now=_dt(2026, 8, 14, 16, 0))
    assert r["satisfied"] is False


def test_cooldown_clock_skew_fail_closed():
    """started_at 晚于 now (时钟回拨) → fail-closed。"""
    started = _dt(2026, 8, 14, 16, 0)
    now = _dt(2026, 8, 14, 15, 0)
    r = is_cooldown_satisfied(started, 10, now=now)
    assert r["satisfied"] is False
    assert r["evidence"]["clock_skew"] is True


def test_cooldown_cross_day_boundary():
    """跨日: 23:50 开始, 次日 00:05 = 15 分钟 → satisfied。"""
    started = _dt(2026, 8, 14, 23, 50)
    now = _dt(2026, 8, 15, 0, 5)
    r = is_cooldown_satisfied(started, 10, now=now)
    assert r["satisfied"] is True


def test_cooldown_naive_started_localized():
    """naive started_at 按默认时区本地化, 记 warning。"""
    naive_started = datetime(2026, 8, 14, 15, 0)
    now = _dt(2026, 8, 14, 15, 11)
    r = is_cooldown_satisfied(naive_started, 10, now=now, default_tz="Asia/Shanghai")
    assert r["satisfied"] is True
    assert any("naive" in w for w in r["evidence"]["warnings"])


def test_cooldown_different_tz_aware():
    """started 用 UTC, now 用 SH, 间隔正确计算。"""
    started_utc = datetime(2026, 8, 14, 7, 0, tzinfo=UTC)   # = SH 15:00
    now_sh = _dt(2026, 8, 14, 15, 11)                        # SH
    r = is_cooldown_satisfied(started_utc, 10, now=now_sh)
    assert r["satisfied"] is True


# ══════════════════════════════════════════════════════════
# 4. 重复抑制 is_duplicate_suppressed
# ══════════════════════════════════════════════════════════
def test_duplicate_suppressed_within_window():
    """同键在抑制窗口内 → suppressed=True。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [{"key": "600519.SH:buy_new", "at": _dt(2026, 8, 14, 15, 40)}]  # 20 分钟前
    r = is_duplicate_suppressed("600519.SH:buy_new", recent, 60, now=now)
    assert r["suppressed"] is True
    assert r["matched_key"] == "600519.SH:buy_new"
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 16, 40).isoformat()


def test_duplicate_not_suppressed_outside_window():
    """同键在抑制窗口外 → 不抑制。"""
    now = _dt(2026, 8, 14, 17, 0)
    recent = [{"key": "600519.SH:buy_new", "at": _dt(2026, 8, 14, 15, 40)}]  # 80 分钟前 > 60
    r = is_duplicate_suppressed("600519.SH:buy_new", recent, 60, now=now)
    assert r["suppressed"] is False


def test_duplicate_different_key_not_suppressed():
    """不同键 → 不抑制。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [{"key": "600519.SH:buy_new", "at": _dt(2026, 8, 14, 15, 40)}]
    r = is_duplicate_suppressed("000001.SZ:buy_new", recent, 60, now=now)
    assert r["suppressed"] is False


def test_duplicate_takes_latest_within_window():
    """多条匹配取最新一条, next_allowed_at 基于最新。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [
        {"key": "k1", "at": _dt(2026, 8, 14, 15, 10)},  # 旧
        {"key": "k1", "at": _dt(2026, 8, 14, 15, 50)},  # 新
    ]
    r = is_duplicate_suppressed("k1", recent, 60, now=now)
    assert r["suppressed"] is True
    assert r["matched_at"] == _dt(2026, 8, 14, 15, 50).isoformat()
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 16, 50).isoformat()


def test_duplicate_empty_key_not_suppressed():
    """空决策键 → 不抑制 (无法去重)。"""
    r = is_duplicate_suppressed("", [{"key": "", "at": _dt(2026, 8, 14, 15, 0)}], 60,
                                now=_dt(2026, 8, 14, 15, 10))
    assert r["suppressed"] is False


def test_duplicate_no_recent_not_suppressed():
    r = is_duplicate_suppressed("k1", None, 60, now=_dt(2026, 8, 14, 16, 0))
    assert r["suppressed"] is False


def test_duplicate_zero_suppression_disabled():
    """抑制窗口 <= 0 → 关闭抑制。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [{"key": "k1", "at": _dt(2026, 8, 14, 15, 59)}]
    r = is_duplicate_suppressed("k1", recent, 0, now=now)
    assert r["suppressed"] is False


def test_duplicate_bad_entries_ignored():
    """坏条目 (非 dict / at 非 datetime) 被丢弃, 不报错。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [
        "not-a-dict",
        {"key": "k1", "at": "2026-08-14 15:00"},  # at 非 datetime → 丢弃
        {"key": "k1"},                              # 无 at → 丢弃
        {"key": "k1", "at": _dt(2026, 8, 14, 15, 50)},  # 有效
    ]
    r = is_duplicate_suppressed("k1", recent, 60, now=now)
    assert r["suppressed"] is True
    assert r["evidence"]["bad_entries"] == 3


def test_duplicate_future_entry_ignored():
    """未来时间条目 (时钟错位) 被忽略。"""
    now = _dt(2026, 8, 14, 16, 0)
    recent = [{"key": "k1", "at": _dt(2026, 8, 14, 17, 0)}]  # 未来
    r = is_duplicate_suppressed("k1", recent, 60, now=now)
    assert r["suppressed"] is False


# ══════════════════════════════════════════════════════════
# 5. 聚合 evaluate_decision_window
# ══════════════════════════════════════════════════════════
def _full_config():
    return {
        "cooldownMinutes": 10,
        "suppressionMinutes": 60,
        "windows": [{"name": "after_close", "start": "15:05", "end": "23:00", "tz": "Asia/Shanghai"}],
        "tz": "Asia/Shanghai",
    }


def test_aggregate_all_pass_allowed():
    """窗口开 + 冷却过 + 无重复 → allowed=True。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 16, 0),
        started_at=_dt(2026, 8, 14, 15, 50),  # 10 分钟前
        decision_key="600519.SH:buy_new",
        recent_decisions=[],
    )
    assert r["allowed"] is True
    assert r["next_allowed_at"] is None


def test_aggregate_window_closed_blocked():
    """窗口关闭 → allowed=False, next_allowed_at=窗口开放 + 冷却。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 10, 0),          # 盘中
        started_at=_dt(2026, 8, 14, 9, 0),     # 冷却已过 (60 分钟)
        decision_key="k1",
        recent_decisions=[],
    )
    assert r["allowed"] is False
    assert "决策窗口未开放" in r["reason"]
    # 窗口 15:05 开放, 冷却已过 → 15:05
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 15, 5).isoformat()


def test_aggregate_cooldown_blocked():
    """冷却未过 → allowed=False, next_allowed_at 综合窗口+冷却。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 15, 10),         # 窗口已开
        started_at=_dt(2026, 8, 14, 15, 5),    # 才 5 分钟 < 10
        decision_key="k1",
        recent_decisions=[],
    )
    assert r["allowed"] is False
    assert "冷却期未过" in r["reason"]
    # 冷却截止 15:15, 窗口已开 → 15:15
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 15, 15).isoformat()


def test_aggregate_duplicate_blocked():
    """重复抑制 → allowed=False, next_allowed_at 综合窗口+抑制。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 16, 0),
        started_at=_dt(2026, 8, 14, 15, 0),    # 冷却过
        decision_key="600519.SH:buy_new",
        recent_decisions=[{"key": "600519.SH:buy_new", "at": _dt(2026, 8, 14, 15, 40)}],
    )
    assert r["allowed"] is False
    assert "重复决策被抑制" in r["reason"]
    # 抑制截止 16:40, 窗口已开 → 16:40
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 16, 40).isoformat()


def test_aggregate_all_three_blocked():
    """三项都阻塞 → next_allowed_at 取最晚约束 + 窗口对齐。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 10, 0),          # 窗口关
        started_at=_dt(2026, 8, 14, 9, 55),    # 5 分钟 < 10
        decision_key="k1",
        recent_decisions=[{"key": "k1", "at": _dt(2026, 8, 14, 9, 50)}],  # 10 分钟前 < 60
    )
    assert r["allowed"] is False
    # 冷却截止 9:55+10=10:05; 抑制截止 9:50+60=10:50; binding=10:50; 窗口对齐→15:05
    assert r["next_allowed_at"] == _dt(2026, 8, 14, 15, 5).isoformat()


def test_aggregate_missing_started_fail_closed():
    """聚合缺失 started_at → 冷却 fail-closed → allowed=False。"""
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 16, 0),
        started_at=None,
        decision_key="k1",
    )
    assert r["allowed"] is False
    assert r["checks"]["cooldown"]["satisfied"] is False


def test_aggregate_no_windows_still_works():
    """无窗口配置 → 仅看冷却+重复。"""
    cfg = {"cooldownMinutes": 10, "suppressionMinutes": 60}
    r = evaluate_decision_window(
        cfg,
        now=_dt(2026, 8, 14, 3, 0),
        started_at=_dt(2026, 8, 14, 2, 0),
        decision_key="k1",
    )
    assert r["allowed"] is True
    assert r["checks"]["window"]["open"] is True


# ══════════════════════════════════════════════════════════
# 6. 无效配置 fail-closed
# ══════════════════════════════════════════════════════════
def test_aggregate_bad_timezone_fail_closed():
    """未知时区 → fail-closed。"""
    cfg = {"tz": "Mars/Olympus", "windows": []}
    r = evaluate_decision_window(cfg, now=_dt(2026, 8, 14, 16, 0), started_at=_dt(2026, 8, 14, 15, 0))
    assert r["allowed"] is False
    assert r["evidence"]["config_invalid"] is True


def test_aggregate_windows_not_list_fail_closed():
    """windows 非 list → fail-closed。"""
    cfg = {"windows": {"not": "a list"}}
    r = evaluate_decision_window(cfg, now=_dt(2026, 8, 14, 16, 0), started_at=_dt(2026, 8, 14, 15, 0))
    assert r["allowed"] is False
    assert r["evidence"]["config_invalid"] is True


def test_aggregate_negative_cooldown_fail_closed():
    """cooldownMinutes 负数 → 冷却 fail-closed。"""
    cfg = {"cooldownMinutes": -1}
    r = evaluate_decision_window(cfg, now=_dt(2026, 8, 14, 16, 0), started_at=_dt(2026, 8, 14, 15, 0))
    assert r["allowed"] is False
    assert r["checks"]["cooldown"]["satisfied"] is False
    assert r["checks"]["cooldown"]["evidence"]["config_invalid"] is True


def test_aggregate_none_config_fail_closed():
    """config=None → 视为空 dict (无窗口限制), 冷却默认值生效。"""
    # config=None 等价空配置: cooldown=默认10, 无窗口。started_at 缺失 → 冷却 fail-closed
    r = evaluate_decision_window(None, now=_dt(2026, 8, 14, 16, 0), started_at=None)
    assert r["allowed"] is False
    assert r["checks"]["cooldown"]["satisfied"] is False


def test_aggregate_default_cooldown_value():
    """未指定 cooldownMinutes → 用 DEFAULT_COOLDOWN_MINUTES。"""
    cfg = {}
    now = _dt(2026, 8, 14, 16, DEFAULT_COOLDOWN_MINUTES + 1)
    r = evaluate_decision_window(cfg, now=now, started_at=_dt(2026, 8, 14, 16, 0))
    assert r["checks"]["cooldown"]["satisfied"] is True


# ══════════════════════════════════════════════════════════
# 7. 时区 / 跨日 综合
# ══════════════════════════════════════════════════════════
def test_aggregate_utc_window_with_shanghai_now():
    """窗口在 UTC, now 在 Shanghai tz, 换算后命中。"""
    cfg = {
        "cooldownMinutes": 5,
        "windows": [{"name": "utc_morning", "start": "07:00", "end": "08:00", "tz": "UTC"}],
        "tz": "UTC",
    }
    # SH 15:30 = UTC 07:30 → 命中
    now = _dt(2026, 8, 14, 15, 30, SH)
    started = now - timedelta(minutes=10)
    r = evaluate_decision_window(cfg, now=now, started_at=started, decision_key="k1")
    assert r["allowed"] is True


def test_aggregate_overnight_window_allowed_late_night():
    """跨午夜窗口: 深夜 + 次日凌晨都允许。"""
    cfg = {
        "cooldownMinutes": 5,
        "windows": [{"name": "night", "start": "22:00", "end": "02:00", "tz": "Asia/Shanghai"}],
    }
    # 次日 01:00 在窗口内, 冷却过
    now = _dt(2026, 8, 15, 1, 0)
    started = _dt(2026, 8, 14, 23, 0)
    r = evaluate_decision_window(cfg, now=now, started_at=started, decision_key="k1")
    assert r["allowed"] is True


def test_aggregate_now_defaults_to_current():
    """now=None 不报错, 用当前时刻。"""
    cfg = {"cooldownMinutes": 5}
    r = evaluate_decision_window(cfg, now=None, started_at=None)
    assert r["allowed"] is False  # started_at 缺失 → fail-closed
    assert "now" in r["evidence"]


def test_decision_window_error_on_bad_tz_helper():
    """_tz 辅助对未知时区抛 DecisionWindowError。"""
    from app.services.trading.decision_window import _tz
    with pytest.raises(DecisionWindowError):
        _tz("Not/A/Zone")


def test_output_serializable_iso_strings():
    """所有 next_allowed_at / matched_at 均为 ISO 字符串或 None (可序列化)。"""
    now = _dt(2026, 8, 14, 16, 0)
    cfg = _full_config()
    r = evaluate_decision_window(
        cfg, now=now, started_at=_dt(2026, 8, 14, 15, 50),
        decision_key="k1",
        recent_decisions=[{"key": "k1", "at": _dt(2026, 8, 14, 15, 30)}],
    )
    # 检查每项输出字段类型
    for field in ("next_allowed_at",):
        v = r.get(field)
        assert v is None or isinstance(v, str)
    for chk in r["checks"].values():
        v = chk.get("next_allowed_at")
        assert v is None or isinstance(v, str)
    # 可被 json 序列化 (无 datetime 残留)
    import json
    json.dumps(r)
