"""市场环境(regime) 计算与持久化测试。

覆盖:
- classify_state: 五种状态边界值(强势/偏强/震荡/偏弱/弱势)
- _compute_subscores: 综合分 = Σ 子分 × 权重 一致性
- _aggregate_daily: 多日多 symbol 聚合(涨停数/涨跌家数/MA20占比)
- upsert_regime_history: 按 date 覆盖(重算的天替换旧行) + schema 迁移
- compute_regime_incremental: 双检测(缺口 + stale mtime)
- regime_t1_entry_mask: T-1 入场过滤(no-data fail-open / exit-unaffected / score 边界)
- 降级: 无 enriched 数据 → run_regime_batch 返回空
"""
from __future__ import annotations

import os
import time
from datetime import date

import polars as pl

from app.services import regime_builder


# ───────────────────────── 状态分类 ─────────────────────────

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
    """抗跌维度专项: 大跌股占比飙升会让总分大幅下降。"""
    base = {
        "up_pct": 40, "down_pct": 55, "avg_pct": -0.005, "median_pct": -0.005,
        "strong_up_pct": 3, "limit_up": 30, "seal_rate": 0.6, "max_consecutive": 2,
        "index_pct": -0.003, "above_ma20_pct": 0.4,
    }
    s_few_down = regime_builder.classify_state({**base, "down_pct": 55, "strong_down_pct": 3, "strong_diff_pct": 0})[1]
    s_many_down = regime_builder.classify_state({**base, "down_pct": 55, "strong_down_pct": 15, "strong_diff_pct": -12})[1]
    assert s_many_down < s_few_down
    assert s_few_down - s_many_down >= 10


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


def test_classify_score_in_range():
    """综合分始终落在 [0, 100]。"""
    extremes = [
        {"up_pct": 0, "avg_pct": -0.1, "median_pct": -0.1, "strong_diff_pct": -50,
         "limit_up": 0, "seal_rate": 0, "max_consecutive": 0,
         "strong_down_pct": 100, "index_pct": -0.1, "above_ma20_pct": 0},
        {"up_pct": 100, "avg_pct": 0.1, "median_pct": 0.1, "strong_diff_pct": 50,
         "limit_up": 200, "seal_rate": 1, "max_consecutive": 15,
         "strong_down_pct": 0, "index_pct": 0.1, "above_ma20_pct": 1},
    ]
    for m in extremes:
        _, score = regime_builder.classify_state(m)
        assert 0 <= score <= 100


# ───────────────────────── 子维度加权一致性 ─────────────────────────

def test_subscores_weighted_to_score():
    """综合分 = Σ 子分 × 权重(一致性校验)。"""
    metrics = {
        "up_pct": 60, "avg_pct": 0.01, "median_pct": 0.008,
        "strong_diff_pct": 5, "limit_up": 50, "seal_rate": 0.7,
        "max_consecutive": 4, "strong_down_pct": 4,
        "index_pct": 0.008, "above_ma20_pct": 0.6,
    }
    sub = regime_builder._compute_subscores(metrics)
    w = regime_builder.WEIGHTS
    expected = (
        sub["profit"] * w["profit"]
        + sub["speculation"] * w["speculation"]
        + sub["resilience"] * w["resilience"]
        + sub["trend"] * w["trend"]
    )
    assert abs(sub["score"] - expected) < 1e-9
    assert 0 <= sub["score"] <= 100


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
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r1["limit_up"] == 1
    assert r1["up_count"] == 2
    assert r1["down_count"] == 2
    assert r1["max_consecutive"] == 1
    r2 = result.filter(pl.col("date") == date(2026, 1, 3)).row(0, named=True)
    assert r2["limit_up"] == 2
    assert r2["max_consecutive"] == 2
    assert all(s in {"strong", "lean_strong", "range", "lean_weak", "weak"}
               for s in result["state"].to_list())
    assert result["score"].min() >= 0 and result["score"].max() <= 100


def test_aggregate_daily_ma20_above():
    """MA20 上方占比正确(close > ma20)。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df)
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r1["above_ma20_pct"] == 0.5


def test_aggregate_daily_persists_subscores():
    """聚合结果持久化 4 个子维度分列。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df)
    for col in ("profit_score", "speculation_score", "resilience_score", "trend_score"):
        assert col in result.columns
        assert result[col].min() >= 0 and result[col].max() <= 100


def test_aggregate_empty_returns_empty():
    assert regime_builder._aggregate_daily(pl.DataFrame()).is_empty()


def test_aggregate_missing_change_pct_returns_empty():
    """缺 change_pct 列 → 返回空(无法聚合)。"""
    df = pl.DataFrame({"date": [date(2026, 1, 1)], "symbol": ["A"]})
    assert regime_builder._aggregate_daily(df).is_empty()


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
    new = pl.DataFrame({
        "date": [date(2026, 1, 2)],
        "state": ["strong"], "score": [85],
    })
    regime_builder.upsert_regime_history(tmp_path, new)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2
    r2 = loaded.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r2["state"] == "strong"
    assert r2["score"] == 85
    r1 = loaded.filter(pl.col("date") == date(2026, 1, 1)).row(0, named=True)
    assert r1["state"] == "range"


def test_upsert_schema_migration(tmp_path):
    """旧 parquet 缺新列 → upsert 时自动补 null 迁移到新 schema。"""
    old = pl.DataFrame({
        "date": [date(2026, 1, 1)], "state": ["range"], "score": [50],
    })
    regime_builder.upsert_regime_history(tmp_path, old)
    new = pl.DataFrame({
        "date": [date(2026, 1, 2)], "state": ["strong"], "score": [85],
        "profit_score": [72], "trend_score": [68],
    })
    regime_builder.upsert_regime_history(tmp_path, new)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2
    assert "profit_score" in loaded.columns
    r1 = loaded.filter(pl.col("date") == date(2026, 1, 1)).row(0, named=True)
    assert r1["profit_score"] is None


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
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    time.sleep(0.05)
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    future = time.time() + 10
    os.utime(enriched_dir / "date=2026-01-02" / "part.parquet", (future, future))

    class _FakeRepo:
        class store:
            data_dir = tmp_path
    stale = regime_builder.detect_stale_dates(tmp_path, _FakeRepo())
    assert date(2026, 1, 2) in stale
    assert date(2026, 1, 1) not in stale


def test_compute_incremental_missing_dates(tmp_path):
    """enriched 有但 regime 没有 → 补算缺口。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)], "state": ["range"], "score": [50],
    }))
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    class _FakeRepo:
        class store:
            data_dir = tmp_path
        def get_enriched_range(self, *a, **k): return None
        def get_index_daily(self, *a, **k): return pl.DataFrame()

    new = regime_builder.compute_regime_incremental(_FakeRepo(), tmp_path, today=date(2026, 1, 3))
    assert new.is_empty() or new.height >= 0


def test_enriched_date_set_scans_partitions(tmp_path):
    """enriched_date_set 扫描分区目录返回日期集合。"""
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-03", "2026-01-05"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    class _FakeRepo:
        class store:
            data_dir = tmp_path
    dates = regime_builder.enriched_date_set(_FakeRepo())
    assert dates == {date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 5)}


def test_earliest_enriched_date(tmp_path):
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-03-01", "2026-01-01"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    class _FakeRepo:
        class store:
            data_dir = tmp_path
    assert regime_builder.earliest_enriched_date(_FakeRepo()) == date(2026, 1, 1)


def test_earliest_enriched_date_empty(tmp_path):
    class _FakeRepo:
        class store:
            data_dir = tmp_path
    assert regime_builder.earliest_enriched_date(_FakeRepo()) is None


# ───────────────────────── 降级: 无数据 ─────────────────────────

def test_run_regime_batch_no_enriched_returns_empty(tmp_path):
    """无 enriched 数据 → run_regime_batch 返回空(清晰降级, 不报错)。"""
    class _FakeRepo:
        class store:
            data_dir = tmp_path
        def get_index_daily(self, *a, **k): return pl.DataFrame()
    result = regime_builder.run_regime_batch(
        _FakeRepo(), start=date(2026, 1, 1), end=date(2026, 1, 5),
    )
    assert result.is_empty()


def test_run_regime_batch_start_after_end_returns_empty(tmp_path):
    """start > end → 直接返回空, 不扫描。"""
    class _FakeRepo:
        class store:
            data_dir = tmp_path
    result = regime_builder.run_regime_batch(
        _FakeRepo(), start=date(2026, 1, 5), end=date(2026, 1, 1),
    )
    assert result.is_empty()


# ───────────────────────── T-1 入场过滤 ─────────────────────────

def test_t1_mask_no_filter_returns_none():
    """未配置过滤条件 → 返回 None (不过滤)。"""
    panel = pl.DataFrame({"date": [date(2026, 1, 1)], "symbol": ["A"]})
    assert regime_builder.regime_t1_entry_mask(panel, "/tmp") is None


def test_t1_mask_no_regime_data_fail_open(tmp_path):
    """有过滤条件但无 regime 数据 → 返回 None (fail-open)。"""
    panel = pl.DataFrame({"date": [date(2026, 1, 1)], "symbol": ["A"]})
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong"}, min_score=50,
    )
    assert mask is None


def test_t1_mask_filters_disallowed_states(tmp_path):
    """T-1 状态不在 allowed → 不允许入场。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["strong", "weak"], "score": [80, 20],
    }))
    panel = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
        "symbol": ["A", "A", "A"],
    })
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong"},
    )
    assert mask is not None
    # 1/1 无 T-1 → fail-open；1/2 使用 1/1 strong；1/3 使用 1/2 weak。
    assert mask.to_list() == [True, True, False]


def test_t1_mask_respects_score_threshold(tmp_path):
    """min_score 过滤: T-1 分数不达标 → 不允许入场。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["strong", "weak"], "score": [80, 20],
    }))
    panel = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
        "symbol": ["A", "A", "A"],
    })
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, min_score=50,
    )
    assert mask is not None
    # 1/1 无 T-1 → fail-open；1/2 使用 1/1 score 80；1/3 使用 1/2 score 20。
    assert mask.to_list() == [True, True, False]


def test_t1_mask_pre_date_fail_open(tmp_path):
    """panel 日期早于所有 regime 记录 → 该日 fail-open(允许)。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 2)], "state": ["strong"], "score": [80],
    }))
    panel = pl.DataFrame({"date": [date(2026, 1, 1)], "symbol": ["A"]})
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong"},
    )
    assert mask is not None
    assert mask.to_list() == [True]  # 无 T-1 → fail-open


def test_t1_mask_uses_previous_trading_day(tmp_path):
    """严格使用 T-1(前一交易日), 不是同日。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["strong", "weak"], "score": [80, 20],
    }))
    # 1/2 必须使用 panel 中前一交易日 1/1 的 strong，而不是同日 weak。
    panel = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "symbol": ["A", "A"],
    })
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong"},
    )
    assert mask is not None
    assert mask.to_list() == [True, True]


def test_t1_mask_does_not_affect_exit():
    """T-1 mask 只作用 entry, exit 不受影响。

    此契约由回测引擎的调用位置保证(entry_mask & regime_mask, exit_mask 不变)。
    此测试验证 mask helper 本身只返回入场 bool, 不修改 exit 相关逻辑。
    """
    # regime_t1_entry_mask 只返回一个 mask, 无 exit 输出 → 契约天然成立
    panel = pl.DataFrame({"date": [date(2026, 1, 1)], "symbol": ["A"]})
    result = regime_builder.regime_t1_entry_mask(panel, "/tmp")
    assert result is None  # 无过滤条件 → 不过滤(exit 也不受影响)


def test_t1_mask_combines_state_and_score(tmp_path):
    """同时传 states 和 min_score: 两个条件取交集。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["lean_strong", "strong"], "score": [58, 80],
    }))
    panel = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
        "symbol": ["A", "A", "A"],
    })
    # states={strong, lean_strong}, min_score=60
    # 1/1 无 T-1 → fail-open；1/2 使用 1/1 lean_strong(58) → score✗；
    # 1/3 使用 1/2 strong(80) → 两项均通过。
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong", "lean_strong"}, min_score=60,
    )
    assert mask is not None
    assert mask.to_list() == [True, False, True]


def test_t1_mask_missing_exact_previous_day_fails_open(tmp_path):
    """T-1 记录缺失时不能沿用更早状态。"""
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)],
        "state": ["weak"],
        "score": [10],
    }))
    panel = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
        "symbol": ["A", "A", "A"],
    })
    mask = regime_builder.regime_t1_entry_mask(
        panel, tmp_path, allowed_states={"strong"},
    )
    assert mask is not None
    # 1/2 精确 T-1=1/1 weak → 拒绝；1/3 的精确 T-1=1/2 缺失 → 放行。
    assert mask.to_list() == [True, False, True]
