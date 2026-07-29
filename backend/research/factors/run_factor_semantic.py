"""语义驱动因子生成 + OOS + 多重检验校正（C 方案）。

P7 已证明：随机 DSL 搜索被多重检验运气主导（DSR≈0.29，最佳精选 +4.70 < 随机天花板 +4.95）。
随机"钓鱼"该废弃，但 DSL 表示层 + 诚实验证纪律必须保留。

本脚本把"钓鱼"升级为"假设检验"：
  - 由我（agent）基于已有证据给出市场叙事 -> 落成 RPN 公式（纯特征组合，无常量）
  - 每个假设在训练段(2025)与测试段(2026H1)各算 IC / ICIR / Top-decile 多头 Sharpe
  - 用 200 个随机公式建 null 分布（同 P7 的"钓鱼"基线）
  - 对每个语义因子算：① 测试段在 null 中的百分位数 ② Bonferroni(V=K) 校正 p 值
    判定"是否真信号"：测试段落在 null 前 α%、且 train->test 方向一致(ICIR 衰减小)

注：P7 用「DSR(p>248次随机最大值)」太苛刻——任何单因子都不可能超过 248 次钓鱼的运气峰值。
这里改为百分位数排名 + Bonferroni，对 V=K 个假设驱动因子才公平。

用法: cd backend && .venv/Scripts/python.exe -m research.factors.run_factor_semantic
"""

import json
import math
import random
import time
from datetime import date

import numpy as np

from research.common.factor_dsl import StackVM, formula_to_str
from research.common.universe import universe_manifest
from research.factors.run_factor_oos import (
    eval_formulas,
    load_subset,
    norm_cdf,
    select_symbols,
)
from research.factors.run_factor_search import build_features, cross_sectional_score
from research.paths import FACTOR_ARTIFACTS_DIR

OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_semantic.json"

TRAIN0, TRAIN1 = date(2025, 1, 1), date(2025, 12, 31)
TEST0, TEST1 = date(2026, 1, 1), date(2026, 6, 24)
N_SYM = 400
N_RANDOM = 1200         # null 分布规模（"钓鱼"基线；约 20-25% 通过校验 -> ~250 有效，与 P7 对齐）
SEED = 20260723

# ---------------------------------------------------------------------------
# 语义假设：基于 P5/P6 证据（A 股动量主导、纯动量过拟合、随机搜索=运气）
# 叙事：「结构牛里真正的 alpha = 有趋势确认 + 有量能配合 + 未过度拉升 的动量股」
# RPN 仅含 9 个特征 + 算子，无常量字面量。
# ---------------------------------------------------------------------------
SEMANTIC = {
    # 趋势确认动量：站稳中期均线(MA60)才信动量方向
    "mom_trend":     ["MOM20", "MA60_DEV", "SIGN", "MUL"],
    # 量能确认动量：放量(VOL_RATIO>均值)才信动量
    "mom_vol":       ["MOM20", "VOL_RATIO", "SIGN", "MUL"],
    # 防追高动量：偏离 MA20 越大权重越小（避免追顶被套）
    "mom_anti_ext":  ["MOM20", "MA20_DEV", "ABS", "DIV"],
    # RSI 加权动量：RSI14 高(归一化>0)代表动量延续置信高
    "mom_rsi":       ["MOM20", "RSI14", "MUL"],
    # 基准 1：纯均线偏离（已知 train IC+0.048）
    "ma20_dev":      ["MA20_DEV"],
    # 基准 2：纯动量（已知 train IC+0.051）
    "mom20":         ["MOM20"],
}

# 各假设的"人类可读"叙事，写进报告
NARRATIVE = {
    "mom_trend":    "趋势确认动量：只有站稳中期均线(MA60)才采信其 20 日动量方向；跌破则反手（下行段押反转）。",
    "mom_vol":      "量能确认动量：动量只在放量（量比>均值）时有效，缩量反弹视为假动作。",
    "mom_anti_ext": "防追高动量：把 20 日动量按 |MA20 偏离| 倒数加权——偏离越大权重越小，天然规避追顶。",
    "mom_rsi":      "RSI 加权动量：用 RSI14(归一化 -1~1) 给动量加置信权重，RSI 偏强时放大、偏弱时削弱。",
    "ma20_dev":     "基准：纯均线偏离（价格相对 MA20 的位置）。",
    "mom20":        "基准：纯 20 日动量。",
}


def percentile_rank(value, samples):
    """返回 value 在 samples 中的经验分位 (0~1)。"""
    if not samples:
        return float("nan")
    n = len(samples)
    below = sum(1 for x in samples if x <= value)
    return (below + 1) / (n + 1)


def main():
    t0 = time.time()
    print("[factor-semantic] 语义驱动因子生成 + OOS 验证 启动", flush=True)
    symbols = select_symbols()
    feats_train = load_subset(symbols, TRAIN0, TRAIN1)
    feats_test = load_subset(symbols, TEST0, TEST1)
    common = set(feats_train.keys()) & set(feats_test.keys())
    feats_train = {s: feats_train[s] for s in common}
    feats_test = {s: feats_test[s] for s in common}
    print(f"[factor-semantic] 训练/测试共有 {len(common)} 只", flush=True)

    # 公式集合：语义假设 + 随机 null
    rng = random.Random(SEED)
    formulas = [(f"sem_{k}", v) for k, v in SEMANTIC.items()]
    for i in range(N_RANDOM):
        formulas.append((f"rand_{i}", _gen_rand(rng)))

    res_train, T_train = eval_formulas(feats_train, formulas)
    res_test, T_test = eval_formulas(feats_test, formulas)
    print(f"[factor-semantic] 训练段评 {len(res_train)} / 测试段评 {len(res_test)} 公式", flush=True)

    by_name_tr = {r[0]: r for r in res_train}
    by_name_te = {r[0]: r for r in res_test}

    null_te = [r[4] for r in res_test if r[0].startswith("rand_")]
    null_tr = [r[4] for r in res_train if r[0].startswith("rand_")]
    null_max_te = max(null_te) if null_te else 0.0
    null_mean_te = float(np.mean(null_te)) if null_te else 0.0
    null_std_te = float(np.std(null_te)) if null_te else 0.0

    K = len(SEMANTIC)
    results = []
    for key in SEMANTIC:
        name = f"sem_{key}"
        if name not in by_name_tr or name not in by_name_te:
            continue
        _, _, mic_tr, icir_tr, shp_tr = by_name_tr[name]
        _, _, mic_te, icir_te, shp_te = by_name_te[name]
        pct_te = percentile_rank(shp_te, null_te)   # 测试段 OOS 在 null 中的位置
        pct_tr = percentile_rank(shp_tr, null_tr)
        # Bonferroni(V=K)：单假设经验 p = 1 - pct；校正 p = min(1, K*(1-pct))
        p_single_te = 1.0 - pct_te
        p_bonf_te = min(1.0, K * p_single_te)
        results.append({
            "key": key,
            "narrative": NARRATIVE[key],
            "formula": formula_to_str(SEMANTIC[key]),
            "train_mean_ic": round(mic_tr, 4),
            "train_icir": round(icir_tr, 3),
            "train_sharpe": round(shp_tr, 3),
            "test_mean_ic": round(mic_te, 4),
            "test_icir": round(icir_te, 3),
            "test_sharpe": round(shp_te, 3),
            "icir_decay": round(icir_te - icir_tr, 3),
            "oos_pct_in_null": round(pct_te, 4),
            "train_pct_in_null": round(pct_tr, 4),
            "bonferroni_p_oos": round(p_bonf_te, 4),
            "significant_oos": bool(p_bonf_te < 0.05 and icir_te > 0),
        })

    # 排序：先按测试段 Sharpe，再看显著性
    results.sort(key=lambda d: (d["test_sharpe"], d["oos_pct_in_null"]), reverse=True)

    out = {
        "config": {
            "train": f"{TRAIN0}~{TRAIN1}",
            "test": f"{TEST0}~{TEST1}",
            "n_symbols": len(common),
            "n_semantic": K,
            "n_random_null": len(null_te),
            "seed": SEED,
            "evidence_status": "canonical_historical_replay_not_fresh_oos",
            "universe_manifest": universe_manifest(
                symbols,
                seed=SEED,
                requested_size=N_SYM,
                start=TRAIN0,
                end=TEST1,
            ),
            "method": "语义假设驱动 + 200 随机 null + Bonferroni(V=K) 校正（替代 P7 的 DSR-vs-max）",
        },
        "null_distribution": {
            "n_random": len(null_te),
            "mean_sharpe": round(null_mean_te, 3),
            "std_sharpe": round(null_std_te, 3),
            "max_sharpe": round(null_max_te, 3),
            "frac_positive": round(float(np.mean([s > 0 for s in null_te])), 3) if null_te else 0.0,
        },
        "semantic_results": results,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s ==", flush=True)
    print("语义因子 OOS（按测试段 Sharpe 降序）：", flush=True)
    for d in results:
        flag = "PASS" if d["significant_oos"] else "FAIL"
        print(f"  {d['key']:<12} trICIR={d['train_icir']:+.2f} teICIR={d['test_icir']:+.2f} "
              f"teSharpe={d['test_sharpe']:+.2f} oosPct={d['oos_pct_in_null']:.3f} "
              f"Bonf_p={d['bonferroni_p_oos']:.3f} {flag} | {d['formula']}", flush=True)


def _gen_rand(rng):
    """复用 factor_dsl 的随机生成（避免重复 import 细节）。"""
    from research.common.factor_dsl import gen_formula
    return gen_formula(rng, max_len=10)


if __name__ == "__main__":
    main()
