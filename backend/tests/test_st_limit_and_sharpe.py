"""回归测试:

1. ST 5% 涨跌停限幅仅适用于主板风险警示股; 创业板/科创板 ST 仍执行 20%。
   (修正前 _is_st 无条件套 5%, 会误报/漏报这批股的涨停。)
2. 因子回测 Sharpe 的年化系数须匹配调仓频率 (月频 √12 / 周频 √52 / 日频 √252);
   (修正前一律 √252, 月频 Sharpe 被高估 √21 ≈ 4.6 倍。)
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.backtest.factor import FactorBacktestService
from app.indicators.pipeline import compute_limit_signals
from app.strategy.builtin.near_limit_up import _limit_pct


def test_near_limit_pct_st_only_on_main_board():
    df = pl.DataFrame({
        "symbol": ["300001", "688001", "600001", "000001", "830001.BJ"],
        "name": ["*ST创业", "科创ST", "*ST主板", "平安银行", "北交ST"],
    })
    lp = df.with_columns(_limit_pct().alias("lp"))["lp"].to_list()
    assert lp[0] == 0.20  # 创业板 ST → 20% (不再是 5%)
    assert lp[1] == 0.20  # 科创板 ST → 20%
    assert lp[2] == 0.05  # 主板 ST → 5%
    assert lp[3] == 0.10  # 主板普通 → 10%
    assert lp[4] == 0.30  # 北交所 → 30%


def _two_day(symbol: str, prev_close: float, today_close: float) -> pl.DataFrame:
    """2 日最小输入: 首日平收, 次日收于 today_close。"""
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "raw_close": [prev_close, today_close],
        "close": [prev_close, today_close],
        "raw_high": [prev_close, today_close],
        "open": [prev_close, today_close],
        "high": [prev_close, today_close],
        "low": [prev_close, today_close],
        "change_pct": [0.0, today_close / prev_close - 1],
        "vol_ratio_5d": [1.0, 1.0],
    })


def _last_limit_up(symbol: str, name: str, prev_close: float, today_close: float):
    df = _two_day(symbol, prev_close, today_close)
    inst = pl.DataFrame({"symbol": [symbol], "name": [name]})
    out = compute_limit_signals(df, inst).sort("date")
    return out["signal_limit_up"].to_list()[-1], out["consecutive_limit_ups"].to_list()[-1]


def test_st_chinext_limit_up_detected_at_20pct():
    # 创业板 *ST 昨收 10.00 → 今日 +20% 至 12.00 应识别为涨停 (修正前按 5% 会漏)
    sig, consec = _last_limit_up("300001", "*ST创业", 10.0, 12.0)
    assert sig is True
    assert consec == 1


def test_st_chinext_plus5pct_is_not_a_false_limit_up():
    # 同股仅 +5% 至 10.50 不应误报涨停 (修正前按 5% 会误报)
    sig, _ = _last_limit_up("300001", "*ST创业", 10.0, 10.5)
    assert sig is False


def test_st_main_board_still_limits_at_5pct():
    # 主板 *ST 昨收 10.00 → 今日 +5% 至 10.50 仍应识别为涨停
    sig, _ = _last_limit_up("600001", "*ST主板", 10.0, 10.5)
    assert sig is True


def test_sharpe_annualization_matches_rebalance_frequency():
    nav = [
        {"date": "2024-01-31", "Q1": 1.00},
        {"date": "2024-02-29", "Q1": 1.02},
        {"date": "2024-03-29", "Q1": 1.01},
        {"date": "2024-04-30", "Q1": 1.05},
        {"date": "2024-05-31", "Q1": 1.04},
        {"date": "2024-06-28", "Q1": 1.08},
    ]
    start, end = date(2024, 1, 1), date(2024, 6, 30)
    m = FactorBacktestService._calc_group_stats(nav, start, end, "monthly")[0]["sharpe"]
    d = FactorBacktestService._calc_group_stats(nav, start, end, "daily")[0]["sharpe"]
    assert m != 0.0 and d != 0.0
    # 同一净值曲线, daily(√252) / monthly(√12) 的比值应为 √21 ≈ 4.58
    assert abs((d / m) - (252 / 12) ** 0.5) < 0.05
