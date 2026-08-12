"""7 策略深度优化 + walk-forward 样本外验证 (in-process)。

两路并行：
  A) 全窗口(2026-03-24~2026-06-24) 更深网格搜索 —— 在 v1 最优解附近细搜,
     给未改善的 broken_board_recovery 补候选, 给两趋势赢家试更长持仓/宽松移动止盈。
  B) walk-forward: 训练 2026-03-24~2026-05-15 上寻优, 测试 2026-05-16~2026-06-24 上验证,
     判断是否过拟合 (用训练集选出的最优直接上测试集 = 公平 OOS)。

目标 = 总收益最大化 (tie-break Sharpe, n_trades>=20 防过拟合)。每个网格含默认基线。

用法:
  cd backend
  TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.legacy.optimization.run_strategy_deep_grid_walkforward
"""
from __future__ import annotations

import json
import traceback
from datetime import date

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import OPTIMIZATION_ARTIFACTS_DIR

FULL_START, FULL_END = date(2026, 3, 24), date(2026, 6, 24)
TRAIN_START, TRAIN_END = date(2026, 3, 24), date(2026, 5, 15)
TEST_START, TEST_END = date(2026, 5, 16), date(2026, 6, 24)

# 更深网格 (每个列表第一项=默认基线)。格式: (params, overrides)
DEEP = {
    "bullish_alignment": [
        (None, None),
        (None, {"score_min": 0.3}),
        (None, {"score_min": 0.5}),
        (None, {"score_min": 0.7}),
        (None, {"score_min": 0.3, "max_hold_days": 10}),
        (None, {"max_hold_days": 12}),
        (None, {"max_hold_days": 15}),
        (None, {"trailing_take_profit_activate": 0.25, "trailing_take_profit_drawdown": 0.15}),
        (None, {"score_min": 0.3, "trailing_take_profit_activate": 0.30, "trailing_take_profit_drawdown": 0.15}),
    ],
    "pullback_to_support": [
        (None, None),
        ({"ma_proximity": 0.02}, None),
        ({"ma_proximity": 0.03}, None),
        ({"ma_proximity": 0.04}, None),
        ({"ma_proximity": 0.03, "vol_ratio_max": 0.6}, None),
        (None, {"score_min": 0.2}),
        (None, {"score_min": 0.3}),
        (None, {"max_hold_days": 10}),
        (None, {"max_hold_days": 12}),
        ({"ma_proximity": 0.03}, {"score_min": 0.3}),
        (None, {"stop_loss": -0.04, "max_hold_days": 12}),
    ],
    "limit_up_momentum": [
        (None, None),
        ({"min_boards": 2, "min_change": 5.0}, None),
        ({"min_boards": 2, "min_change": 6.0}, None),
        ({"min_boards": 2, "min_change": 7.0}, None),
        ({"min_boards": 3, "min_change": 7.0}, None),
        ({"min_boards": 1, "min_change": 3.0}, None),
        ({"min_boards": 2}, {"max_hold_days": 10}),
        ({"min_boards": 2, "min_change": 5.0}, {"max_hold_days": 12}),
        ({"min_boards": 2}, {"max_hold_days": 15}),
        ({"min_boards": 3}, {"max_hold_days": 12}),
        ({"min_boards": 1}, {"max_hold_days": 15}),
    ],
    "consecutive_limit_ups": [
        (None, None),
        ({"min_boards": 3}, None),
        ({"min_boards": 4}, None),
        ({"min_boards": 5}, None),
        ({"min_boards": 3}, {"max_hold_days": 10}),
        ({"min_boards": 3}, {"max_hold_days": 15}),
        ({"min_boards": 4}, {"max_hold_days": 15}),
        ({"min_boards": 4}, {"max_hold_days": 20}),
        ({"min_boards": 5}, {"max_hold_days": 15}),
    ],
    "volume_price_surge": [
        (None, None),
        ({"vol_ratio_min": 2.0}, None),
        ({"vol_ratio_min": 2.5}, None),
        ({"vol_ratio_min": 3.0}, None),
        ({"vol_ratio_min": 3.5}, None),
        ({"vol_ratio_min": 4.0}, None),
        ({"vol_ratio_min": 3.0}, {"stop_loss": -0.05, "max_hold_days": 10}),
        ({"vol_ratio_min": 3.5}, {"stop_loss": -0.05, "max_hold_days": 10}),
        ({"vol_ratio_min": 4.0}, {"stop_loss": -0.05, "max_hold_days": 10}),
        ({"vol_ratio_min": 3.0}, {"stop_loss": -0.04, "max_hold_days": 12}),
        ({"vol_ratio_min": 2.5}, {"stop_loss": -0.06, "max_hold_days": 8}),
    ],
    "broken_board_recovery": [
        (None, None),
        ({"vol_ratio_min": 1.0}, None),
        ({"vol_ratio_min": 1.5}, None),
        ({"vol_ratio_min": 2.0}, None),
        ({"vol_ratio_min": 2.5}, None),
        ({"vol_ratio_min": 3.0}, None),
        ({"vol_ratio_min": 3.5}, None),
        ({"change_pct_min": 0.02}, None),
        ({"change_pct_min": 0.05}, None),
        ({"change_pct_min": 0.07}, None),
        ({"require_limit_up": False}, None),
        ({"require_limit_up": False, "vol_ratio_min": 2.0}, None),
        ({"vol_ratio_min": 2.5}, {"max_hold_days": 8}),
        ({"vol_ratio_min": 2.0, "change_pct_min": 0.05}, {"stop_loss": -0.06, "max_hold_days": 10}),
        (None, {"stop_loss": -0.06, "max_hold_days": 8}),
    ],
    "near_limit_up": [
        (None, None),
        ({"min_change": 8.0, "limit_gap": 2.0}, None),
        ({"min_change": 9.0, "limit_gap": 3.0}, None),
        ({"min_change": 9.5, "limit_gap": 2.0}, None),
        ({"min_change": 9.0, "limit_gap": 4.0}, None),
        ({"min_change": 8.0, "limit_gap": 2.0}, {"max_hold_days": 10}),
        ({"min_change": 9.0, "limit_gap": 3.0}, {"max_hold_days": 10}),
        ({"min_change": 9.0, "limit_gap": 3.0}, {"max_hold_days": 12}),
        ({"min_change": 8.5, "limit_gap": 2.5}, {"max_hold_days": 15}),
    ],
}

STRATEGY_ORDER = list(DEEP.keys())

V1_BASELINE = {  # 用于报告连续对照 (v1 默认基线收益)
    "bullish_alignment": 0.586, "pullback_to_support": 0.396,
    "limit_up_momentum": -0.086, "consecutive_limit_ups": -0.140,
    "volume_price_surge": -0.118, "broken_board_recovery": 0.010,
    "near_limit_up": 0.031,
}


def build_config(sid, params, overrides, start, end) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=sid, symbols=None, start=start, end=end,
        params=params, overrides=overrides, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=10, max_exposure_pct=1.0, initial_capital=1_000_000.0,
        position_sizing="equal", mode="position", holding_days=5,
        asset_type="stock", minute_fill=False,
    )


def summarize(res):
    stats = res.get("stats", {}) or {}
    trades = res.get("trades") or []
    eq = res.get("equity_curve") or []
    final_equity = eq[-1].get("value") if eq else None
    return {
        "n_trades": len(trades), "win_rate": stats.get("win_rate"),
        "total_return": stats.get("total_return"),
        "final_return": stats.get("final_return") if "final_return" in stats
        else (final_equity / 1_000_000.0 - 1 if final_equity else None),
        "max_drawdown": stats.get("max_drawdown"), "sharpe": stats.get("sharpe"),
        "sortino": stats.get("sortino"), "profit_factor": stats.get("profit_factor"),
        "final_equity": final_equity,
    }


def run_one(sid, params, overrides, start, end):
    cfg = build_config(sid, params, overrides, start, end)
    task = make_worker_task("backtest", settings.data_dir, cfg)
    try:
        res = run_worker_task(task)
        if res.get("error"):
            print(f"     [FAIL] {sid}: {res['error']}", flush=True)
            return {"error": res["error"], "summary": None}
        s = summarize(res)
        print(f"     [OK]   {sid}: n={s['n_trades']} 胜率={s['win_rate']} "
              f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}", flush=True)
        return {"error": None, "summary": s}
    except Exception as e:
        print(f"     [EXC]  {sid}: {e}", flush=True)
        return {"error": f"{type(e).__name__}: {e}", "summary": None,
                "traceback": traceback.format_exc()}


def short_params(params, overrides):
    d = {}
    if params:
        d.update(params)
    if overrides:
        d.update({f"ov:{k}": v for k, v in overrides.items()})
    return d or "默认(基线)"


def pick_best(grid_results):
    eligible = [g for g in grid_results if g["summary"] and g["summary"]["n_trades"] >= 20]
    pool = eligible if eligible else [g for g in grid_results if g["summary"]]
    return max(pool, key=lambda g: (g["summary"]["total_return"] or -9e9,
                                    g["summary"]["sharpe"] or -9e9))


def run_grid(sid, start, end):
    print(f"\n=== {sid} 网格 ({len(DEEP[sid])} 候选) [{start}~{end}] ===", flush=True)
    results = []
    for params, overrides in DEEP[sid]:
        tag = short_params(params, overrides)
        print(f"  -- {tag}", flush=True)
        r = run_one(sid, params, overrides, start, end)
        results.append({"params": params, "overrides": overrides, "tag": tag,
                        "error": r["error"], "summary": r["summary"]})
    best = pick_best(results)
    base = results[0]
    return results, base, best


def pct(x):
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—"


def main():
    out_dir = OPTIMIZATION_ARTIFACTS_DIR
    out_json = out_dir / "strategy_opt_v2.json"

    full_deep = {}
    wf = {}

    # ---- A) 全窗口深度搜索 ----
    print("\n########## A) 全窗口深度优化 ##########", flush=True)
    for sid in STRATEGY_ORDER:
        results, base, best = run_grid(sid, FULL_START, FULL_END)
        full_deep[sid] = {
            "baseline": {"tag": base["tag"], "params": base["params"],
                         "overrides": base["overrides"], "summary": base["summary"]},
            "optimized": {"tag": best["tag"], "params": best["params"],
                          "overrides": best["overrides"], "summary": best["summary"],
                          "improved": (best is not base)
                          and (best["summary"]["total_return"] or 0) > (base["summary"]["total_return"] or 0)},
            "grid": [{"tag": g["tag"], "params": g["params"], "overrides": g["overrides"],
                      "summary": g["summary"]} for g in results],
        }
        out_json.write_text(json.dumps({"full_deep": full_deep, "walk_forward": wf},
                          ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- B) walk-forward: 训练集寻优 ----
    print("\n########## B) walk-forward (训练集寻优) ##########", flush=True)
    train_best_cfg = {}
    for sid in STRATEGY_ORDER:
        results, base, best = run_grid(sid, TRAIN_START, TRAIN_END)
        train_best_cfg[sid] = (best["params"], best["overrides"])
        wf[sid] = {
            "train_best": {"tag": best["tag"], "params": best["params"],
                           "overrides": best["overrides"], "summary": best["summary"]},
        }
        out_json.write_text(json.dumps({"full_deep": full_deep, "walk_forward": wf},
                          ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- B2) 测试集验证: 训练最优 / 全窗口最优 / 默认 ----
    print("\n########## B2) 测试集(OOS)验证 ##########", flush=True)
    for sid in STRATEGY_ORDER:
        fb = full_deep[sid]["optimized"]
        tp, to = train_best_cfg[sid]
        # 默认
        d = run_one(sid, None, None, TEST_START, TEST_END)
        # 训练最优
        t = run_one(sid, tp, to, TEST_START, TEST_END)
        # 全窗口最优 (in-sample, 仅作参考)
        f = run_one(sid, fb["params"], fb["overrides"], TEST_START, TEST_END)
        wf[sid].update({
            "test_default": d["summary"],
            "test_of_train_best": t["summary"],
            "test_of_full_best": f["summary"],
            "oos_holds": (t["summary"] and d["summary"]
                          and (t["summary"]["total_return"] or 0) > (d["summary"]["total_return"] or 0)),
        })
        out_json.write_text(json.dumps({"full_deep": full_deep, "walk_forward": wf},
                          ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- 报告 ----
    write_report(out_dir, full_deep, wf)
    print("\n== 完成 | 报告已写 | JSON:", out_json, "==", flush=True)


def write_report(out_dir, full_deep, wf):
    L = []
    L.append("# 7 策略深度优化 + walk-forward 样本外验证：2026-03-24 ~ 2026-06-24")
    L.append("")
    L.append("> 数据：自供 Tushare 前复权日 K（2024-09-24 ~ 2026-07-20），区间切片 2026-03-24~2026-06-24。")
    L.append("> 区间背景：大盘上升段、AI 硬件主升浪（结构性行情：全市场中位 -9.3%、上涨占比仅 34%）。")
    L.append("> 方法 A：对 7 策略在其原生 `params` + 风控 `overrides`（止损/移动止盈/持仓时长/score_min）上做**更深网格**（在 v1 最优解附近细搜，")
    L.append("> 给 broken_board_recovery 补候选、给两趋势赢家试更长持仓/宽松移动止盈）。目标=总收益最大化(tie-break Sharpe, n_trades≥20)。")
    L.append("> 方法 B：walk-forward 防过拟合——训练 2026-03-24~05-15 上寻优，测试 2026-05-16~06-24 上验证；")
    L.append("> 用训练集选出的最优直接上测试集=公平 OOS。另附「全窗口最优(in-sample)上测试集」作参考。")
    L.append("> 其余参数与基线一致（全市场、¥1M、max_positions=10、equal、open_t+1、费0.02%、滑点5bps）。")
    L.append("")
    L.append("## 一、全窗口深度优化（vs v1 默认基线）")
    L.append("")
    L.append("| 策略 | v1基线收益 | 深度最优配置 | 前→后收益 | 前→后Sharpe | 前→后回撤 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    n_imp = 0
    for sid in STRATEGY_ORDER:
        d = full_deep[sid]
        b, o = d["baseline"]["summary"], d["optimized"]["summary"]
        imp = d["optimized"]["improved"]
        n_imp += 1 if imp else 0
        L.append(f"| {sid} | {pct(V1_BASELINE[sid])} | `{d['optimized']['tag']}` | "
                 f"{pct(b['total_return'])}→{pct(o['total_return'])} | {b['sharpe']}→{o['sharpe']} | "
                 f"{pct(b['max_drawdown'])}→{pct(o['max_drawdown'])} | {'✅' if imp else '➖'} |")
    L.append("")
    L.append("## 二、walk-forward 样本外验证（训练 3/24–5/15 → 测试 5/16–6/24）")
    L.append("")
    L.append("| 策略 | 训练最优收益 | 测试默认收益 | 测试(训练最优)收益 | 测试(全窗口最优)收益 | OOS 是否成立 |")
    L.append("|---|---|---|---|---|---|")
    n_oos = 0
    for sid in STRATEGY_ORDER:
        w = wf[sid]
        tr = w["train_best"]["summary"]
        td = w["test_default"]
        tt = w["test_of_train_best"]
        tf = w["test_of_full_best"]
        holds = w["oos_holds"]
        n_oos += 1 if holds else 0
        L.append(f"| {sid} | {pct(tr['total_return'])} | {pct(td['total_return']) if td else '—'} | "
                 f"{pct(tt['total_return']) if tt else '—'} | {pct(tf['total_return']) if tf else '—'} | "
                 f"{'✅ 泛化' if holds else '⚠️ 未泛化/过拟合风险'} |")
    L.append("")
    L.append("## 三、逐项结论")
    L.append("")
    for sid in STRATEGY_ORDER:
        d = full_deep[sid]
        w = wf[sid]
        b, o = d["baseline"]["summary"], d["optimized"]["summary"]
        imp = d["optimized"]["improved"]
        holds = w["oos_holds"]
        L.append(f"### {sid}")
        L.append(f"- 深度优化：{'✅ 由 ' + pct(b['total_return']) + ' 提升至 ' + pct(o['total_return']) if imp else '➖ 未超越默认(' + pct(b['total_return']) + ')'}")
        L.append(f"  - 最优配置：`{d['optimized']['tag']}`")
        if imp:
            L.append(f"  - 增量：收益 {(o['total_return'] or 0)-(b['total_return'] or 0):+.1%}，"
                     f"Sharpe {(o['sharpe'] or 0)-(b['sharpe'] or 0):+.2f}，"
                     f"回撤 {((o['max_drawdown'] or 0)-(b['max_drawdown'] or 0)):+.1%}")
        tr = w["train_best"]["summary"]
        tt = w["test_of_train_best"]
        td = w["test_default"]
        L.append(f"- OOS 验证：训练收益 {pct(tr['total_return'])} → 测试(训练最优) {pct(tt['total_return']) if tt else '—'}"
                 f" / 测试默认 {pct(td['total_return']) if td else '—'} → {'✅ 泛化' if holds else '⚠️ 未泛化'}")
        L.append("")
    L.append("---")
    L.append(f"**总览**：全窗口深度优化 {n_imp}/7 改善；walk-forward 中 {n_oos}/7 在测试集上仍优于默认(泛化)。")
    L.append("**判读**：若某策略『深度优化改善』但『OOS 未泛化』，则其参数很可能是对 3/24–6/24 整段过拟合，")
    L.append("实盘应以训练集选出、且测试集验证通过的配置为准，或进一步做多段 walk-forward 确认稳定性。")
    L.append("")
    L.append("> ⚠️ 样本外测试窗口仅约 21 个交易日，样本量小，结论为方向性参考而非统计确证。")
    (out_dir / "strategy_opt_v2_report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
