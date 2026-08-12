"""B 方案：mom_trend 因子接入真实引擎 canonical historical replay。

复用 P9 的 400 只 universe（seed 20260723）+ 4 折测试段切分，把语义因子
mom_trend = MOM20 MA60_DEV SIGN MUL（站稳 MA60 才采信 20 日动量方向）经
custom_factor 策略接入 tickflow 真实 Polars 引擎，对每个 OOS 测试段做实盘式回测
（费用/滑点/风控全部生效），检验「因子级 OOS 正收益」能否在带风控的真实组合里活下来。

对照配置：
  - mom_trend          : 主因子（MA60 趋势确认动量），无 regime
  - mom20_raw          : 纯 20 日动量（去掉 MA60 确认），验证趋势过滤是否真加值
  - mom_trend_regime   : 主因子 + regime 软减仓(scale_existing)，看回撤能否压住
  - pullback_to_support: 既定强策略基准（score_weight, mp=5）
  - bullish_alignment  : 既定强策略基准（score_weight, mp=10）

用法: cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.factors.run_factor_engine_wf
"""

import json
import time
from datetime import date

import numpy as np
import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.common.universe import stable_symbol_sample, universe_manifest
from research.paths import DATA_DIR, FACTOR_ARTIFACTS_DIR

OUT = FACTOR_ARTIFACTS_DIR / "strategy_factor_engine_wf.json"

# ── 与 P9 (run_factor_walkforward.py) 完全一致的 universe / 切分，保证因子级与引擎级口径可比 ──
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

# (key, strategy_id, factor_formula|None, regime_filter|None, max_positions, position_sizing)
CONFIGS = [
    ("mom_trend", "custom_factor", MOM_TREND, None, 10, "score_weight"),
    ("mom20_raw", "custom_factor", "MOM20", None, 10, "score_weight"),
    ("mom_trend_regime", "custom_factor", MOM_TREND,
     {"type": "leader_index", "ma": 60, "mode": "soft", "bear_weight": 0.3, "scale_existing": True},
     10, "score_weight"),
    ("pullback_to_support", "pullback_to_support", None, None, 5, "score_weight"),
    ("bullish_alignment", "bullish_alignment", None, None, 10, "score_weight"),
]

CONFIG_NOTE = {
    "mom_trend": "主因子：MOM20·SIGN(MA60_DEV) 站稳长期趋势才采信动量",
    "mom20_raw": "对照：纯 20 日动量（无 MA60 趋势确认）",
    "mom_trend_regime": "主因子 + 龙头指数 regime 软减仓(scale_existing, bear=0.3)",
    "pullback_to_support": "既定强策略基准",
    "bullish_alignment": "既定强策略基准",
}


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
    """优先用引擎 stats；缺字段时从 equity_curve/trades 兜底计算。"""
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
    # 兜底
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


def run_engine(key, sid, formula, regime, mp, ps, start, end, symbols):
    params = {"factor_formula": formula} if formula else None
    cfg = StrategyBacktestConfig(
        strategy_id=sid, symbols=symbols,
        start=start, end=end, params=params, overrides=dict(BASE_OVERRIDES),
        matching="open_t+1", entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=mp, max_exposure_pct=1.0, initial_capital=1_000_000.0,
        position_sizing=ps, mode="position", holding_days=20, asset_type="stock",
        minute_fill=False, regime_filter=regime,
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    if res.get("error"):
        return None, res["error"]
    return derive_metrics(res), None


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    t0 = time.time()
    print("[engine-wf] mom_trend 接入真实引擎 canonical replay 启动", flush=True)
    symbols = select_universe()
    print(f"[engine-wf] universe = {len(symbols)} 只 (seed={SEED})", flush=True)
    folds = fold_dates()
    print("[engine-wf] 折叠: " + ", ".join(
        f"{fid}:{te0}~{te1}" for fid, _, _, te0, te1 in folds), flush=True)

    fold_records = []
    for fid, tr0, tr1, te0, te1 in folds:
        print(f"\n===== {fid} 测试段 {te0}~{te1} =====", flush=True)
        row = {"fold": fid, "train": f"{tr0}~{tr1}", "test": f"{te0}~{te1}", "runs": []}
        for (key, sid, formula, regime, mp, ps) in CONFIGS:
            m, err = run_engine(key, sid, formula, regime, mp, ps, te0, te1, symbols)
            if err:
                print(f"  [FAIL] {key}: {err}", flush=True)
                row["runs"].append({"key": key, "error": str(err)})
                continue
            print(f"  {key:<18} ret={pct(m['total_return'])} Sharpe={m['sharpe']:+.2f} "
                  f"MDD={pct(m['max_drawdown'])} 胜率={pct(m['win_rate'])} 笔数={m['n_trades']}", flush=True)
            row["runs"].append({"key": key, **m})
        fold_records.append(row)

    # ── 跨区间聚合 ──
    agg = {}
    for key, *_ in [(c[0],) for c in CONFIGS]:
        rets, shps, mdds, wins, ntr = [], [], [], [], []
        pos = 0
        for fr in fold_records:
            r = next((x for x in fr["runs"] if x.get("key") == key and "error" not in x), None)
            if not r:
                continue
            rets.append(r["total_return"])
            shps.append(r["sharpe"])
            mdds.append(r["max_drawdown"])
            wins.append(r["win_rate"])
            ntr.append(r["n_trades"])
            if r["total_return"] > 0:
                pos += 1
        agg[key] = {
            "note": CONFIG_NOTE[key],
            "positive_folds": pos,
            "n_folds": len(fold_records),
            "mean_total_return": round(float(np.mean(rets)), 4) if rets else None,
            "mean_sharpe": round(float(np.mean(shps)), 3) if shps else None,
            "mean_max_drawdown": round(float(np.mean(mdds)), 4) if mdds else None,
            "mean_win_rate": round(float(np.mean(wins)), 4) if wins else None,
            "mean_n_trades": round(float(np.mean(ntr)), 1) if ntr else None,
        }

    out = {
        "config": {
            "description": "mom_trend 接入真实引擎的 canonical historical replay（含费/滑点/风控）",
            "evidence_status": "canonical_historical_replay_not_fresh_oos",
            "universe": len(symbols), "seed": SEED,
            "n_folds": N_FOLDS, "factor": MOM_TREND,
            "universe_manifest": universe_manifest(
                symbols,
                seed=SEED,
                requested_size=N_SYM,
                start=FULL0,
                end=FULL1,
            ),
            "overrides": BASE_OVERRIDES,
            "note": "与 P9 同 universe/同 4 折测试段；这些区间已影响过后续研究选择，"
                    "本轮只能视为 deterministic replay。引擎 warmup 用段前历史，净值仅覆盖测试段。",
        },
        "folds": fold_records,
        "aggregate": agg,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s | 结果 {OUT} ==", flush=True)
    print("\n跨区间聚合（positive=正收益区间数）：", flush=True)
    for key, a in agg.items():
        print(f"  {key:<18} +{a['positive_folds']}/{a['n_folds']} "
              f"meanRet={pct(a['mean_total_return'])} meanSharpe={a['mean_sharpe']:+.2f} "
              f"meanMDD={pct(a['mean_max_drawdown'])}", flush=True)


if __name__ == "__main__":
    main()
