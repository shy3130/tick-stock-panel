"""全量指标路径的量比盘中折算 (compute_indicators.vol_ratio_5d)。

背景: 盘中手动「重载」触发 refresh-cache, 重建缓存从磁盘今日分区跑
compute_indicators — 今日分区是实时快照的部分量, 而全量量比公式按
「volume 已是全天量」直接相除, 量比被低估约 240/elapsed 倍 (实测盘中
重载放量占比 1.8% vs 实时路径 40%)。修复: 最新行是北京今天且盘中
(0 < elapsed < 240) 时, 今日行按已交易分钟折算, 与 live 增量路径同口径;
盘后/盘前/历史数据 (回测、慢路径) 完全不受影响。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators.pipeline import compute_indicators

TODAY = date(2026, 3, 2)  # 周一


def _make_df(include_today: bool = True) -> pl.DataFrame:
    """1 只股票 × 7 天: 前 6 天量 200, 最新日量 100 (盘中部分量)。

    无折算量比 = 100/200 = 0.5; elapsed=95 折算 = 100*(240/95)/200 ≈ 1.2632。
    """
    days = [TODAY - timedelta(days=k) for k in range(6, 0, -1)]  # 前 6 个日历日
    if include_today:
        days.append(TODAY)
    return pl.DataFrame({
        "symbol": ["600000.SH"] * len(days),
        "date": days,
        "open": [10.0] * len(days),
        "high": [10.5] * len(days),
        "low": [9.5] * len(days),
        "close": [10.0] * len(days),
        "volume": [200.0] * (len(days) - 1) + [100.0],
    })


def _vol_ratio_of_latest(df: pl.DataFrame) -> float:
    out = compute_indicators(df)
    return out.sort("date").tail(1)["vol_ratio_5d"][0]


def test_intraday_rebuild_scales_today_volume(monkeypatch):
    """盘中重建缓存: 今日行按 240/elapsed 折算, 历史行保持原口径。"""
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 95.0)
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)

    out = compute_indicators(_make_df()).sort("date")
    today_row = out.tail(1)
    prev_row = out.tail(2).head(1)

    assert today_row["vol_ratio_5d"][0] == 100.0 * (240.0 / 95.0) / 200.0
    # 历史行不受影响: 昨日 volume=200, 其前5日均量=200 → 量比 1.0
    assert prev_row["vol_ratio_5d"][0] == 1.0


def test_after_close_no_scaling(monkeypatch):
    """盘后 (elapsed=240): volume 已是全天量, 与旧公式完全一致。"""
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 240.0)
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    assert _vol_ratio_of_latest(_make_df()) == 0.5


def test_before_open_no_scaling(monkeypatch):
    """盘前 (elapsed=0): 不折算 (此时今日分区尚不存在, 属防御分支)。"""
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 0.0)
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    assert _vol_ratio_of_latest(_make_df()) == 0.5


def test_history_without_today_rows_untouched(monkeypatch):
    """盘中但数据不含今日行 (历史回测/慢路径): 不折算, 行为与修复前一致。"""
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 95.0)
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    # 最新行是昨天: volume=100(残缺列构造同上), 但 date≠today → 不折算
    df = _make_df(include_today=False)
    out = compute_indicators(df).sort("date")
    # 最新历史日 volume=100, 前5日均量=200 → 0.5 (无折算)
    assert out.tail(1)["vol_ratio_5d"][0] == 0.5
