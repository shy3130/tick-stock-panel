"""``factor_ops`` 向量化算子库测试。

每个算子对照 numpy/pandas 参考实现（忠实复刻
``Vibe-Trading/agent/src/factors/base.py`` 的语义）逐项校验：warmup→null、
NaN 传播、ties、常量窗口、因果对齐。

注意：``rank`` / ``scale`` 是横截面算子（按 ``date`` 分组），其余 ``ts_*`` /
``decay_linear`` / ``signed_power`` / ``ts_regression`` 是时序算子，需收尾
``.over("symbol")``。
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.backtest import factor_ops as ops


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
def _panel(n_days: int = 12, symbols: tuple[str, ...] = ("A", "B", "C")) -> pl.DataFrame:
    """可复现的长表 panel：symbol × date × (open/close/...)。

    用固定种子生成有正有负、含重复值（触发 ties）的数值，并在固定位置注入
    null 以校验 NaN 传播。
    """
    rng = np.random.default_rng(20260814)
    rows = []
    for sym in symbols:
        base = rng.uniform(5.0, 50.0)
        for d in range(n_days):
            close = round(float(base + rng.normal(0, 1.5) * (d + 1) * 0.05), 4)
            opn = round(close + rng.normal(0, 0.3), 4)
            high = round(max(opn, close) + abs(rng.normal(0, 0.4)), 4)
            low = round(min(opn, close) - abs(rng.normal(0, 0.4)), 4)
            vol = round(float(rng.uniform(100, 1000)), 4)
            rows.append({"symbol": sym, "date": d, "open": opn, "close": close,
                         "high": high, "low": low, "volume": vol})
    df = pl.DataFrame(rows)
    # 在 A 的第 3 行 close 注入 null，校验 NaN 传播。
    return df.with_columns(
        pl.when((pl.col("symbol") == "A") & (pl.col("date") == 3))
        .then(None)
        .otherwise(pl.col("close"))
        .alias("close")
    )


def _per_symbol_values(df: pl.DataFrame, col: str, out: str) -> dict[str, np.ndarray]:
    # partition_by(as_dict=True) 的键是分组列取值的 tuple，取首列得到纯 symbol 字符串。
    return {s[0]: gdf.sort("date")[out].to_numpy() for s, gdf in df.partition_by("symbol", as_dict=True).items()}


# --------------------------------------------------------------------------- #
# rank / scale（横截面）
# --------------------------------------------------------------------------- #
def test_rank_cross_sectional_percentile():
    df = _panel()
    out = df.with_columns(ops.rank(pl.col("close")).alias("r"))

    # 逐交易日对照 pandas：method=average, pct, na_option=keep。
    for _, gdf in out.partition_by("date", as_dict=True).items():
        pdf = gdf.to_pandas()
        expected = pdf["close"].rank(method="average", pct=True, na_option="keep")
        got = pdf["r"].to_numpy()
        assert np.allclose(got, expected.to_numpy(), equal_nan=True)


def test_rank_nan_propagation():
    df = _panel()
    out = df.with_columns(ops.rank(pl.col("close")).alias("r"))
    a = out.filter((pl.col("symbol") == "A") & (pl.col("date") == 3))
    # 注入了 null 的位置，rank 必须为 null。
    assert a["r"].null_count() == 1


def test_scale_l1_normalization():
    df = _panel()
    out = df.with_columns(ops.scale(pl.col("close"), a=1.0).alias("s"))
    # 每个交易日内 |s| 之和应为 1（A 的第 3 天因 null 被排除，其余 2 个符号求和=1）。
    sums = out.group_by("date").agg(pl.col("s").abs().sum().alias("abs_sum"))
    assert np.allclose(sums.sort("date")["abs_sum"].to_numpy(), 1.0)


def test_scale_zero_row_is_nan():
    # 全 0 行：绝对值之和 0 → 整行 NaN（不静默归零）。
    df = pl.DataFrame({"symbol": ["A", "B"], "date": [0, 0], "x": [0.0, 0.0]})
    out = df.with_columns(ops.scale(pl.col("x")).alias("s"))
    assert out["s"].is_nan().all()


# --------------------------------------------------------------------------- #
# ts_mean / ts_std / ts_sum / ts_max / ts_min
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("window", [1, 3, 5])
def test_ts_mean_matches_numpy(window):
    df = _panel()
    out = df.with_columns(ops.ts_mean(pl.col("close"), window).over("symbol").alias("m"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["m"].to_numpy()
        # numpy warmup → nan，min_periods = window。
        ref = np.full(len(vals), np.nan)
        for i in range(window - 1, len(vals)):
            w = vals[i - window + 1 : i + 1]
            # min_periods = window：窗口内任一 null → 整窗 null（非 nanmean）。
            ref[i] = np.nan if np.isnan(w).any() else float(np.mean(w))
        assert np.allclose(got, ref, equal_nan=True), sym


@pytest.mark.parametrize("window", [2, 4])
def test_ts_std_matches_numpy_ddof1(window):
    df = _panel()
    out = df.with_columns(ops.ts_std(pl.col("close"), window).over("symbol").alias("s"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["s"].to_numpy()
        ref = np.full(len(vals), np.nan)
        for i in range(window - 1, len(vals)):
            w = vals[i - window + 1 : i + 1]
            ref[i] = np.nan if np.isnan(w).any() else float(np.std(w, ddof=1))
        assert np.allclose(got, ref, equal_nan=True), sym


@pytest.mark.parametrize("window", [1, 3, 5])
def test_ts_sum_matches_numpy(window):
    df = _panel()
    out = df.with_columns(ops.ts_sum(pl.col("close"), window).over("symbol").alias("sm"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["sm"].to_numpy()
        ref = np.full(len(vals), np.nan)
        for i in range(window - 1, len(vals)):
            ref[i] = float(np.sum(vals[i - window + 1 : i + 1]))
        assert np.allclose(got, ref, equal_nan=True), sym


@pytest.mark.parametrize("window", [1, 3])
def test_ts_max_min_match_numpy(window):
    df = _panel()
    out = df.with_columns([
        ops.ts_max(pl.col("close"), window).over("symbol").alias("mx"),
        ops.ts_min(pl.col("close"), window).over("symbol").alias("mn"),
    ])
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        sub = out.filter(pl.col("symbol") == sym).sort("date")
        gotmx, gotmn = sub["mx"].to_numpy(), sub["mn"].to_numpy()
        refmx = np.full(len(vals), np.nan)
        refmn = np.full(len(vals), np.nan)
        for i in range(window - 1, len(vals)):
            w = vals[i - window + 1 : i + 1]
            # min_periods = window：窗口内任一 null → 整窗 null（非 nanmax/nanmin）。
            if np.isnan(w).any():
                refmx[i] = refmn[i] = np.nan
            else:
                refmx[i], refmn[i] = float(np.max(w)), float(np.min(w))
        assert np.allclose(gotmx, refmx, equal_nan=True), sym
        assert np.allclose(gotmn, refmn, equal_nan=True), sym


# --------------------------------------------------------------------------- #
# ts_delta / ts_delay / signed_power
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("d", [1, 2, 3])
def test_ts_delta_matches_shift_diff(d):
    df = _panel()
    out = df.with_columns(ops.ts_delta(pl.col("close"), d).over("symbol").alias("dl"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["dl"].to_numpy()
        ref = vals - np.roll(vals, d)
        ref[:d] = np.nan
        assert np.allclose(got, ref, equal_nan=True), sym


@pytest.mark.parametrize("d", [1, 2])
def test_ts_delay_matches_shift(d):
    df = _panel()
    out = df.with_columns(ops.ts_delay(pl.col("close"), d).over("symbol").alias("dy"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["dy"].to_numpy()
        ref = np.roll(vals, d).astype(float)
        ref[:d] = np.nan
        assert np.allclose(got, ref, equal_nan=True), sym


def test_ts_delta_lookahead_ban():
    with pytest.raises(ValueError):
        ops.ts_delta(pl.col("close"), 0)
    with pytest.raises(ValueError):
        ops.ts_delta(pl.col("close"), -1)


def test_ts_delay_lookahead_ban():
    with pytest.raises(ValueError):
        ops.ts_delay(pl.col("close"), 0)


@pytest.mark.parametrize("p", [0.5, 2.0, 3.0])
def test_signed_power_preserves_sign(p):
    df = pl.DataFrame({"symbol": ["A"] * 5, "date": list(range(5)),
                       "x": [2.0, -3.0, 0.0, 0.5, -0.5]})
    out = df.with_columns(ops.signed_power(pl.col("x"), p).alias("sp"))
    vals = df["x"].to_numpy()
    expected = np.sign(vals) * np.power(np.abs(vals), p)
    assert np.allclose(out["sp"].to_numpy(), expected)


def test_signed_power_nan_propagates():
    df = pl.DataFrame({"symbol": ["A", "A"], "date": [0, 1], "x": [2.0, None]})
    out = df.with_columns(ops.signed_power(pl.col("x"), 2.0).alias("sp"))
    assert out["sp"].to_list() == [4.0, None]


# --------------------------------------------------------------------------- #
# decay_linear
# --------------------------------------------------------------------------- #
def test_decay_linear_weights_and_warmup():
    df = _panel()
    n = 4
    out = df.with_columns(ops.decay_linear(pl.col("close"), n).over("symbol").alias("dl"))
    # 标准 Alpha101：最近一期权重最大。w=[最旧..最新]，故权重反转使最新值取 n。
    weights = np.arange(n, 0, -1, dtype=float)
    weights = weights[::-1] / weights.sum()
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["dl"].to_numpy()
        ref = np.full(len(vals), np.nan)
        for i in range(n - 1, len(vals)):
            w = vals[i - n + 1 : i + 1]
            ref[i] = np.nan if np.isnan(w).any() else float(np.dot(w, weights))
        assert np.allclose(got, ref, equal_nan=True), sym


def test_decay_linear_causal_recent_weight_max():
    # 标准 Alpha101 / decay 语义：最近一期权重最大（= n/sum）。
    # 窗口 [1,1,100]，最近值 100 取权重 3/6 → (100*3 + 1*2 + 1*1)/6 = 303/6。
    df = pl.DataFrame({"symbol": ["A"] * 4, "date": [0, 1, 2, 3], "x": [1.0, 1.0, 1.0, 100.0]})
    out = df.with_columns(ops.decay_linear(pl.col("x"), 3).over("symbol").alias("dl"))
    assert np.isclose(out.filter(pl.col("date") == 3)["dl"][0], 303 / 6)


# --------------------------------------------------------------------------- #
# ts_rank
# --------------------------------------------------------------------------- #
def _numpy_ts_rank(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1 : i + 1]
        last = w[-1]
        if np.isnan(last) or np.isnan(w).any():
            continue
        less = int((w < last).sum())
        eq = int((w == last).sum())
        out[i] = (less + 0.5 * (eq + 1)) / n
    return out


@pytest.mark.parametrize("n", [1, 3, 5])
def test_ts_rank_matches_reference(n):
    df = _panel()
    out = df.with_columns(ops.ts_rank(pl.col("close"), n).over("symbol").alias("tr"))
    for sym, vals in _per_symbol_values(df, "close", "close").items():
        got = out.filter(pl.col("symbol") == sym).sort("date")["tr"].to_numpy()
        ref = _numpy_ts_rank(vals, n)
        assert np.allclose(got, ref, equal_nan=True), sym


def test_ts_rank_ties_average():
    df = pl.DataFrame({"symbol": ["A"] * 4, "date": list(range(4)), "x": [2.0, 2.0, 2.0, 2.0]})
    out = df.with_columns(ops.ts_rank(pl.col("x"), 3).over("symbol").alias("tr"))
    # 全相等窗口：less=0, eq=3 → (0 + 0.5*4)/3 = 2/3
    vals = out.sort("date")["tr"].to_numpy()
    assert np.allclose(vals[2:], [2 / 3, 2 / 3], equal_nan=True)
    assert np.isnan(vals[0]) and np.isnan(vals[1])


def test_ts_rank_max_is_one():
    df = pl.DataFrame({"symbol": ["A"] * 4, "date": list(range(4)), "x": [1.0, 2.0, 3.0, 9.0]})
    out = df.with_columns(ops.ts_rank(pl.col("x"), 3).over("symbol").alias("tr"))
    assert np.isclose(out.filter(pl.col("date") == 3)["tr"][0], 1.0)


# --------------------------------------------------------------------------- #
# ts_corr
# --------------------------------------------------------------------------- #
def test_ts_corr_perfect_correlation():
    df = _panel()
    out = df.with_columns(ops.ts_corr(pl.col("close"), pl.col("volume"), 5).over("symbol").alias("c"))
    # 取无 null 的 B，构造完美正相关替身校验公式。
    base = pl.DataFrame({"symbol": ["B"] * 8, "date": list(range(8)),
                         "x": np.arange(8, dtype=float), "y": 2 * np.arange(8, dtype=float)})
    res = base.with_columns(ops.ts_corr(pl.col("x"), pl.col("y"), 5).alias("c"))
    corr = res.sort("date")["c"].to_numpy()
    # warmup null，其余应 ≈ 1.0
    assert np.allclose(corr[4:], 1.0, equal_nan=True)
    assert np.isnan(corr[0])


def test_ts_corr_matches_numpy():
    df = _panel()
    n = 5
    out = df.with_columns(ops.ts_corr(pl.col("close"), pl.col("volume"), n).over("symbol").alias("c"))
    for key, gdf in df.partition_by("symbol", as_dict=True).items():
        sym = key[0]
        x = gdf.sort("date")["close"].to_numpy()
        y = gdf.sort("date")["volume"].to_numpy()
        got = out.filter(pl.col("symbol") == sym).sort("date")["c"].to_numpy()
        ref = np.full(len(x), np.nan)
        for i in range(n - 1, len(x)):
            wx, wy = x[i - n + 1 : i + 1], y[i - n + 1 : i + 1]
            if np.isnan(wx).any() or np.isnan(wy).any():
                continue
            sx, sy = np.std(wx, ddof=1), np.std(wy, ddof=1)
            if sx == 0 or sy == 0:
                continue  # 常量 → NaN
            ref[i] = float(np.corrcoef(wx, wy)[0, 1])
        assert np.allclose(got, ref, equal_nan=True), sym


def test_ts_corr_constant_series_is_nan():
    base = pl.DataFrame({"symbol": ["A"] * 4, "date": list(range(4)),
                         "x": [2.0, 2.0, 2.0, 2.0], "y": [1.0, 2.0, 3.0, 4.0]})
    out = base.with_columns(ops.ts_corr(pl.col("x"), pl.col("y"), 3).alias("c"))
    # x 恒定 → 分母 0 → null（非 0）。
    assert out["c"].null_count() == 4


# --------------------------------------------------------------------------- #
# ts_regression
# --------------------------------------------------------------------------- #
def test_ts_regression_residual_matches_lstsq():
    df = _panel()
    n = 5
    out = df.with_columns(
        ops.ts_regression(pl.col("close"), pl.col("volume"), n).over("symbol").alias("resid")
    )
    for key, gdf in df.partition_by("symbol", as_dict=True).items():
        sym = key[0]
        yv = gdf.sort("date")["close"].to_numpy()
        xv = gdf.sort("date")["volume"].to_numpy()
        got = out.filter(pl.col("symbol") == sym).sort("date")["resid"].to_numpy()
        ref = np.full(len(xv), np.nan)
        for i in range(n - 1, len(xv)):
            wx, wy = xv[i - n + 1 : i + 1], yv[i - n + 1 : i + 1]
            if np.isnan(wx).any() or np.isnan(wy).any():
                continue
            if np.std(wx, ddof=1) == 0:
                continue
            b, a = np.polyfit(wx, wy, 1)
            ref[i] = wy[-1] - (a + b * wx[-1])
        assert np.allclose(got, ref, equal_nan=True), sym


def test_ts_regression_perfect_fit_zero_residual():
    # y 完美线性于 x → 残差 ≈ 0。
    base = pl.DataFrame({"symbol": ["A"] * 6, "date": list(range(6)),
                         "x": np.arange(6, dtype=float), "y": 2 * np.arange(6, dtype=float) + 1})
    out = base.with_columns(ops.ts_regression(pl.col("y"), pl.col("x"), 4).alias("resid"))
    resid = out.sort("date")["resid"].to_numpy()
    assert np.allclose(resid[3:], 0.0, atol=1e-9, equal_nan=True)
