"""多区间 walk-forward OOS（P9）：消解 P8 暴露的「单区间 regime 运气」。

P8 结论里最刺眼的一点：6 个语义因子 train(2025) ICIR 全负、test(2026H1) 全正——
单测试区间碰巧是动量牛，不能据此分配资金。本脚本把这个隐患坐实/排除：

  1. 用全量 enriched 数据（2024-09-24 ~ 2026-06-30）固定一个 universe（与 P7/P8 同口径 400 只）
  2. 切成 6 个季度测试区间，每个区间用「此前全部历史」做训练（expanding window，无前视）
  3. 每个区间对 6 个语义因子(+2 基准) 算 train/test 的 IC / ICIR / Top-decile 多头 Sharpe
  4. 每个区间用 100 个随机公式建 null，算该因子测试段 Sharpe 在 null 中的百分位
  5. 跨 6 区间聚合：正收益区间数、方向一致区间数、平均 OOS Sharpe
     → 稳健判据：正收益区间 >= 4/6 且方向一致 >= 4/6 才算「跨 regime 稳健」

用法: cd backend && .venv/Scripts/python.exe -m research.factors.run_factor_walkforward
"""

import json
import math
import random
import time
from datetime import date

import numpy as np
import polars as pl

from research.common.factor_dsl import StackVM, formula_to_str, gen_formula
from research.common.universe import stable_symbol_sample, universe_manifest
from research.factors.run_factor_oos import norm_cdf
from research.factors.run_factor_search import build_features, cross_sectional_score
from research.paths import DATA_DIR, FACTOR_ARTIFACTS_DIR

OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_walkforward.json"

N_SYM = 400
N_RANDOM = 100          # 每区间 null 规模（百分位上下文，非主判据）
SEED = 20260723

# 全量范围（数据实际 2024-09-24 ~ 2026-07-21；取至 2026-06-30 收口）
FULL0 = date(2024, 9, 24)
FULL1 = date(2026, 6, 30)

# 折叠数（按实际交易日动态切分，保证每区间 >= 60 交易日，避开 build_features 的 close.size<60 丢弃）
N_FOLDS = 4
TRAIN_SKIP_TD = 80      # 初始训练最少交易日（让 MA60 等指标有效）

# 语义因子（与 run_factor_semantic.py 完全一致，可对照）
SEMANTIC = {
    "mom_trend":    ["MOM20", "MA60_DEV", "SIGN", "MUL"],
    "mom_vol":      ["MOM20", "VOL_RATIO", "SIGN", "MUL"],
    "mom_anti_ext": ["MOM20", "MA20_DEV", "ABS", "DIV"],
    "mom_rsi":      ["MOM20", "RSI14", "MUL"],
    "ma20_dev":     ["MA20_DEV"],
    "mom20":        ["MOM20"],
}
NARRATIVE = {
    "mom_trend":    "趋势确认动量：站稳 MA60 才采信 20 日动量方向。",
    "mom_vol":      "量能确认动量：放量才信动量，缩量反弹视为假动作。",
    "mom_anti_ext": "防追高动量：动量按 |MA20 偏离| 倒数加权，规避追顶。",
    "mom_rsi":      "RSI 加权动量：用 RSI14 给动量加置信权重。",
    "ma20_dev":     "基准：纯均线偏离。",
    "mom20":        "基准：纯 20 日动量。",
}


def select_universe():
    lf = pl_scan()
    all_syms = (
        lf.filter((pl.col("date") >= FULL0) & (pl.col("date") <= FULL1))
        .select("symbol").unique().collect()["symbol"].to_list()
    )
    return stable_symbol_sample(all_syms, N_SYM, SEED)


def pl_scan():
    import polars as pl
    return pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )


def load_subset(symbols, start, end):
    import polars as pl
    lf = pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )
    df = lf.filter(
        (pl.col("date") >= start) & (pl.col("date") <= end)
        & (pl.col("symbol").is_in(symbols))
    ).collect()
    feats = build_features(df)
    return feats


def eval_formulas(feats, formulas):
    symbols = list(feats.keys())
    T = min(len(feats[s]["close"]) for s in symbols)
    fwdret = np.full((len(symbols), T), np.nan)
    for i, s in enumerate(symbols):
        c = feats[s]["close"][-T:]
        fr = np.full(T, np.nan)
        if c.size >= 2:
            fr[1:] = c[1:] / c[:-1] - 1.0
        fwdret[i] = fr
    vm = StackVM()
    out = []
    for name, tokens in formulas:
        sig_mat = np.full((len(symbols), T), np.nan)
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


def percentile_rank(value, samples):
    if not samples:
        return float("nan")
    below = sum(1 for x in samples if x <= value)
    return (below + 1) / (len(samples) + 1)


def main():
    import polars as pl  # noqa: F401 (pl_scan 用)
    t0 = time.time()
    print("[wf] 多区间 walk-forward OOS 启动", flush=True)
    symbols = select_universe()
    print(f"[wf] universe = {len(symbols)} 只", flush=True)

    rng = random.Random(SEED + 7)
    rand_formulas = [(f"rand_{i}", gen_formula(rng, max_len=10)) for i in range(N_RANDOM)]
    sem_formulas = [(f"sem_{k}", v) for k, v in SEMANTIC.items()]
    all_formulas = sem_formulas + rand_formulas

    # ── 动态切分：取全量交易日，初始训练 TRAIN_SKIP_TD 天，剩余均分 N_FOLDS 段 ──
    lf_all = pl_scan()
    all_dates = sorted(
        d for d in lf_all.select("date").unique().collect()["date"].to_list()
        if FULL0 <= d <= FULL1
    )
    if len(all_dates) <= TRAIN_SKIP_TD + N_FOLDS:
        raise SystemExit(f"交易日不足：{len(all_dates)}，无法切 {N_FOLDS} 个折叠")
    rest = all_dates[TRAIN_SKIP_TD:]
    chunk = len(rest) // N_FOLDS
    FOLDS = []
    for k in range(N_FOLDS):
        s = k * chunk
        e = (k + 1) * chunk if k < N_FOLDS - 1 else len(rest)
        test_dates = rest[s:e]
        train_dates = all_dates[: TRAIN_SKIP_TD + s]
        FOLDS.append((
            f"F{k+1}",
            train_dates[0], train_dates[-1],
            test_dates[0], test_dates[-1],
        ))
    print(f"[wf] 动态切分 {N_FOLDS} 折叠；总交易日 {len(all_dates)}，每段测试约 {chunk} 天", flush=True)

    fold_results = []
    for fid, tr0, tr1, te0, te1 in FOLDS:
        tr = load_subset(symbols, tr0, tr1)
        te = load_subset(symbols, te0, te1)
        common = set(tr.keys()) & set(te.keys())
        tr = {s: tr[s] for s in common}
        te = {s: te[s] for s in common}
        res_tr, T_tr = eval_formulas(tr, all_formulas)
        res_te, T_te = eval_formulas(te, all_formulas)
        by_tr = {r[0]: r for r in res_tr}
        by_te = {r[0]: r for r in res_te}
        null_te = [r[4] for r in res_te if r[0].startswith("rand_")]

        fac = {}
        for key in SEMANTIC:
            name = f"sem_{key}"
            if name not in by_tr or name not in by_te:
                continue
            _, _, mic_tr, icir_tr, shp_tr = by_tr[name]
            _, _, mic_te, icir_te, shp_te = by_te[name]
            pct = percentile_rank(shp_te, null_te)
            fac[key] = {
                "train_mean_ic": round(mic_tr, 4),
                "train_icir": round(icir_tr, 3),
                "train_sharpe": round(shp_tr, 3),
                "test_mean_ic": round(mic_te, 4),
                "test_icir": round(icir_te, 3),
                "test_sharpe": round(shp_te, 3),
                "oos_pct_in_null": round(pct, 4),
            }
        fold_results.append({
            "fold": fid,
            "train": f"{tr0}~{tr1}",
            "test": f"{te0}~{te1}",
            "n_symbols": len(common),
            "T_test": T_te - 1,
            "null_frac_positive": round(float(np.mean([s > 0 for s in null_te])), 3) if null_te else 0.0,
            "null_mean_sharpe": round(float(np.mean(null_te)), 3) if null_te else 0.0,
            "factors": fac,
        })
        print(f"[wf] {fid}: {len(common)} 只, testSharpe(null均值={fold_results[-1]['null_mean_sharpe']:+.2f})", flush=True)

    # ── 跨区间聚合 ──
    n_folds = len(FOLDS)
    agg = {}
    for key in SEMANTIC:
        pos = 0
        beat_null = 0
        sharpes = []
        icirs_te = []
        for fr in fold_results:
            f = fr["factors"].get(key)
            if not f:
                continue
            if f["test_sharpe"] > 0:
                pos += 1
            if f["test_sharpe"] > fr["null_mean_sharpe"]:
                beat_null += 1
            sharpes.append(f["test_sharpe"])
            icirs_te.append(f["test_icir"])
        mean_oos = float(np.mean(sharpes)) if sharpes else float("nan")
        # 稳健判据：既在多数区间为正(真赚钱)，又在多数区间跑赢随机 null(真有 alpha，非 regime β)
        robust = (pos >= (n_folds // 2 + 1)) and (beat_null >= (n_folds // 2 + 1))
        agg[key] = {
            "narrative": NARRATIVE[key],
            "formula": formula_to_str(SEMANTIC[key]),
            "positive_folds": pos,
            "beat_null_folds": beat_null,
            "n_folds": n_folds,
            "mean_oos_sharpe": round(mean_oos, 3),
            "mean_test_icir": round(float(np.mean(icirs_te)), 3) if icirs_te else float("nan"),
            "robust_across_regimes": bool(robust),
        }

    out = {
        "config": {
            "full_range": f"{FULL0}~{FULL1}",
            "n_symbols": len(symbols),
            "n_folds": n_folds,
            "n_random_null_per_fold": N_RANDOM,
            "seed": SEED,
            "evidence_status": "canonical_historical_replay_not_fresh_oos",
            "universe_manifest": universe_manifest(
                symbols,
                seed=SEED,
                requested_size=N_SYM,
                start=FULL0,
                end=FULL1,
            ),
            "robust_rule": "positive_folds>=3/4 AND beat_null_folds>=3/4 (在多数区间既盈利、又跑赢随机 null)",
        },
        "folds": fold_results,
        "aggregate": agg,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s ==", flush=True)
    print("跨区间稳健性（positive=盈利区间数, beat_null=跑赢随机 null 区间数）：", flush=True)
    for key, a in sorted(agg.items(), key=lambda kv: (kv[1]["positive_folds"], kv[1]["mean_oos_sharpe"]), reverse=True):
        print(f"  {key:<12} +{a['positive_folds']}/{a['n_folds']} 跑赢null{a['beat_null_folds']}/{a['n_folds']} "
              f"meanOOS={a['mean_oos_sharpe']:+.2f} {'PASS' if a['robust_across_regimes'] else 'FAIL'} | {a['formula']}", flush=True)


if __name__ == "__main__":
    main()
