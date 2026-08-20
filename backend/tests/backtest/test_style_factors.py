"""style_factors 单元测试 — 本地风格因子构建与归因 (A5)。

合成数据全部使用固定 seed; 面板注入已知结构:
* 小盘组日收益系统性 +0.1% → SMB ≈ +0.001
* 动量赢家组日收益系统性 +0.15% → UMD ≈ +0.0015
* 未注入波动结构 → LMV ≈ 0
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.metrics import MetricContext
from app.backtest.style_factors import (
    FACTOR_VERSION,
    build_style_factor_returns,
    style_attribution,
)

START = date(2023, 1, 2)
CTX_DAILY = MetricContext("daily")

# 股本按规模组相差 100x, 价格漂移 (~x2.2) 不会让市值跨越组界
_GROUP_SHARES = (1e6, 1e8, 1e10)
_PANEL_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
    "float_shares": pl.Float64,
    "total_shares": pl.Float64,
}
def _panel(
    n_symbols: int = 150,
    n_days: int = 320,
    seed: int = 42,
    noise_sd: float = 0.01,
    late_from: int | None = None,
    late_count: int = 0,
) -> pl.DataFrame:
    """合成面板: index//50 定规模组 (0=小盘), index%2==0 为动量赢家。"""
    rng = np.random.default_rng(seed)
    n_per_group = n_symbols // 3
    rows: list[dict] = []
    for i in range(n_symbols):
        size_group = min(i // n_per_group, 2)
        drift = 0.001 if size_group == 0 else 0.0
        if i % 2 == 0:
            drift += 0.0015
        rets = rng.normal(drift, noise_sd, size=n_days)
        closes = 10.0 * np.cumprod(1.0 + rets)
        shares = float(_GROUP_SHARES[size_group])
        start_idx = late_from if (late_from is not None and i >= n_symbols - late_count) else 0
        for t in range(start_idx, n_days):
            rows.append({
                "symbol": f"S{i:03d}",
                "date": START + timedelta(days=t),
                "close": float(closes[t]),
                "float_shares": shares,
                "total_shares": None,
            })
    return pl.DataFrame(rows, schema=_PANEL_SCHEMA)


def _trio_panel(with_dead: bool, n_days: int = 130) -> pl.DataFrame:
    """3 只股票小面板 (噪声调小以收紧断言):

    A 小盘 (float 1e6, +0.1%/日) / B 大盘 (float 1e8) /
    C float 全缺 → 回退 total_shares (1e8);
    with_dead=True 时追加 D (float/total 均缺, 巨大漂移) — 若误入截面会显著改变 SMB。
    """
    rng = np.random.default_rng(7)
    specs = [("A", 1e6, 0.001), ("B", 1e8, 0.0), ("C", None, 0.0)]
    rows: list[dict] = []
    for sym, shares, drift in specs:
        rets = rng.normal(drift, 0.001, size=n_days)
        closes = 10.0 * np.cumprod(1.0 + rets)
        for t in range(n_days):
            rows.append({
                "symbol": sym,
                "date": START + timedelta(days=t),
                "close": float(closes[t]),
                "float_shares": shares,
                "total_shares": None if shares is not None else 1e8,
            })
    if with_dead:
        rets = rng.normal(0.5, 0.002, size=n_days)
        closes = 10.0 * np.cumprod(1.0 + rets)
        for t in range(n_days):
            rows.append({
                "symbol": "D",
                "date": START + timedelta(days=t),
                "close": float(closes[t]),
                "float_shares": None,
                "total_shares": None,
            })
    return pl.DataFrame(rows, schema=_PANEL_SCHEMA)


# ---------------------------------------------------------------------------
# build_style_factor_returns
# ---------------------------------------------------------------------------


def test_build_recovers_injected_size_and_momentum_structure():
    df, meta = build_style_factor_returns(_panel())

    assert df is not None
    assert set(df.columns) == {"date", "smb", "umd", "lmv"}
    assert meta["min_cross_section"] == 100
    assert meta["valid_days"] == 319      # 首日无 ret → 跳过
    assert meta["skipped_days"] == 1
    assert meta["median_cross_section"] == 150.0

    # SMB: 小盘 +0.1%/日 (小盘组内赢家/非赢家各半, 与大盘组对冲后净漂移 0.001)
    assert df["smb"].null_count() == 0
    smb_mean = df["smb"].mean()
    assert smb_mean is not None and 0.0007 < smb_mean < 0.0013

    # UMD: mom_252_21 需 253 个观测 → 320-252=68 日; 赢家 +0.15%/日。
    # 最高动量组富集小盘赢家 (漂移 0.0025 = 规模 0.001 + 动量 0.0015),
    # 故 UMD 略高于裸注入的 0.0015。
    umd = df.filter(pl.col("umd").is_not_null())
    assert umd.height == 68
    umd_mean = umd["umd"].mean()
    assert umd_mean is not None and 0.0015 < umd_mean < 0.0025

    # LMV: 未注入波动结构 → ≈ 0; vol_60 需 60 个 ret → 260 日
    lmv = df.filter(pl.col("lmv").is_not_null())
    assert lmv.height == 260
    lmv_mean = lmv["lmv"].mean()
    assert lmv_mean is not None and abs(lmv_mean) < 0.0006


def test_build_skips_thin_cross_section_days():
    # 后 60 只自第 150 日起上市: 此前每日截面 90 < 100 → 跳过;
    # 上市首日无 ret → 仍 90 → 跳过; 第 151 日起 150 只 → 有效
    df, meta = build_style_factor_returns(_panel(late_from=150, late_count=60))

    assert df is not None
    assert meta["valid_days"] == 169
    assert meta["skipped_days"] == 320 - 169
    assert df.height == 169


def test_build_returns_none_below_120_valid_days():
    df, meta = build_style_factor_returns(_panel(n_days=100))
    assert df is None
    assert meta["valid_days"] == 99
    assert meta["reason"] == "valid_days<120"


def test_build_boundary_exactly_120_valid_days():
    df, meta = build_style_factor_returns(_panel(n_days=121))
    assert df is not None
    assert df.height == 120


def test_shares_fallback_and_full_exclusion():
    base = _trio_panel(with_dead=False)
    with_dead = _trio_panel(with_dead=True)

    df_base, meta_base = build_style_factor_returns(base, min_cross_section=3)
    df_dead, meta_dead = build_style_factor_returns(with_dead, min_cross_section=3)

    # C 靠 total_shares 回退进入截面 (若被剔除则每日截面只有 2 → 无有效日)
    assert df_base is not None
    assert meta_base["valid_days"] == 129
    # D (float/total 均缺) 永不入截面 → 两个面板输出完全一致
    assert df_dead is not None
    assert df_base.equals(df_dead)
    assert meta_dead["valid_days"] == meta_base["valid_days"]
    assert meta_dead["skipped_days"] == meta_base["skipped_days"]

    # 规格指纹随实际 min_cross_section 变化
    expected = hashlib.sha256(b"v1|smb,umd,lmv|tercile|cs>=3").hexdigest()[:12]
    assert meta_base["factor_version"] == expected

    # SMB = 小盘 A − 大盘 (B/C), 注入 0.001
    smb_mean = df_base["smb"].mean()
    assert smb_mean is not None and 0.0006 < smb_mean < 0.0014


def test_build_fail_closed_on_missing_columns_empty_and_no_shares():
    df, meta = build_style_factor_returns(pl.DataFrame({"symbol": ["A"]}))
    assert df is None and "missing_columns" in meta["reason"]

    df2, meta2 = build_style_factor_returns(pl.DataFrame())
    assert df2 is None and meta2["reason"] == "empty_panel"

    # 完全没有股本列 → 全部行剔除 → 无有效截面
    bare = _trio_panel(with_dead=False).select("date", "symbol", "close")
    df3, meta3 = build_style_factor_returns(bare, min_cross_section=3)
    assert df3 is None
    assert meta3["valid_days"] == 0
    assert meta3["reason"] == "no_valid_cross_section"


# ---------------------------------------------------------------------------
# style_attribution
# ---------------------------------------------------------------------------


def _synthetic_factors(n: int, seed: int):
    rng = np.random.default_rng(seed)
    dates = [START + timedelta(days=i) for i in range(n)]
    return (
        dates,
        rng.normal(0.0, 0.002, size=n),   # smb
        rng.normal(0.0, 0.0015, size=n),  # umd
        rng.normal(0.0, 0.001, size=n),   # lmv
    )


def test_attribution_recovers_known_loadings():
    n = 200
    dates, smb, umd, lmv = _synthetic_factors(n, seed=5)
    noise = np.random.default_rng(123).normal(0.0, 0.0002, size=n)
    strat = 1.0 * smb + noise
    factor_df = pl.DataFrame({"date": dates, "smb": smb, "umd": umd, "lmv": lmv})

    res = style_attribution(strat, dates, factor_df, CTX_DAILY)
    assert res is not None
    assert res["n_obs"] == n
    # 策略 = smb + 独立噪声 → beta_smb ≈ 1, 其余 ≈ 0
    assert 0.95 < res["betas"]["smb"] < 1.05
    assert abs(res["betas"]["umd"]) < 0.1
    assert abs(res["betas"]["lmv"]) < 0.1
    assert res["t_stats"]["smb"] > 10
    assert abs(res["t_stats"]["umd"]) < 3
    assert abs(res["t_stats"]["lmv"]) < 3
    assert abs(res["t_stats"]["alpha"]) < 3
    assert abs(res["alpha_per_period"]) < 0.0001
    assert res["r_squared"] is not None and res["r_squared"] > 0.85
    assert set(res["t_stats"]) == {"alpha", "smb", "umd", "lmv"}
    assert set(res["betas"]) == {"smb", "umd", "lmv"}

    # 年化只走 MetricContext (与 relative_performance_metrics 口径一致)
    assert math.isclose(res["alpha_annualized"], res["alpha_per_period"] * 252, rel_tol=1e-9)
    res_weekly = style_attribution(strat, dates, factor_df, MetricContext("weekly"))
    assert res_weekly is not None
    assert math.isclose(res_weekly["alpha_annualized"], res["alpha_per_period"] * 52, rel_tol=1e-9)

    # 规格指纹
    expected = hashlib.sha256(b"v1|smb,umd,lmv|tercile|cs>=100").hexdigest()[:12]
    assert res["factor_version"] == expected == FACTOR_VERSION


def test_attribution_insufficient_obs_returns_none_and_boundary_ok():
    dates, smb, umd, lmv = _synthetic_factors(200, seed=6)

    # 119 个对齐观测 < 120 → None
    short_dates = dates[:119]
    strat = smb[:119]
    factor_df = pl.DataFrame({"date": dates, "smb": smb, "umd": umd, "lmv": lmv})
    assert style_attribution(strat, short_dates, factor_df, CTX_DAILY) is None

    # 恰好 120 → 可回归
    res = style_attribution(smb[:120], dates[:120], factor_df, CTX_DAILY)
    assert res is not None and res["n_obs"] == 120


def test_attribution_aligns_by_date_and_drops_uncovered():
    dates, smb, umd, lmv = _synthetic_factors(200, seed=8)
    factor_df = pl.DataFrame({"date": dates, "smb": smb, "umd": umd, "lmv": lmv})

    # 策略侧只覆盖日期 40..189, 外加一个因子范围外的日期 → 交集 150
    strat_dates = dates[40:190] + [date(2030, 1, 1)]
    strat = np.concatenate([smb[40:190], [0.01]])
    res = style_attribution(strat, strat_dates, factor_df, CTX_DAILY)
    assert res is not None
    assert res["n_obs"] == 150
    assert 0.95 < res["betas"]["smb"] < 1.05


def test_attribution_collinear_factors_return_none():
    dates, smb, umd, lmv = _synthetic_factors(200, seed=9)
    factor_df = pl.DataFrame({"date": dates, "smb": smb, "umd": smb.copy(), "lmv": lmv})
    assert style_attribution(smb, dates, factor_df, CTX_DAILY) is None


def test_attribution_fail_closed_on_missing_inputs():
    rets = np.full(150, 0.01)
    assert style_attribution(rets, [START] * 150, pl.DataFrame(), CTX_DAILY) is None
    bad = pl.DataFrame({"date": [START] * 150, "smb": [0.0] * 150})
    assert style_attribution(rets, [START] * 150, bad, CTX_DAILY) is None


# ---------------------------------------------------------------------------
# 集成: 面板 → 因子 → 归因
# ---------------------------------------------------------------------------


def test_end_to_end_panel_factors_attribution():
    # 400 日面板: 三因子齐全 = 400-252 = 148 日 ≥ 120 → 可归因
    df, meta = build_style_factor_returns(_panel(n_days=400))
    assert df is not None
    complete = df.filter(
        pl.col("smb").is_not_null() & pl.col("umd").is_not_null() & pl.col("lmv").is_not_null()
    )
    assert complete.height == 148

    strat = complete["smb"].to_numpy() + np.random.default_rng(11).normal(0.0, 0.0005, size=148)
    res = style_attribution(strat, complete["date"].to_list(), df, CTX_DAILY)
    assert res is not None
    assert res["n_obs"] == 148
    assert 0.85 < res["betas"]["smb"] < 1.15
    assert res["r_squared"] is not None and res["r_squared"] > 0.8
    assert meta["valid_days"] == 399


def test_end_to_end_short_factor_history_returns_none():
    # 320 日面板: 三因子齐全仅 68 日 < 120 → 归因 fail-closed
    df, _ = build_style_factor_returns(_panel(n_days=320))
    assert df is not None
    complete = df.filter(
        pl.col("smb").is_not_null() & pl.col("umd").is_not_null() & pl.col("lmv").is_not_null()
    )
    assert complete.height == 68
    strat = complete["smb"].to_numpy() + np.random.default_rng(3).normal(0.0, 0.0005, size=68)
    assert style_attribution(strat, complete["date"].to_list(), df, CTX_DAILY) is None
