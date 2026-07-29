"""吸取 AlphaGPT 精华：在 tickflow 的真实 A 股数据上跑「自动因子工厂」。

流程（与 AlphaGPT 同构，但评估器用我们更严谨的框架）：
  1. 取一篮子 A 股样本（real data from kline_daily_enriched）
  2. 计算股权特征（factor_dsl.compute_features）
  3. 随机生成 RPN 因子公式（factor_dsl.gen_formula）
  4. StackVM 横截面向量化执行 -> 信号矩阵 [S, T]
  5. 打分：横截面 IC / ICIR + Top-decile 多头日收益 Sharpe
  6. 输出 Top 因子

用法: cd backend && .venv/Scripts/python.exe -m research.factors.run_factor_search
"""

import json
import random
import time
from datetime import date

import numpy as np
import polars as pl

from research.common.factor_dsl import FEATURE_NAMES, StackVM, formula_to_str, gen_formula
from research.common.universe import (
    stable_symbol_sample,
    universe_manifest,
    write_universe_manifest,
)
from research.paths import DATA_DIR, FACTOR_ARTIFACTS_DIR

OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_search.json"
UNIVERSE_OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_search_universe.json"

D0 = date(2025, 1, 1)
D1 = date(2026, 6, 24)
N_SYM = 400
N_FORMULAS = 1200
TOP_DECILE = 0.10
SEED = 20260622

# 几个可解释的"种子"公式（验证 DSL 能表达我们的已知逻辑）
SEED_FORMULAS = {
    "momentum20": ["MOM20"],
    "pullback_ma20": ["MA20_DEV"],
    "vol_breakout": ["MOM5", "VOL_RATIO", "MUL"],
    "mean_revert": ["RET", "NEG"],
    "trend_strength": ["MA60_DEV", "MOM20", "MUL"],
}


def load_sample():
    lf = pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )
    all_syms = (
        lf.filter((pl.col("date") >= D0) & (pl.col("date") <= D1))
        .select("symbol")
        .unique()
        .collect()["symbol"]
        .to_list()
    )
    sample = stable_symbol_sample(all_syms, N_SYM, SEED)
    write_universe_manifest(
        UNIVERSE_OUT,
        universe_manifest(
            sample,
            seed=SEED,
            requested_size=N_SYM,
            start=D0,
            end=D1,
        ),
    )
    df = (
        lf.filter(
            (pl.col("date") >= D0)
            & (pl.col("date") <= D1)
            & (pl.col("symbol").is_in(sample))
        )
        .collect()
    )
    print(f"[data] 样本 {len(sample)} 只, 行数 {df.height}", flush=True)
    return df


def build_features(df):
    """返回 {symbol: {"feat": {...}, "close": np.array}}，单次扫描同源，杜绝错位。"""
    feats = {}
    cols = set(df.columns)
    for sym, g in df.group_by("symbol", maintain_order=True):
        g = g.sort("date")
        close = g["close"].to_numpy().astype(float)
        if close.size < 60:
            continue
        op = g["open"].to_numpy().astype(float) if "open" in cols else close
        high = g["high"].to_numpy().astype(float) if "high" in cols else close
        low = g["low"].to_numpy().astype(float) if "low" in cols else close
        vol = g["volume"].to_numpy().astype(float)
        turn = g["turnover_rate"].to_numpy().astype(float) if "turnover_rate" in cols else np.zeros_like(close)
        if np.all(np.isnan(turn)):
            turn = np.zeros_like(close)
        f = compute_features_safe(close, op, high, low, vol, turn)
        feats[sym[0]] = {"feat": f, "close": close}
    return feats


def compute_features_safe(close, op, high, low, vol, turn):
    from research.common.factor_dsl import compute_features
    return compute_features(None, close, op, high, low, vol, turn)


def cross_sectional_score(signal_mat, fwdret_mat):
    """signal_mat, fwdret_mat: [S, T]。返回 (meanIC, icir, top_decile_sharpe)。"""
    S, T = signal_mat.shape
    sig = np.nan_to_num(signal_mat, nan=0.0)
    fwd = fwdret_mat

    # ---- IC: signal[:,t] vs fwdret[:,t+1] ----
    sig_f = sig[:, :-1]
    tgt = fwd[:, 1:]
    sc = sig_f - np.nanmean(sig_f, axis=0, keepdims=True)
    tc = tgt - np.nanmean(tgt, axis=0, keepdims=True)
    numer = np.nansum(sc * tc, axis=0)
    denom = np.sqrt(np.nansum(sc**2, axis=0) * np.nansum(tc**2, axis=0)) + 1e-9
    ic = numer / denom
    ic = ic[np.isfinite(ic)]
    if ic.size < 10:
        return 0.0, 0.0, 0.0
    mean_ic = float(np.mean(ic))
    std_ic = float(np.std(ic)) + 1e-9
    icir = mean_ic / std_ic * np.sqrt(252)

    # ---- Top-decile 多头日收益 Sharpe ----（纯 numpy 排名，避免 scipy 依赖）
    ranks = np.argsort(np.argsort(sig, axis=0), axis=0) + 1.0  # 序数秩 1..S
    top_mask = ranks >= (1.0 - TOP_DECILE) * S
    port = np.full(T - 1, np.nan)
    for u in range(1, T):
        sel = top_mask[:, u - 1]
        vals = fwd[sel, u]
        vals = vals[np.isfinite(vals)]
        if vals.size:
            port[u - 1] = vals.mean()
    port = port[np.isfinite(port)]
    if port.size < 20 or port.std() == 0:
        return mean_ic, icir, 0.0
    sharpe = float(port.mean() / port.std() * np.sqrt(252))
    return mean_ic, icir, sharpe


def main():
    t0 = time.time()
    print("[factor-search] 启动 (AlphaGPT DSL + tickflow 真实数据)", flush=True)
    df = load_sample()
    feats = build_features(df)
    symbols = list(feats.keys())
    S = len(symbols)
    # 不同 symbol 历史长度不一致 -> 截断到最小长度(取尾部, 在最近端对齐)
    T = min(len(feats[s]["close"]) for s in symbols)
    fwdret = np.full((S, T), np.nan)
    # 同源计算前向收益
    for i, s in enumerate(symbols):
        c = feats[s]["close"][-T:]
        fr = np.full(T, np.nan)
        if c.size >= 2:
            fr[1:] = c[1:] / c[:-1] - 1.0
        fwdret[i] = fr

    vm = StackVM()
    results = []

    # 种子公式
    cand = list(SEED_FORMULAS.items())
    rng = random.Random(SEED + 1)
    for _ in range(N_FORMULAS):
        cand.append((f"rand_{_}", gen_formula(rng, max_len=10)))

    for name, tokens in cand:
        sig_mat = np.full((S, T), np.nan)
        ok = True
        for i, s in enumerate(symbols):
            sig = vm.execute(tokens, feats[s]["feat"])
            if sig is None:
                ok = False
                break
            sig_mat[i] = sig[-T:]
        if not ok:
            continue
        try:
            mean_ic, icir, sharpe = cross_sectional_score(sig_mat, fwdret)
        except Exception:
            continue
        results.append({
            "name": name,
            "formula": formula_to_str(tokens),
            "mean_ic": round(mean_ic, 4),
            "icir": round(icir, 3),
            "top_decile_sharpe": round(sharpe, 3),
        })

    results.sort(key=lambda r: (r["icir"], r["top_decile_sharpe"]), reverse=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 完成 | 公式 {len(results)} 个 | 用时 {time.time()-t0:.0f}s ==", flush=True)
    print("Top 15 (按 ICIR):", flush=True)
    for r in results[:15]:
        print(f"  {r['name']:<18} IC={r['mean_ic']:+.4f} ICIR={r['icir']:+.2f} "
              f"Sharpe={r['top_decile_sharpe']:+.2f}  | {r['formula']}", flush=True)


if __name__ == "__main__":
    main()
