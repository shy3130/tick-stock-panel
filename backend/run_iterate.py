"""迭代优化框架：在 2026-03-24~2026-06-24 区间持续迭代，目标样本内总收益 ≥ 100%。

主线逻辑（合法杠杆，无杠杆可用：引擎 max_exposure_pct 钳死在 1.0）：
  动量集中 —— 用 score_weight 把资金压到策略打分(动量为核心)最高的 1~N 只，
  配合拉长持仓让 AI 硬件真龙头主升充分展开。这是对"趋势/动量"策略最自然的增强，
  而非马后炮选股。

每轮迭代都记录：调整内容 + 收益/胜率/最大回撤/Sharpe/Sortino/盈亏比，以及相对上一轮的变化。
达到 ≥100% 后继续少量迭代以确认/微调，然后输出报告。

用法:
  cd backend
  TICKFLOW_BACKTEST_MODE=inprocess ../.venv/Scripts/python.exe run_iterate.py
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backtest.strategy import StrategyBacktestConfig  # noqa: E402
from app.backtest.worker import make_worker_task, run_worker_task  # noqa: E402
from app.config import settings  # noqa: E402

START, END = date(2026, 3, 24), date(2026, 6, 24)
TARGET = 1.0  # 100%

# 迭代计划：每一项都是一次可复现的配置变更。
# 字段: phase, label, change, sid, params, overrides, max_positions, position_sizing
ITER = [
    # ---- 阶段0: 锚点 (已知基线) ----
    dict(phase="0-锚点", label="bullish_alignment 默认(mp=10, equal)",
         change="以已知最优基线 +58.6% 作锚点", sid="bullish_alignment",
         params=None, overrides=None, max_positions=10, position_sizing="equal"),

    # ---- 阶段1: bullish_alignment 动量集中 (score_weight) ----
    dict(phase="1-集中", label="bullish mp=5 score_weight",
         change="仓位由10降到5，按动量打分加权(集中到前5强)", sid="bullish_alignment",
         params=None, overrides=None, max_positions=5, position_sizing="score_weight"),
    dict(phase="1-集中", label="bullish mp=3 score_weight",
         change="仓位降到3，进一步集中到动量前3", sid="bullish_alignment",
         params=None, overrides=None, max_positions=3, position_sizing="score_weight"),
    dict(phase="1-集中", label="bullish mp=2 score_weight",
         change="仓位降到2，集中到动量前2", sid="bullish_alignment",
         params=None, overrides=None, max_positions=2, position_sizing="score_weight"),
    dict(phase="1-集中", label="bullish mp=1 score_weight",
         change="仓位降到1，全部压到动量最强单只", sid="bullish_alignment",
         params=None, overrides=None, max_positions=1, position_sizing="score_weight"),
    dict(phase="1-集中", label="bullish mp=1 score_weight hold=20",
         change="单只集中 + 持仓上限20天(让龙头主升充分展开)", sid="bullish_alignment",
         params=None, overrides={"max_hold_days": 20}, max_positions=1, position_sizing="score_weight"),
    dict(phase="1-集中", label="bullish mp=1 score_weight hold=30",
         change="单只集中 + 持仓上限30天(极端让赢家奔跑)", sid="bullish_alignment",
         params=None, overrides={"max_hold_days": 30}, max_positions=1, position_sizing="score_weight"),

    # ---- 阶段2: pullback_to_support 动量集中 ----
    dict(phase="2-集中", label="pullback mp=5 score_weight",
         change="缩量回踩同样按动量打分加权，仓位5", sid="pullback_to_support",
         params=None, overrides=None, max_positions=5, position_sizing="score_weight"),
    dict(phase="2-集中", label="pullback mp=3 score_weight",
         change="缩量回踩仓位降到3", sid="pullback_to_support",
         params=None, overrides=None, max_positions=3, position_sizing="score_weight"),
    dict(phase="2-集中", label="pullback mp=1 score_weight",
         change="缩量回踩单只集中", sid="pullback_to_support",
         params=None, overrides=None, max_positions=1, position_sizing="score_weight"),
    dict(phase="2-集中", label="pullback mp=1 score_weight hold=20",
         change="缩量回踩单只 + 持仓20天", sid="pullback_to_support",
         params=None, overrides={"max_hold_days": 20}, max_positions=1, position_sizing="score_weight"),

    # ---- 阶段3: 组合(ensemble) —— 两趋势策略信号合并后集中 ----
    dict(phase="3-组合", label="bullish+pullback 合并 mp=2 score_weight",
         change="两趋势策略信号取并集，每期最多持2只、按动量打分加权(分散+集中)",
         sid="bullish_alignment", params=None, overrides=None,
         max_positions=2, position_sizing="score_weight", ensemble_with="pullback_to_support"),
]


def build_config(sid, params, overrides, max_positions, position_sizing):
    return StrategyBacktestConfig(
        strategy_id=sid, symbols=None, start=START, end=END,
        params=params, overrides=overrides, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=max_positions, max_exposure_pct=1.0, initial_capital=1_000_000.0,
        position_sizing=position_sizing, mode="position", holding_days=5,
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


def run_one(sid, params, overrides, max_positions, position_sizing, ensemble_with=None):
    cfg = build_config(sid, params, overrides, max_positions, position_sizing)
    task = make_worker_task("backtest", settings.data_dir, cfg)
    try:
        res = run_worker_task(task)
        if res.get("error"):
            print(f"     [FAIL] {sid}: {res['error']}", flush=True)
            return {"error": res["error"], "summary": None}
        # 组合(ensemble): 再跑第二个策略, 等权合并两者每日净值曲线
        if ensemble_with:
            cfg2 = build_config(ensemble_with, params, overrides, max_positions, position_sizing)
            res2 = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg2))
            if res2.get("error"):
                print(f"     [FAIL-ensemble] {ensemble_with}: {res2['error']}", flush=True)
                return {"error": res2["error"], "summary": None}
            s = ensemble_summarize(res, res2)
            print(f"     [OK-ens] {sid}+{ensemble_with}: n={s['n_trades']} 胜率={s['win_rate']} "
                  f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}", flush=True)
            return {"error": None, "summary": s}
        s = summarize(res)
        print(f"     [OK]   {sid}: n={s['n_trades']} 胜率={s['win_rate']} "
              f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}", flush=True)
        return {"error": None, "summary": s}
    except Exception as e:  # noqa: BLE001
        print(f"     [EXC]  {sid}: {e}", flush=True)
        return {"error": f"{type(e).__name__}: {e}", "summary": None,
                "traceback": traceback.format_exc()}


def _eq_values(res):
    eq = res.get("equity_curve") or []
    return [e.get("value") for e in eq if e.get("value") is not None]


def ensemble_summarize(resA, resB):
    """两个策略等权合并每日净值曲线 (策略级 ensemble)。"""
    va, vb = _eq_values(resA), _eq_values(resB)
    n = min(len(va), len(vb))
    va, vb = va[:n], vb[:n]
    if n == 0 or va[0] == 0 or vb[0] == 0:
        return {"n_trades": 0, "win_rate": None, "total_return": None,
                "max_drawdown": None, "sharpe": None, "sortino": None,
                "profit_factor": None, "final_equity": None}
    combo = [(a / va[0] + b / vb[0]) / 2.0 for a, b in zip(va, vb)]  # 相对净值
    eq = [c * 1_000_000.0 for c in combo]
    total_return = combo[-1] - 1.0
    # 日收益
    rets = [(combo[t] / combo[t - 1] - 1.0) for t in range(1, n)]
    import statistics
    mu = statistics.mean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd * (252 ** 0.5)) if sd > 0 else 0.0
    down = [r for r in rets if r < 0]
    dsd = statistics.pstdev(down) if len(down) > 1 else 0.0
    sortino = (mu / dsd * (252 ** 0.5)) if dsd > 0 else 0.0
    # 回撤
    peak = combo[0]
    mdd = 0.0
    for c in combo:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1.0)
    tA = resA.get("trades") or []
    tB = resB.get("trades") or []
    n_tr = len(tA) + len(tB)
    wins = sum(1 for t in (tA + tB) if (t.get("pnl_pct") or 0) > 0)
    win_rate = wins / n_tr if n_tr else None
    return {"n_trades": n_tr, "win_rate": win_rate, "total_return": total_return,
            "max_drawdown": mdd, "sharpe": sharpe, "sortino": sortino,
            "profit_factor": None, "final_equity": eq[-1]}


def pct(x):
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—"


def main():
    out_dir = Path(__file__).resolve().parent.parent
    out_json = out_dir / "strategy_iterate.json"
    log_md = out_dir / "strategy_iterate_log.md"

    records = []
    best = {"iter": -1, "total_return": -9e9}
    reached = False

    def write_files():
        out_json.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        # markdown log
        L = ["# 策略迭代优化日志：2026-03-24 ~ 2026-06-24（目标样本内收益 ≥ 100%）", ""]
        L.append("> 区间背景：大盘上升段、AI 硬件主升浪（结构性行情：全市场中位 -9.3%、上涨占比仅 34%）。")
        L.append("> 主线：动量集中（score_weight 把资金压到打分最高 1~N 只）+ 拉长持仓让真龙头主升展开。")
        L.append("> 引擎 max_exposure_pct 钳死在 1.0，**无杠杆**；故 100% 只能靠极度集中 + 高确定性趋势。")
        L.append("> ⚠️ 以下均为样本内(in-sample)结果，高集中度=高方差=对这段行情过拟合风险高，实盘前务必 walk-forward。")
        L.append("")
        L.append("| # | 阶段 | 调整 | 仓位 | 加权 | 笔数 | 胜率 | 总收益 | 最大回撤 | Sharpe | Sortino | 盈亏比 | 相对上一轮 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        prev = None
        for i, r in enumerate(records):
            s = r["summary"]
            delta = "—"
            if prev and s and prev.get("summary"):
                d = (s["total_return"] or 0) - (prev["summary"]["total_return"] or 0)
                delta = f"{d:+.1%}"
            L.append(f"| {i} | {r['phase']} | {r['label']} | {r['max_positions']} | "
                     f"{r['position_sizing']} | {s['n_trades'] if s else '—'} | "
                     f"{pct(s['win_rate']) if s else '—'} | {pct(s['total_return']) if s else '—'} | "
                     f"{pct(s['max_drawdown']) if s else '—'} | {s['sharpe'] if s else '—'} | "
                     f"{s['sortino'] if s else '—'} | {s['profit_factor'] if s else '—'} | {delta} |")
            prev = r
        L.append("")
        if reached:
            L.append(f"✅ **已在迭代 #{best['iter']} 达到样本内收益 ≥ 100%**"
                     f"（{pct(best['total_return'])}），此后继续微调/组合以确认稳定性。")
        else:
            L.append(f"⚠️ 当前最高样本内收益为 {pct(best['total_return'])}（迭代 #{best['iter']}），"
                     f"尚未达到 100% 目标；高集中度已逼近引擎能力上限（无杠杆）。")
        L.append("")
        log_md.write_text("\n".join(L), encoding="utf-8")

    print(f"########## 迭代优化开始 | 目标样本内收益 ≥ {pct(TARGET)} ##########", flush=True)
    for i, it in enumerate(ITER):
        print(f"\n--- 迭代 #{i} [{it['phase']}] {it['label']} ---", flush=True)
        print(f"    变更: {it['change']}", flush=True)
        r = run_one(it["sid"], it["params"], it["overrides"],
                    it["max_positions"], it["position_sizing"],
                    ensemble_with=it.get("ensemble_with"))
        rec = {**it, "summary": r["summary"], "error": r["error"]}
        records.append(rec)
        if r["summary"]:
            tr = r["summary"]["total_return"] or 0
            if tr > best["total_return"]:
                best = {"iter": i, "total_return": tr}
            if tr >= TARGET and not reached:
                reached = True
                print(f"    🎯 达到目标! 样本内收益 {pct(tr)} (迭代 #{i})", flush=True)
        write_files()

    print("\n== 迭代完成 | 日志已写 | JSON:", out_json, "==", flush=True)
    print(f"   最佳样本内收益: {pct(best['total_return'])} (迭代 #{best['iter']}) | 达标: {reached}", flush=True)


if __name__ == "__main__":
    main()
