"""非有限输入不得中断 regime 计算 —— 2026-09-15「regime 停更」事故的回归测试。

事故链: 停牌股在 2026-09-14 被补零(open/high/low/close 全 0)
    ⇒ 09-15 的 `change_pct = close / prev_close - 1` = **inf**
    ⇒ 当日 `avg_pct`(均值)被**单个 inf** 污染成 inf
    ⇒ `regime_builder._score` 里 `round(inf)` 抛 OverflowError
      (`round` 在 `min`/`max` 钳制**之前**求值, 钳制救不了它)
    ⇒ `compute_regime` 整段中止、`regime_history` 停更。

修法: `_score` 对非有限输入取**中性 50**(不崩、也不伪造看多/看空)。

⚠️ 移植边界: 本件只含 `_score` 防护。本机同批修复里「上游 `change_pct` 对非正前收取 **NULL**」
(`ratio_over_positive`, app/indicators/pipeline.py) **未**随之移植 ⇒ 本件只堵「坏值已产生后不崩」,
不堵「坏值的产生」；对应的 tests/test_pipeline_price_ratio.py 亦未移植。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import regime_builder


def test_score_non_finite_returns_neutral():
    """inf/-inf/nan 无法映射到 [0,100]: 取中性 50, 既不抛异常也不伪造方向。"""
    for value in (float("inf"), float("-inf"), float("nan")):
        assert regime_builder._score(value, -1.2, 1.3) == 50.0


def test_classify_state_tolerates_non_finite_metric():
    """任一 metric 变成 inf 时, 分类仍须给出合法状态与 0-100 分, 不得抛异常。"""
    state, score = regime_builder.classify_state({
        "up_pct": 50, "down_pct": 50, "avg_pct": float("inf"), "median_pct": 0.0,
        "strong_up_pct": 5, "strong_down_pct": 5, "strong_diff_pct": 0,
        "limit_up": 40, "seal_rate": 0.7, "max_consecutive": 5,
        "index_pct": 0.0, "above_ma20_pct": 0.5,
    })
    assert state in regime_builder.STATE_LABELS
    assert 0 <= score <= 100


def test_aggregate_daily_tolerates_non_finite_change_pct():
    """端到端复现: 池内含 change_pct=inf 的一行时, 按日聚合仍须产出该日结果。"""
    df = pl.DataFrame({
        "date": [date(2026, 9, 15)] * 3,
        "symbol": ["000016.SZ", "600000.SH", "600001.SH"],
        "change_pct": [float("inf"), 0.02, -0.01],
        "close": [0.0, 11.0, 9.0],
        "ma20": [0.0, 10.5, 9.5],
        "amount": [0.0, 2.0e8, 1.0e8],
        "signal_limit_up": [False, True, False],
        "signal_limit_down": [False, False, False],
        "signal_broken_limit_up": [False, False, False],
        "consecutive_limit_ups": [0, 1, 0],
    })
    out = regime_builder._aggregate_daily(df)
    assert out.height == 1
    assert 0 <= out["score"][0] <= 100
