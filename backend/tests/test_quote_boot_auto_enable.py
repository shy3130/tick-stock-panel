"""启动时交易时段自动开启实时行情 (QuoteService.boot_check)。

需求: 程序启动时判断是否交易时间, 是则自动开启实时行情 (免手动点开关)。

关键边界:
- 时段判据必须是连续竞价 (_is_continuous_trading, 与 status.is_trading_hours 同口径)。
  不能用 _is_trading_hours: 它是轮询窗口, 收盘定版未完成时工作日夜间也为真, 会在半夜自动开启。
- none 档无实时权限、watchlist 模式未配置自选标的时不自动开启 (与手动开启门禁一致)。
- 开关已开启时行为不变 (与时段无关照常启动)。
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch

from app.market_time import CN_TZ
from app.services import preferences as prefs
from app.services import quote_service as qs_module
from app.services.quote_service import QuoteService


def _cn_now_at(hour: int, minute: int, weekday: int = 0):
    """构造固定北京时刻的 cn_now 替身。weekday: 0=周一 ... 6=周日。"""
    # 2024-01-01 是周一, 据此定位目标 weekday 的日期
    day = 1 + ((weekday - datetime(2024, 1, 1).weekday()) % 7)
    fixed = datetime(2024, 1, day, hour, minute, tzinfo=CN_TZ)
    return lambda: fixed


def _run_boot_check(
    hour: int,
    minute: int,
    *,
    weekday: int = 0,
    prefs_enabled: bool = False,
    mode: str = "full_market",
    watchlist: tuple[str, ...] = (),
):
    """在固定北京时刻 + 固定档位/开关下执行 boot_check。

    返回 (start_mock, save_enabled_mock): start 被拦下, 不真启轮询线程 (无网络请求)。
    """
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(qs_module, "cn_now", _cn_now_at(hour, minute, weekday)))
        enter(patch.object(QuoteService, "realtime_mode", classmethod(lambda cls: mode)))
        enter(patch.object(prefs, "get_realtime_quotes_enabled", return_value=prefs_enabled))
        enter(patch.object(prefs, "get_realtime_watchlist_symbols", return_value=list(watchlist)))
        start = enter(patch.object(QuoteService, "start"))
        save = enter(patch.object(QuoteService, "_save_enabled"))
        QuoteService().boot_check()
    return start, save


# ── 交易时段: 自动开启 ────────────────────────────────────────────────
def test_morning_session_auto_enables():
    """周一 10:30 早盘, 开关为关 → 自动开启。"""
    start, _ = _run_boot_check(10, 30)
    assert start.call_count == 1


def test_afternoon_session_auto_enables():
    """周一 14:00 午后, 开关为关 → 自动开启。"""
    start, _ = _run_boot_check(14, 0)
    assert start.call_count == 1


def test_open_boundary_930_auto_enables():
    start, _ = _run_boot_check(9, 30)
    assert start.call_count == 1


def test_close_boundary_1500_auto_enables():
    start, _ = _run_boot_check(15, 0)
    assert start.call_count == 1


# ── 非交易时段: 保持关闭 ──────────────────────────────────────────────
def test_lunch_break_does_not_auto_enable():
    """12:00 午休, 无连续竞价 → 不自动开启。"""
    start, _ = _run_boot_check(12, 0)
    assert start.call_count == 0


def test_after_close_does_not_auto_enable():
    """22:00 工作日夜间 → 不自动开启。

    关键回归点: 此刻 _is_trading_hours (轮询窗口) 因收盘定版未完成而为真,
    若用它做判据, 半夜启动会自动开启实时行情。
    """
    start, _ = _run_boot_check(22, 0)
    assert start.call_count == 0


def test_preopen_auction_does_not_auto_enable():
    """9:20 开盘集合竞价 (指示价, 非连续竞价) → 不自动开启。"""
    start, _ = _run_boot_check(9, 20)
    assert start.call_count == 0


def test_weekend_does_not_auto_enable():
    """周六 10:30 落在时间区间内, 但非交易日 → 不自动开启。"""
    start, _ = _run_boot_check(10, 30, weekday=5)
    assert start.call_count == 0


# ── 权限 / 标的门禁 ───────────────────────────────────────────────────
def test_none_tier_does_not_auto_enable():
    """none 档无实时权限, 交易时段也不自动开启。"""
    start, _ = _run_boot_check(10, 30, mode="none")
    assert start.call_count == 0


def test_none_tier_forces_pref_off():
    """none 档 + 开关残留为开 → 不启动并回写关闭 (原有行为不变)。"""
    start, save = _run_boot_check(10, 30, mode="none", prefs_enabled=True)
    assert start.call_count == 0
    save.assert_called_once_with(False)


def test_watchlist_mode_without_symbols_does_not_auto_enable():
    """watchlist 模式未配置自选标的 → 不自动开启 (与手动开启 watchlist_empty 门禁一致)。"""
    start, _ = _run_boot_check(10, 30, mode="watchlist")
    assert start.call_count == 0


def test_watchlist_mode_with_symbols_auto_enables():
    start, _ = _run_boot_check(10, 30, mode="watchlist", watchlist=("600000.SH",))
    assert start.call_count == 1


# ── 开关已开启: 原有行为不变 ──────────────────────────────────────────
def test_pref_enabled_starts_outside_trading_hours():
    """开关已开启时与时段无关照常启动 (线程内部按 market_phase 决定是否取数)。"""
    start, _ = _run_boot_check(22, 0, prefs_enabled=True)
    assert start.call_count == 1


# ── 自动开启必须持久化 ────────────────────────────────────────────────
def test_auto_enable_persists_enabled_flag():
    """前端开关读的是 preferences.realtime_quotes_enabled, 自动开启必须回写 True,
    否则 UI 显示"已关闭"而服务在跑, 且 depth 轮询 (同样读该开关) 不会启动。"""
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(qs_module, "cn_now", _cn_now_at(10, 30)))
        enter(patch.object(QuoteService, "realtime_mode", classmethod(lambda cls: "full_market")))
        enter(patch.object(prefs, "get_realtime_quotes_enabled", return_value=False))
        enter(patch.object(prefs, "get_realtime_quote_interval", return_value=6.0))
        # 轮询体置空: 线程立即退出, 不发网络请求
        enter(patch.object(QuoteService, "_poll_loop", lambda self: None))
        save = enter(patch.object(prefs, "save"))

        svc = QuoteService()
        svc.boot_check()
        assert svc._enabled is True
        svc.stop()

    saved = [c.args[0].get("realtime_quotes_enabled") for c in save.call_args_list if c.args]
    assert True in saved
