"""depth_service 连续竞价窗口测试 (午间休市涨跌停误判回归).

回归点: _poll_loop 原用 _is_trading_hours() (宽窗口 9:25-11:35 / 12:55-15:05),
12:55-13:00 午后集合竞价准备期会恢复拉 depth, 此时 ask1/bid1 竞价盘口语义使
「涨停价上卖一==0」真封判定失效, 覆盖 11:30 已定格的正确 sealed 值, 导致
涨停股误判为 sealed=False (假涨停) 被错误扣减。

修复: depth sealed 轮询改用 _is_continuous_trading() (9:30-11:30 / 13:00-15:00),
午间 11:30-13:00 停止轮询, 缓存保持 11:30 正确值。窗口定义须与
quote_service._is_continuous_trading 一致。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app.market_time import CN_TZ
from app.services import depth_service
from app.services.depth_service import DepthService


def _cn_now_at(hour: int, minute: int, weekday: int = 0):
    """构造一个北京时间固定时刻的 cn_now 替身。weekday: 0=周一 ... 6=周日。"""
    # 2024-01-01 是周一, 据此定位目标 weekday 的日期
    day = 1 + ((weekday - datetime(2024, 1, 1).weekday()) % 7)
    fixed = datetime(2024, 1, day, hour, minute, tzinfo=CN_TZ)

    def _fn():
        return fixed

    return _fn


def _set_cn_now(hour: int, minute: int, weekday: int = 0):
    """patch depth_service 模块内的 cn_now 到固定时刻, 返回 patcher。"""
    return patch.object(depth_service, "cn_now", _cn_now_at(hour, minute, weekday))


# ── 连续竞价窗口边界 (应返回 True) ───────────────────────────────────
def test_morning_open_930_is_continuous():
    with _set_cn_now(9, 30, weekday=0):
        assert DepthService._is_continuous_trading() is True


def test_morning_close_1130_is_continuous():
    """11:30 早盘最后一刻, 仍连续竞价, 应拉 depth 定格正确值。"""
    with _set_cn_now(11, 30, weekday=0):
        assert DepthService._is_continuous_trading() is True


def test_afternoon_open_1300_is_continuous():
    with _set_cn_now(13, 0, weekday=0):
        assert DepthService._is_continuous_trading() is True


def test_afternoon_close_1500_is_continuous():
    with _set_cn_now(15, 0, weekday=0):
        assert DepthService._is_continuous_trading() is True


# ── 回归关键点: 午间休市 + 集合竞价准备期必须停止轮询 (应返回 False) ──
def test_lunch_break_1200_not_continuous():
    """12:00 午间休市, 必须停止轮询。"""
    with _set_cn_now(12, 0, weekday=0):
        assert DepthService._is_continuous_trading() is False


def test_auction_prep_1255_not_continuous():
    """12:55 午后集合竞价准备期, 必须停止轮询 (最关键回归点)。

    这是原 bug 的根因时刻: 宽窗口在此恢复拉 depth, 竞价盘口覆盖 11:30 正确值。
    """
    with _set_cn_now(12, 55, weekday=0):
        assert DepthService._is_continuous_trading() is False


def test_just_before_open_1259_not_continuous():
    """12:59, 13:00 开盘前 1 分钟, 仍属集合竞价准备期, 不可拉。"""
    with _set_cn_now(12, 59, weekday=0):
        assert DepthService._is_continuous_trading() is False


def test_just_after_morning_close_1131_not_continuous():
    """11:31 早盘刚收盘, 不可拉 (定格 11:30)。"""
    with _set_cn_now(11, 31, weekday=0):
        assert DepthService._is_continuous_trading() is False


def test_morning_auction_925_not_continuous():
    """9:25 开盘集合竞价, 非连续竞价, 不可拉 (避免指示价误判)。"""
    with _set_cn_now(9, 25, weekday=0):
        assert DepthService._is_continuous_trading() is False


def test_after_close_1505_not_continuous():
    """15:05 收盘后, 不可拉。"""
    with _set_cn_now(15, 5, weekday=0):
        assert DepthService._is_continuous_trading() is False


# ── 周末全天不交易 ────────────────────────────────────────────────────
def test_weekend_saturday_not_trading():
    """10:30 周六, 落在连续竞价时间区间内, 但周末不交易。"""
    with _set_cn_now(10, 30, weekday=5):
        assert DepthService._is_continuous_trading() is False


def test_weekend_sunday_not_trading():
    """14:00 周日, 落在连续竞价时间区间内, 但周末不交易。"""
    with _set_cn_now(14, 0, weekday=6):
        assert DepthService._is_continuous_trading() is False


# ── 与 quote_service 窗口定义一致性 ──────────────────────────────────
def test_window_matches_quote_service():
    """depth_service._is_continuous_trading 必须与 quote_service 同名方法逐点一致。

    同时 patch 两个模块各自的 cn_now 引用, 确保比较的是同一时刻下的窗口判定。
    """
    from app.services import quote_service
    from app.services.quote_service import QuoteService

    for weekday in range(7):
        for hour in range(0, 24):
            for minute in (0, 30):
                fn = _cn_now_at(hour, minute, weekday)
                with patch.object(depth_service, "cn_now", fn), \
                        patch.object(quote_service, "cn_now", fn):
                    assert DepthService._is_continuous_trading() == QuoteService._is_continuous_trading(), (
                        f"窗口不一致 @ weekday={weekday} {hour:02d}:{minute:02d}"
                    )
