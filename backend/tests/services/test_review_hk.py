"""港股复盘口径测试。

港股与 A 股最容易串的两件事,这里各锁一条:
  1. 分桶边界 —— 港股无涨跌停,桶放宽到 ±7%,不能照抄 A 股的 ±5%;
  2. 单位 —— provider 给的 change_pct 已是百分数,不能再 ×100。
"""
from __future__ import annotations

from app.services.review_hk import STRONG_PCT, _pct_bands


def test_pct_bands_widen_to_7_percent():
    """港股无涨跌停,分桶上下界是 ±7%(A 股是 ±5%),且首尾桶开放。"""
    labels = [b["label"] for b in _pct_bands([0.0])]
    assert labels[0] == "<-7%"
    assert labels[-1] == ">7%"


def test_pct_bands_treats_input_as_percent_not_ratio():
    """输入是百分数:8.0 表示 +8%,应落进 >7% 桶,而不是被当成 800% 或 0.08%。"""
    bands = {b["label"]: b["count"] for b in _pct_bands([8.0, -9.0, 1.0])}
    assert bands[">7%"] == 1      # 8.0
    assert bands["<-7%"] == 1     # -9.0
    assert bands["0~2%"] == 1     # 1.0


def test_pct_bands_boundaries_are_half_open():
    """边界值归属:[low, high) —— 5.0 进 5~7%,不进 2~5%,避免重复计数。"""
    bands = {b["label"]: b["count"] for b in _pct_bands([5.0, 2.0, 0.0])}
    assert bands["5~7%"] == 1
    assert bands["2~5%"] == 1
    assert bands["0~2%"] == 1
    # 总数守恒:每个样本只落进一个桶
    assert sum(b["count"] for b in _pct_bands([5.0, 2.0, 0.0])) == 3


def test_strong_threshold_is_percent_units():
    """强弱阈值是百分数 5.0(不是 0.05),否则港股会把几乎所有票判成"异动"。"""
    assert STRONG_PCT == 5.0
