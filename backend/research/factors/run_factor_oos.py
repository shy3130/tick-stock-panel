"""walk-forward OOS + Deflated Sharpe：诚实检验因子工厂是否真有 alpha。

P6 已暴露问题：因子工厂 Top 因子在样本内 Sharpe +2.9，接入真实引擎后崩成 -97%，
典型多重检验幸存者偏差。本脚本用 walk-forward 把它坐实 / 推翻：

  1. 同 run_factor_search 的样本与 DSL，但把 2025 当训练段、2026H1 当测试段（无前视）
  2. 训练段：随机 1200 公式 + 5 种子公式，按 ICIR 选 Top-N
  3. 测试段：对【全部】公式算 OOS IC/ICIR/Top-decile 多头 Sharpe，建 null 分布
  4. Deflated Sharpe：以「V 次随机检验里的最大测试段 Sharpe」为多检验基准，
     问 train 选出的 Top 因子 OOS 是否显著高于「纯钓鱼」能拿到的最好结果
  5. 落盘 strategy_factor_oos.json + 报告

用法: cd backend && .venv/Scripts/python.exe -m research.factors.run_factor_oos
"""

import json
import math
import random
import time
from datetime import date

import numpy as np
import polars as pl

from research.common.factor_dsl import FEATURE_NAMES, StackVM, formula_to_str, gen_formula
from research.common.universe import stable_symbol_sample, universe_manifest
from research.factors.run_factor_search import (
    SEED_FORMULAS,
    build_features,
    cross_sectional_score,
)
from research.paths import DATA_DIR, FACTOR_ARTIFACTS_DIR

OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_oos.json"

TRAIN0, TRAIN1 = date(2025, 1, 1), date(2025, 12, 31)
TEST0, TEST1 = date(2026, 1, 1), date(2026, 6, 24)
N_SYM = 400
N_FORMULAS = 1200
N_TOP = 20
SEED = 20260622


def select_symbols():
    lf = pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )
    all_syms = (
        lf.filter((pl.col("date") >= TRAIN0) & (pl.col("date") <= TEST1))
        .select("symbol")
        .unique()
        .collect()["symbol"]
        .to_list()
    )
    return stable_symbol_sample(all_syms, N_SYM, SEED)


def load_subset(symbols, start, end):
    lf = pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )
    df = (
        lf.filter(
            (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & (pl.col("symbol").is_in(symbols))
        )
        .collect()
    )
    feats = build_features(df)
    print(f"[load] {start}~{end}: 样本 {len(symbols)} 只 -> 有效 {len(feats)} 只", flush=True)
    return feats


def eval_formulas(feats, formulas):
    """对公式列表在每个 symbol 上跑 StackVM，返回 [(name, tokens, mean_ic, icir, sharpe), ...]。"""
    symbols = list(feats.keys())
    S = len(symbols)
    T = min(len(feats[s]["close"]) for s in symbols)
    fwdret = np.full((S, T), np.nan)
    for i, s in enumerate(symbols):
        c = feats[s]["close"][-T:]
        fr = np.full(T, np.nan)
        if c.size >= 2:
            fr[1:] = c[1:] / c[:-1] - 1.0
        fwdret[i] = fr
    vm = StackVM()
    out = []
    for name, tokens in formulas:
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
        out.append((name, tokens, mean_ic, icir, sharpe))
    return out, T


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def dsr_pvalue(sr, T, benchmark=0.0):
    """Deflated/Probabilistic Sharpe Ratio p-value（高斯假设 γ3=0, γ4=3）。

    benchmark=0 即 PSR（检验 SR>0）；benchmark=多检验最大 SR 即 DSR（校正钓鱼）。"""
    denom = math.sqrt(max(1e-9, 1.0 + sr * sr / 2.0))
    t = (sr - benchmark) * math.sqrt(max(1, T - 1)) / denom
    return 1.0 - norm_cdf(t)


def main():
    t0 = time.time()
    print("[factor-oos] walk-forward OOS + Deflated Sharpe 启动", flush=True)
    symbols = select_symbols()
    feats_train = load_subset(symbols, TRAIN0, TRAIN1)
    feats_test = load_subset(symbols, TEST0, TEST1)
    common = set(feats_train.keys()) & set(feats_test.keys())
    feats_train = {s: feats_train[s] for s in common}
    feats_test = {s: feats_test[s] for s in common}
    print(f"[factor-oos] 训练/测试共有 {len(common)} 只", flush=True)

    # 公式集合（与 run_factor_search 同 seed，可复现）
    rng = random.Random(SEED + 1)
    formulas = list(SEED_FORMULAS.items())
    for i in range(N_FORMULAS):
        formulas.append((f"rand_{i}", gen_formula(rng, max_len=10)))

    res_train, T_train = eval_formulas(feats_train, formulas)
    res_test, T_test = eval_formulas(feats_test, formulas)
    print(f"[factor-oos] 训练段评 {len(res_train)} 公式 / 测试段评 {len(res_test)} 公式", flush=True)

    # 测试段 null 分布（仅随机公式）
    test_by_name = {r[0]: r for r in res_test}
    null_sharpes = [r[4] for r in res_test if r[0].startswith("rand_")]
    null_max = max(null_sharpes) if null_sharpes else 0.0
    null_mean = float(np.mean(null_sharpes)) if null_sharpes else 0.0
    null_std = float(np.std(null_sharpes)) if null_sharpes else 0.0

    # 训练段按 ICIR 选 Top-N
    res_train.sort(key=lambda r: (r[3], r[4]), reverse=True)
    top = res_train[:N_TOP]

    top_oos = []
    for name, tokens, mic_tr, icir_tr, shp_tr in top:
        if name not in test_by_name:
            continue
        _, _, mic_te, icir_te, shp_te = test_by_name[name]
        all_sharpes_sorted = sorted(null_sharpes)
        rank = (sum(1 for x in null_sharpes if x <= shp_te) + 1)
        pct = rank / (len(null_sharpes) + 1)
        top_oos.append({
            "name": name,
            "formula": formula_to_str(tokens),
            "train_mean_ic": round(mic_tr, 4),
            "train_icir": round(icir_tr, 3),
            "train_sharpe": round(shp_tr, 3),
            "test_mean_ic": round(mic_te, 4),
            "test_icir": round(icir_te, 3),
            "test_sharpe": round(shp_te, 3),
            "icir_decay": round(icir_te - icir_tr, 3),
            "oos_pct_in_null": round(pct, 4),
        })

    # DSR：取 Top-N 中最佳 OOS Sharpe
    best = max(top_oos, key=lambda d: d["test_sharpe"]) if top_oos else None
    dsr = None
    if best:
        sr = best["test_sharpe"]
        dsr = {
            "best_factor": best["name"],
            "best_oos_sharpe": sr,
            "T_test_obs": T_test - 1,
            "null_mean_sharpe": round(null_mean, 3),
            "null_max_sharpe": round(null_max, 3),
            "PSR_p_gt0": round(dsr_pvalue(sr, T_test - 1, 0.0), 4),
            "DSR_p_gt_nullmax": round(dsr_pvalue(sr, T_test - 1, null_max), 4),
        }

    out = {
        "config": {
            "train": f"{TRAIN0}~{TRAIN1}",
            "test": f"{TEST0}~{TEST1}",
            "n_symbols": len(common),
            "n_formulas": len(formulas),
            "n_top": N_TOP,
            "seed": SEED,
            "evidence_status": "canonical_historical_replay_not_fresh_oos",
            "universe_manifest": universe_manifest(
                symbols,
                seed=SEED,
                requested_size=N_SYM,
                start=TRAIN0,
                end=TEST1,
            ),
        },
        "null_distribution": {
            "n_random": len(null_sharpes),
            "mean_sharpe": round(null_mean, 3),
            "std_sharpe": round(null_std, 3),
            "max_sharpe": round(null_max, 3),
            "frac_positive": round(float(np.mean([s > 0 for s in null_sharpes])), 3) if null_sharpes else 0.0,
        },
        "deflated_sharpe": dsr,
        "top_oos": top_oos,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s ==", flush=True)
    if dsr:
        print(f"最佳 OOS 因子 {dsr['best_factor']} Sharpe={dsr['best_oos_sharpe']:+.2f} "
              f"PSR(p>0)={dsr['PSR_p_gt0']} DSR(p>nullmax)={dsr['DSR_p_gt_nullmax']}", flush=True)
    print("Top 10 OOS（按测试段 Sharpe）:", flush=True)
    for d in sorted(top_oos, key=lambda x: x["test_sharpe"], reverse=True)[:10]:
        print(f"  {d['name']:<14} trICIR={d['train_icir']:+.2f} teICIR={d['test_icir']:+.2f} "
              f"teSharpe={d['test_sharpe']:+.2f} oosPct={d['oos_pct_in_null']:.2f} | {d['formula']}", flush=True)


if __name__ == "__main__":
    main()
