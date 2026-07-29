"""Step1+Step2：regime 干净归因（2x2）+ 因子 ensemble，canonical historical replay。

Step1 — 干净归因（消除 regime 信号源混淆）：
  因子动量策略在熊市的两种处置（action）x 两种 regime 信号源（signal）：
    - action:  flat = 熊市空仓 / switch = 熊市切 pullback_to_support
    - signal:  leader = 引擎 leader_index 龙头指数 MA60 / ew = 回测 universe 等权指数 MA60
  组合出 4 个 regime 配置 + mom_trend 基线：
    flat_leader / switch_leader / flat_ew / switch_ew
  归因：
    - action 效应（同信号）：switch_leader - flat_leader；switch_ew - flat_ew
    - signal 效应（同处置）：flat_leader - flat_ew；switch_leader - switch_ew
  关键：switch_leader 与 flat_leader 用完全相同的 leader 信号 -> 第一次真正隔离
        "切换策略本身" 是否加值。

Step2 — 因子 ensemble：
  factor_ensemble（6 个语义动量因子横截面 z-score 归一后等权平均）vs mom_trend 基线。

严格复用 P9 的 400 只 universe（seed 20260723）+ 4 折测试段，数据仅来自
data/kline_daily_enriched/**/*.parquet，回测复用真实引擎（费/滑点5bps/风控）。

用法: cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_regime_ensemble
"""

import json
import time
from datetime import date

import numpy as np
import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.common.universe import stable_symbol_sample, symbols_sha256, universe_manifest
from research.paths import CURRENT_ARTIFACTS_DIR, DATA_DIR

OUT = CURRENT_ARTIFACTS_DIR / "strategy_regime_ensemble.json"

N_SYM = 400
SEED = 20260723
FULL0 = date(2024, 9, 24)
FULL1 = date(2026, 6, 30)
N_FOLDS = 4
TRAIN_SKIP_TD = 80

BASE_OVERRIDES = {
    "stop_loss": -0.05,
    "take_profit": None,
    "trailing_stop": None,
    "max_hold_days": 20,
}

MOM_TREND = "MOM20 MA60_DEV SIGN MUL"
LEADER_FLAT = {"type": "leader_index", "ma": 60, "mode": "soft", "bear_weight": 0.0, "scale_existing": True}

# 对照配置（key, strategy_id, factor_formula|None, regime_filter|None, mp, ps, params|None）
CONFIGS = [
    dict(key="mom_trend", sid="custom_factor", formula=MOM_TREND, regime=None,
         mp=10, ps="score_weight", params=None, note="基线：主因子 mom_trend，无 regime"),
    dict(key="flat_leader", sid="custom_factor", formula=MOM_TREND, regime=LEADER_FLAT,
         mp=10, ps="score_weight", params=None,
         note="熊市空仓(flat) + leader 信号（= regime_flat，引擎 leader_index bear_weight=0）"),
    dict(key="switch_leader", sid="regime_conditional", formula=None, regime=None,
         mp=10, ps="score_weight", params={"bear_strategy": "pullback", "regime_source": "leader"},
         note="熊市切 pullback(switch) + leader 信号（与 flat_leader 同源，做干净归因）"),
    dict(key="flat_ew", sid="regime_conditional", formula=None, regime=None,
         mp=10, ps="score_weight", params={"bear_strategy": "flat", "regime_source": "ew"},
         note="熊市空仓(flat) + 等权指数(ew) 信号"),
    dict(key="switch_ew", sid="regime_conditional", formula=None, regime=None,
         mp=10, ps="score_weight", params={"bear_strategy": "pullback", "regime_source": "ew"},
         note="熊市切 pullback(switch) + 等权指数(ew) 信号（= 原 regime_switch）"),
    # —— 响应式 MA20 信号：让牛熊腿都真正部署，做「干净动作效应」——
    dict(key="flat_ew20", sid="regime_conditional", formula=None, regime=None,
         mp=10, ps="score_weight", params={"bear_strategy": "flat", "regime_source": "ew", "regime_ma": 20},
         note="熊市空仓(flat) + 等权指数 MA20 信号（响应式，牛腿可部署）"),
    dict(key="switch_ew20", sid="regime_conditional", formula=None, regime=None,
         mp=10, ps="score_weight", params={"bear_strategy": "pullback", "regime_source": "ew", "regime_ma": 20},
         note="熊市切 pullback(switch) + 等权指数 MA20 信号（干净动作效应）"),
    dict(key="ensemble", sid="factor_ensemble", formula=None, regime=None,
         mp=10, ps="score_weight", params=None,
         note="因子 ensemble：6 语义动量因子横截面归一后等权平均"),
]


def pl_scan():
    return pl.scan_parquet(
            str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )


def select_universe():
    lf = pl_scan()
    all_syms = (
        lf.filter((pl.col("date") >= FULL0) & (pl.col("date") <= FULL1))
        .select("symbol").unique().collect()["symbol"].to_list()
    )
    return stable_symbol_sample(all_syms, N_SYM, SEED)


def fold_dates():
    lf = pl_scan()
    all_dates = sorted(
        d for d in lf.select("date").unique().collect()["date"].to_list()
        if FULL0 <= d <= FULL1
    )
    rest = all_dates[TRAIN_SKIP_TD:]
    chunk = len(rest) // N_FOLDS
    FOLDS = []
    for k in range(N_FOLDS):
        s = k * chunk
        e = (k + 1) * chunk if k < N_FOLDS - 1 else len(rest)
        test = rest[s:e]
        train = all_dates[: TRAIN_SKIP_TD + s]
        FOLDS.append((f"F{k+1}", train[0], train[-1], test[0], test[-1]))
    return FOLDS


def derive_metrics(res):
    stats = res.get("stats") or {}
    if isinstance(stats, dict) and "total_return" in stats:
        return {
            "total_return": float(stats.get("total_return", 0.0)),
            "sharpe": float(stats.get("sharpe", 0.0)),
            "max_drawdown": float(stats.get("max_drawdown", 0.0)),
            "win_rate": float(stats.get("win_rate", 0.0)),
            "n_trades": int(stats.get("n_trades", 0)),
            "sortino": float(stats.get("sortino", 0.0)) if stats.get("sortino") is not None else None,
            "calmar": float(stats.get("calmar", 0.0)) if stats.get("calmar") is not None else None,
            "avg_pnl": float(stats.get("avg_pnl", 0.0)) if stats.get("avg_pnl") is not None else None,
        }
    trades = res.get("trades") or []
    eq = 1.0
    wins = 0
    for t in trades:
        pnl = t.get("pnl_pct") or 0.0
        eq *= (1.0 + pnl)
        if pnl > 0:
            wins += 1
    n = len(trades)
    total_return = eq - 1.0
    win_rate = wins / n if n else 0.0
    curve = res.get("equity_curve") or []
    sharpe = max_dd = 0.0
    if curve:
        vals = [float(r["value"]) for r in curve]
        rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
        rets = np.array(rets, dtype=float)
        if len(rets) > 1 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
        peaks = np.maximum.accumulate(vals)
        dd = np.min(vals / peaks - 1)
        max_dd = float(dd)
    return {
        "total_return": total_return, "sharpe": sharpe, "max_drawdown": max_dd,
        "win_rate": win_rate, "n_trades": n, "sortino": None, "calmar": None, "avg_pnl": None,
    }


def run_engine(cfg, start, end, symbols):
    params = {}
    if cfg["formula"]:
        params["factor_formula"] = cfg["formula"]
    if cfg["params"]:
        params.update(cfg["params"])
    params = params or None
    backtest_cfg = StrategyBacktestConfig(
        strategy_id=cfg["sid"], symbols=symbols,
        start=start, end=end, params=params, overrides=dict(BASE_OVERRIDES),
        matching="open_t+1", entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=cfg["mp"], max_exposure_pct=1.0, initial_capital=1_000_000.0,
        position_sizing=cfg["ps"], mode="position", holding_days=20, asset_type="stock",
        minute_fill=False, regime_filter=cfg["regime"],
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, backtest_cfg))
    if res.get("error"):
        return None, res["error"]
    return derive_metrics(res), None


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def _agg_for(key, fold_records):
    rets, shps, mdds, wins, ntr = [], [], [], [], []
    pos = 0
    for fr in fold_records:
        r = next((x for x in fr["runs"] if x.get("key") == key and "error" not in x), None)
        if not r:
            continue
        rets.append(r["total_return"]); shps.append(r["sharpe"])
        mdds.append(r["max_drawdown"]); wins.append(r["win_rate"]); ntr.append(r["n_trades"])
        if r["total_return"] > 0:
            pos += 1
    return {
        "positive_folds": pos, "n_folds": len(fold_records),
        "mean_total_return": round(float(np.mean(rets)), 4) if rets else None,
        "mean_sharpe": round(float(np.mean(shps)), 3) if shps else None,
        "mean_max_drawdown": round(float(np.mean(mdds)), 4) if mdds else None,
        "mean_win_rate": round(float(np.mean(wins)), 4) if wins else None,
        "mean_n_trades": round(float(np.mean(ntr)), 1) if ntr else None,
    }


def main():
    t0 = time.time()
    print("[regime-ens] regime 干净归因(2x2) + 因子 ensemble canonical replay 启动", flush=True)
    symbols = select_universe()
    print(f"[regime-ens] universe = {len(symbols)} 只 (seed={SEED})", flush=True)
    folds = fold_dates()
    print(f"[regime-ens] 折叠: " + ", ".join(
        f"{fid}:{te0}~{te1}" for fid, _, _, te0, te1 in folds), flush=True)

    fold_records = []
    for fid, tr0, tr1, te0, te1 in folds:
        print(f"\n===== {fid} 测试段 {te0}~{te1} =====", flush=True)
        row = {"fold": fid, "train": f"{tr0}~{tr1}", "test": f"{te0}~{te1}", "runs": []}
        for cfg in CONFIGS:
            m, err = run_engine(cfg, te0, te1, symbols)
            if err:
                print(f"  [FAIL] {cfg['key']}: {err}", flush=True)
                row["runs"].append({"key": cfg["key"], "error": str(err)})
                continue
            print(f"  {cfg['key']:<14} ret={pct(m['total_return'])} Sharpe={m['sharpe']:+.2f} "
                  f"MDD={pct(m['max_drawdown'])} 胜率={pct(m['win_rate'])} 笔数={m['n_trades']}", flush=True)
            row["runs"].append({"key": cfg["key"], **m})
        fold_records.append(row)

    agg = {cfg["key"]: {**_agg_for(cfg["key"], fold_records), "note": cfg["note"]} for cfg in CONFIGS}

    # ── 干净归因（action 效应 / signal 效应，针对均值收益/Sharpe/MDD）──
    def g(k, f):
        return agg[k][f]
    def delta(a, b, f):
        return round(g(a, f) - g(b, f), 4) if g(a, f) is not None and g(b, f) is not None else None
    attribution = {
        "action_effect_leader": {  # 同 leader 信号下，switch - flat
            "mean_total_return": delta("switch_leader", "flat_leader", "mean_total_return"),
            "mean_sharpe": delta("switch_leader", "flat_leader", "mean_sharpe"),
            "mean_max_drawdown": delta("switch_leader", "flat_leader", "mean_max_drawdown"),
        },
        "action_effect_ew": {  # 同 ew 信号下，switch - flat
            "mean_total_return": delta("switch_ew", "flat_ew", "mean_total_return"),
            "mean_sharpe": delta("switch_ew", "flat_ew", "mean_sharpe"),
            "mean_max_drawdown": delta("switch_ew", "flat_ew", "mean_max_drawdown"),
        },
        "signal_effect_flat": {  # 同 flat 处置下，leader - ew
            "mean_total_return": delta("flat_leader", "flat_ew", "mean_total_return"),
            "mean_sharpe": delta("flat_leader", "flat_ew", "mean_sharpe"),
            "mean_max_drawdown": delta("flat_leader", "flat_ew", "mean_max_drawdown"),
        },
        "signal_effect_switch": {  # 同 switch 处置下，leader - ew
            "mean_total_return": delta("switch_leader", "switch_ew", "mean_total_return"),
            "mean_sharpe": delta("switch_leader", "switch_ew", "mean_sharpe"),
            "mean_max_drawdown": delta("switch_leader", "switch_ew", "mean_max_drawdown"),
        },
        "action_effect_ew20": {  # 同 ew MA20 信号下，switch - flat（响应式信号，干净动作效应）
            "mean_total_return": delta("switch_ew20", "flat_ew20", "mean_total_return"),
            "mean_sharpe": delta("switch_ew20", "flat_ew20", "mean_sharpe"),
            "mean_max_drawdown": delta("switch_ew20", "flat_ew20", "mean_max_drawdown"),
        },
    }

    out = {
        "config": {
            "description": "canonical universe 上的 regime/ensemble 历史 walk-forward 复验（非 fresh OOS）",
            "universe": len(symbols), "seed": SEED, "n_folds": N_FOLDS,
            "universe_selection": "deduplicate + lexicographic symbol sort + random.Random(seed).sample",
            "universe_sha256": symbols_sha256(symbols),
            "universe_manifest": universe_manifest(
                symbols,
                seed=SEED,
                requested_size=N_SYM,
                start=FULL0,
                end=FULL1,
            ),
            "factor": MOM_TREND, "overrides": BASE_OVERRIDES,
            "note": "同一次运行内 8 配置可控对比；regime 信号源分 leader(引擎龙头指数) / ew(universe等权指数)；"
                    "因子 ensemble=6 语义动量因子横截面归一等权平均。F1-F4 已参与早期研究选择，"
                    "本轮用于 deterministic replay，不得重新宣称 fresh OOS。",
        },
        "folds": fold_records,
        "aggregate": agg,
        "attribution": attribution,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s | 结果 {OUT} ==", flush=True)
    print("\n跨区间聚合：", flush=True)
    for key, a in agg.items():
        if a is None or a.get("mean_total_return") is None:
            print(f"  {key:<14} [NO DATA / FAILED]", flush=True)
            continue
        print(f"  {key:<14} +{a['positive_folds']}/{a['n_folds']} "
              f"meanRet={pct(a['mean_total_return'])} meanSharpe={a['mean_sharpe']:+.2f} "
              f"meanMDD={pct(a['mean_max_drawdown'])}", flush=True)
    print("\n归因（switch-flat / leader-ew）：", flush=True)
    for k, v in attribution.items():
        if v.get("mean_total_return") is None:
            print(f"  {k:<20} [N/A]", flush=True)
            continue
        print(f"  {k:<20} dRet={pct(v['mean_total_return'])} dSharpe={v['mean_sharpe']:+.2f} "
              f"dMDD={pct(v['mean_max_drawdown'])}", flush=True)


if __name__ == "__main__":
    main()
