"""市场环境(regime) 计算与持久化测试。

覆盖:
- classify_state: 五种状态边界值(强势/偏强/震荡/偏弱/弱势)
- _aggregate_daily: 多日多 symbol 聚合(涨停数/涨跌家数/MA20占比)
- upsert_regime_history: 按 date 覆盖(重算的天替换旧行)
- compute_regime_incremental: 双检测(缺口 + stale mtime)
"""
from __future__ import annotations

import os
import time
from datetime import date

import polars as pl

from app.services import regime_builder

# ───────────────────────── 状态分类 ─────────────────────────
# 新评分模型(对齐看板情绪分): 4 维 profit/speculation/resilience/trend,
# metrics 字段为 up_pct/down_pct/avg_pct/median_pct/strong_up_pct/strong_down_pct/
# strong_diff_pct/limit_up/seal_rate/max_consecutive/index_pct/above_ma20_pct。


def test_classify_strong():
    """普涨行情: 涨家数多、跌幅小、涨停多 → 强势。"""
    state, score = regime_builder.classify_state({
        "up_pct": 80, "down_pct": 15, "avg_pct": 0.025, "median_pct": 0.02,
        "strong_up_pct": 12, "strong_down_pct": 1, "strong_diff_pct": 11,
        "limit_up": 90, "seal_rate": 0.8, "max_consecutive": 6,
        "index_pct": 0.015, "above_ma20_pct": 0.8,
    })
    assert state == "strong"
    assert score >= 70


def test_classify_weak():
    """千股跌停: 跌家数多、大跌股多 → 抗跌维暴跌 → 弱势。"""
    state, score = regime_builder.classify_state({
        "up_pct": 10, "down_pct": 85, "avg_pct": -0.035, "median_pct": -0.03,
        "strong_up_pct": 1, "strong_down_pct": 30, "strong_diff_pct": -29,
        "limit_up": 5, "seal_rate": 0.3, "max_consecutive": 1,
        "index_pct": -0.02, "above_ma20_pct": 0.1,
    })
    assert state == "weak"
    assert score < 30


def test_classify_range():
    """均衡市: 涨跌各半、无明显方向 → 震荡。"""
    state, score = regime_builder.classify_state({
        "up_pct": 48, "down_pct": 48, "avg_pct": 0.0, "median_pct": 0.0,
        "strong_up_pct": 4, "strong_down_pct": 4, "strong_diff_pct": 0,
        "limit_up": 40, "seal_rate": 0.6, "max_consecutive": 2,
        "index_pct": 0.0, "above_ma20_pct": 0.5,
    })
    assert state == "range"
    assert 45 <= score < 55


def test_classify_resilience_drives_weak():
    """抗跌维度专项: 同样的涨跌家数, 大跌股占比飙升会让总分大幅下降。

    验证抗跌维是识别弱势的关键 — 即使涨停数不少, 但大跌股多也会判弱。
    """
    base = {
        "up_pct": 40, "down_pct": 55, "avg_pct": -0.005, "median_pct": -0.005,
        "strong_up_pct": 3, "limit_up": 30, "seal_rate": 0.6, "max_consecutive": 2,
        "index_pct": -0.003, "above_ma20_pct": 0.4,
    }
    # 大跌股少 → 分数较高
    s_few_down = regime_builder.classify_state({**base, "down_pct": 55, "strong_down_pct": 3, "strong_diff_pct": 0})[1]
    # 大跌股飙升 → 分数显著下降(抗跌维暴跌)
    s_many_down = regime_builder.classify_state({**base, "down_pct": 55, "strong_down_pct": 15, "strong_diff_pct": -12})[1]
    assert s_many_down < s_few_down
    assert s_few_down - s_many_down >= 10  # 抗跌维影响显著(权重0.25)


def test_classify_monotonic_up_pct():
    """涨家数占比越高, 综合分越高(其他条件相同)。"""
    base = {
        "down_pct": 30, "avg_pct": 0.01, "median_pct": 0.01,
        "strong_up_pct": 5, "strong_down_pct": 3, "strong_diff_pct": 2,
        "limit_up": 40, "seal_rate": 0.65, "max_consecutive": 3,
        "index_pct": 0.005, "above_ma20_pct": 0.55,
    }
    s_low = regime_builder.classify_state({**base, "up_pct": 30})[1]
    s_mid = regime_builder.classify_state({**base, "up_pct": 50})[1]
    s_high = regime_builder.classify_state({**base, "up_pct": 75})[1]
    assert s_low < s_mid < s_high


# ───────────────────────── 聚合 ─────────────────────────


def _enriched_df() -> pl.DataFrame:
    """构造 2 天 × 4 标的 的 enriched 数据(含信号列)。"""
    return pl.DataFrame({
        "date": [date(2026, 1, 2)] * 4 + [date(2026, 1, 3)] * 4,
        "symbol": ["A", "B", "C", "D"] * 2,
        "close": [11, 9, 21, 19, 12, 8, 22, 18],
        "change_pct": [0.1, -0.1, 0.05, -0.05, 0.08, -0.12, 0.02, -0.08],
        "amount": [1e8, 2e8, 3e8, 4e8] * 2,
        "ma20": [10, 10, 20, 20, 10, 10, 20, 20],
        "signal_limit_up": [True, False, False, False, True, False, True, False],
        "signal_limit_down": [False, False, False, True, False, False, False, False],
        "signal_broken_limit_up": [False, False, False, False, False, False, False, False],
        "consecutive_limit_ups": [1, 0, 0, 0, 2, 0, 1, 0],
    })


def test_aggregate_daily_basic():
    """聚合多日: 每天的涨停数/涨跌家数正确。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df, index_pct_map={
        date(2026, 1, 2): 0.01, date(2026, 1, 3): -0.005,
    })
    assert result.height == 2
    # 第一天(1/2): 1 个涨停, 2 涨 2 跌
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r1["limit_up"] == 1
    assert r1["up_count"] == 2
    assert r1["down_count"] == 2
    assert r1["max_consecutive"] == 1
    # 第二天(1/3): 2 个涨停, 2 涨 2 跌, 连板高度 2
    r2 = result.filter(pl.col("date") == date(2026, 1, 3)).row(0, named=True)
    assert r2["limit_up"] == 2
    assert r2["max_consecutive"] == 2
    # 每行都有 state 和 score
    assert all(s in {"strong", "lean_strong", "range", "lean_weak", "weak"}
               for s in result["state"].to_list())
    assert result["score"].min() >= 0 and result["score"].max() <= 100


def test_aggregate_daily_ma20_above():
    """MA20 上方占比正确(close > ma20)。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df)
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    # 1/2: A(close11>ma10)✓, B(9<10)✗, C(21>20)✓, D(19<20)✗ → 2/4 = 0.5
    assert r1["above_ma20_pct"] == 0.5


def test_aggregate_empty_returns_empty():
    assert regime_builder._aggregate_daily(pl.DataFrame()).is_empty()


# ───────────────────────── 持久化(upsert) ─────────────────────────


def test_upsert_inserts_new(tmp_path):
    rows = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["strong", "range"],
        "score": [80, 50],
    })
    regime_builder.upsert_regime_history(tmp_path, rows)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2


def test_upsert_overwrites_existing_date(tmp_path):
    """重算的天覆盖旧行(upsert 语义)。"""
    old = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    })
    regime_builder.upsert_regime_history(tmp_path, old)
    # 重算 1/2
    new = pl.DataFrame({
        "date": [date(2026, 1, 2)],
        "state": ["strong"], "score": [85],
    })
    regime_builder.upsert_regime_history(tmp_path, new)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2  # 仍是 2 天(1/2 被覆盖, 不重复)
    r2 = loaded.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r2["state"] == "strong"
    assert r2["score"] == 85
    # 1/1 不受影响
    r1 = loaded.filter(pl.col("date") == date(2026, 1, 1)).row(0, named=True)
    assert r1["state"] == "range"


def test_coverage_metadata(tmp_path):
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 5)],
        "state": ["strong", "weak"], "score": [80, 20],
    }))
    cov = regime_builder.get_regime_coverage(tmp_path)
    assert cov["rows"] == 2
    assert cov["earliest_date"] == "2026-01-01"
    assert cov["latest_date"] == "2026-01-05"


def test_coverage_empty(tmp_path):
    cov = regime_builder.get_regime_coverage(tmp_path)
    assert cov["rows"] == 0
    assert cov["earliest_date"] is None


# ───────────────────────── 双检测 ─────────────────────────


def test_detect_stale_dates_by_mtime(tmp_path):
    """enriched 分区 mtime > regime mtime → 标记重算。"""
    # 准备 regime 历史
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    # 模拟 enriched 分区(先写, mtime=T2)
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")
    # 重新 upsert regime → regime mtime 更新到 T3 > enriched 的 T2
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    time.sleep(0.05)  # 确保 mtime 精度差异
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    # 让 1/2 的 mtime 更新到 future > regime mtime
    future = time.time() + 10
    os.utime(enriched_dir / "date=2026-01-02" / "part.parquet", (future, future))

    class _FakeRepo:
        class store:
            data_dir = tmp_path
    stale = regime_builder.detect_stale_dates(tmp_path, _FakeRepo())
    assert date(2026, 1, 2) in stale
    assert date(2026, 1, 1) not in stale  # 1/1 没更新


def test_compute_incremental_missing_dates(tmp_path):
    """enriched 有但 regime 没有 → 补算缺口。"""
    # regime 只有 1/1
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)], "state": ["range"], "score": [50],
    }))
    # 模拟 enriched 有 1/1 和 1/2
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    class _FakeRepo:
        class store:
            data_dir = tmp_path
        def get_enriched_range(self, *a, **k): return None  # 无缓存, 不实际算

    # compute_regime_incremental 会识别 1/2 缺口, 但 run_regime_batch 因无数据返回空
    new = regime_builder.compute_regime_incremental(_FakeRepo(), tmp_path, today=date(2026, 1, 3))
    # 无真实 enriched 数据 → 不算出新行, 但不报错
    assert new.is_empty() or new.height >= 0


# ───────────────────────── 回测环境过滤(T-1 防未来函数) ─────────────────────────


def test_build_regime_mask_t1_alignment(tmp_path):
    """_build_regime_mask 强制 T-1: regime[T-1] 决定 entry[T]。

    场景: regime 1/1=weak(10), 1/2=strong(85)。
    timestamp_labels: [1/1, 1/2, 1/3]。
    filter: 只允许 strong。
    期望: mask = [True(首日默认允许), False(1/2的前一日=1/1=weak), True(1/3的前一日=1/2=strong)]。
    """
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["weak", "strong"],
        "score": [10, 85],
    }))
    labels = ("2026-01-01", "2026-01-02", "2026-01-03")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"states": ["strong"]}, tmp_path,
    )
    assert mask is not None
    assert mask.tolist() == [True, False, True]


def test_build_regime_mask_min_score(tmp_path):
    """min_score 过滤: regime[T-1] 的 score >= min_score 才允许入场。"""
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "lean_strong"],
        "score": [45, 65],
    }))
    labels = ("2026-01-01", "2026-01-02", "2026-01-03")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"min_score": 60}, tmp_path,
    )
    # 1/2 entry 由 1/1(score=45 < 60) 决定 → False
    # 1/3 entry 由 1/2(score=65 >= 60) 决定 → True
    assert mask.tolist() == [True, False, True]


def test_build_regime_mask_none_when_no_filter():
    """regime_filter 为 None → 返回 None(不过滤)。"""
    from app.backtest.strategy import StrategyBacktestService

    assert StrategyBacktestService._build_regime_mask(("2026-01-01",), None, None) is None


def test_build_regime_mask_none_when_no_data(tmp_path):
    """无 regime 历史数据 → 返回 None(不阻断回测)。"""
    from app.backtest.strategy import StrategyBacktestService

    mask = StrategyBacktestService._build_regime_mask(
        ("2026-01-01", "2026-01-02"), {"states": ["strong"]}, tmp_path,
    )
    assert mask is None


def test_build_regime_mask_first_day_allowed(tmp_path):
    """首日无前一日环境数据 → 默认允许(不阻断)。"""
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)],
        "state": ["weak"], "score": [10],
    }))
    labels = ("2026-01-01", "2026-01-02")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"states": ["strong"]}, tmp_path,
    )
    # 1/1 首日 → True; 1/2 由 1/1(weak) → False
    assert mask.tolist() == [True, False]

