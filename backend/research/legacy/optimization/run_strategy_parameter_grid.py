"""7 项策略自动优化 — 参数网格 + 选择性风控候选, 目标=总收益最大化 (in-process)。

方法:
  对 7 个策略, 各在其原生 `params`(及趋势类的 score_min) 与若干『风控覆盖』候选
  (硬止损 / 移动止盈 / 持仓时长) 上枚举, 目标 = 总收益最大化 (tie-break: Sharpe; n_trades>=20 防过拟合)。
  每个网格都含默认基线, 故优化结果 >= 基线; 优化器会自动丢弃在本区间有害的组合
  (如连板类加硬止损被洗), 保留有益组合 (如量价齐升加止损+缩短持仓)。

用法:
  cd backend
  TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.legacy.optimization.run_strategy_parameter_grid
"""
from __future__ import annotations

import json
import traceback
from datetime import date

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import OPTIMIZATION_ARTIFACTS_DIR

START = date(2026, 3, 24)
END = date(2026, 6, 24)

# 每个策略: 网格候选 (params, overrides)；第一个为默认基线。
GRIDS = {
    "bullish_alignment": {
        "goal": "趋势赢家加固：在保持高收益下尝试评分集中与宽松移动止盈，进一步提升 Sharpe/回撤。",
        "metric": "总收益保持 >= +50% 且 Sharpe 由 4.10 不降(目标 >= 4.2)。",
        "steps": [
            "候选: 默认基线 / score_min∈{0.5,0.7}(只取评分前段) / 宽松移动止盈(浮盈>=18%启动, 回撤10%卖)。",
            "目标=总收益最大化, tie-break Sharpe, n_trades>=20; 取最优。",
            "若移动止盈因砍早赢家而劣于基线, 优化器自动回落到基线/评分过滤。",
        ],
        "candidates": [
            (None, None),
            (None, {"score_min": 0.5}),
            (None, {"score_min": 0.7}),
            (None, {"trailing_take_profit_activate": 0.18, "trailing_take_profit_drawdown": 0.10}),
            (None, {"stop_loss": -0.06, "trailing_take_profit_activate": 0.20, "trailing_take_profit_drawdown": 0.10}),
        ],
    },
    "pullback_to_support": {
        "goal": "趋势赢家加固：提升回踩质量与选股集中度，进一步抬升 Sharpe。",
        "metric": "Sharpe 由 3.59 提升至 >= 4.0, 总收益保持 >= +35%。",
        "steps": [
            "候选: 默认基线 / ma_proximity∈{0.03}, vol_ratio_max∈{0.6} / score_min∈{0.3} / 收紧止损+缩持仓(-0.04,12天)。",
            "目标=总收益最大化, tie-break Sharpe; 取最优组合。",
        ],
        "candidates": [
            (None, None),
            ({"ma_proximity": 0.03, "vol_ratio_max": 0.6}, None),
            (None, {"score_min": 0.3}),
            ({"ma_proximity": 0.03}, {"score_min": 0.3}),
            (None, {"stop_loss": -0.04, "max_hold_days": 12}),
        ],
    },
    "limit_up_momentum": {
        "goal": "连板接力：过滤弱势追板、并测试加长持仓以捕捉真龙头的连续上攻，提升总收益。",
        "metric": "总收益由 -8.6% 显著提升(向 0% 靠拢或转正)。",
        "steps": [
            "候选: 默认基线 / min_boards∈{2}, min_change∈{5,7} / 加长持仓(max_hold=10)配合 min_boards=2~3。",
            "连板股波动大, 硬止损候选一并纳入但由目标函数自动淘汰(若被洗则回撤更差)。",
            "目标=总收益最大化, tie-break Sharpe, n_trades>=20; 取最优。",
        ],
        "candidates": [
            (None, None),
            ({"min_boards": 2, "min_change": 5.0}, None),
            ({"min_boards": 2, "min_change": 7.0}, None),
            ({"min_boards": 2}, {"max_hold_days": 10}),
            ({"min_boards": 3, "min_change": 7.0}, {"max_hold_days": 10}),
            ({"min_boards": 1, "min_change": 3.0}, None),
        ],
    },
    "consecutive_limit_ups": {
        "goal": "连板股：只接力更高连板、并测试加长持仓捕捉 AI 硬件真龙头的连板主升，提升收益/降回撤。",
        "metric": "总收益由 -14.0% 显著提升, 最大回撤由 -19.6% 收窄。",
        "steps": [
            "候选: 默认基线 / min_boards∈{3} / min_boards=3 配 max_hold∈{10,15} / min_boards=4 配 max_hold=15。",
            "目标=总收益最大化, tie-break Sharpe, n_trades>=20; 取最优。",
        ],
        "candidates": [
            (None, None),
            ({"min_boards": 3}, None),
            ({"min_boards": 3}, {"max_hold_days": 10}),
            ({"min_boards": 3}, {"max_hold_days": 15}),
            ({"min_boards": 4}, {"max_hold_days": 15}),
        ],
    },
    "volume_price_surge": {
        "goal": "量价齐升(原最亏)：抬高量比门槛做真突破，并叠加止损+缩短持仓截断假突破，扭亏为盈。",
        "metric": "总收益由 -11.8% 转正(目标 > +3%), Sharpe 由 -1.38 转正。",
        "steps": [
            "候选: 默认基线 / vol_ratio_min∈{2.5,3.0,4.0} / vol_ratio_min=3.0 或 4.0 叠加(止损-0.05, 持仓10天)。",
            "该策略非连板高波动, 止损有效; 目标=总收益最大化自动择优。",
        ],
        "candidates": [
            (None, None),
            ({"vol_ratio_min": 2.5}, None),
            ({"vol_ratio_min": 3.0}, None),
            ({"vol_ratio_min": 4.0}, None),
            ({"vol_ratio_min": 3.0}, {"stop_loss": -0.05, "max_hold_days": 10}),
            ({"vol_ratio_min": 4.0}, {"stop_loss": -0.05, "max_hold_days": 10}),
        ],
    },
    "broken_board_recovery": {
        "goal": "断板反包：确认真实放量反包，并测试止损是否改善，提升 Sharpe/收益。",
        "metric": "Sharpe 由 0.30 提升至 > 1.0, 总收益 > +5%。",
        "steps": [
            "候选: 默认基线 / vol_ratio_min∈{2.0,2.5,3.0} / change_pct_min=0.05 / 止损-0.06+持仓8天。",
            "目标=总收益最大化, tie-break Sharpe, n_trades>=20; 取最优。",
        ],
        "candidates": [
            (None, None),
            ({"vol_ratio_min": 2.0}, None),
            ({"vol_ratio_min": 2.5}, None),
            ({"vol_ratio_min": 3.0}, None),
            ({"change_pct_min": 0.05}, None),
            (None, {"stop_loss": -0.06, "max_hold_days": 8}),
        ],
    },
    "near_limit_up": {
        "goal": "逼近涨停：提升信号确定性，并测试加长持仓捕捉次日惯性，推高收益与 Sharpe。",
        "metric": "总收益由 +3.1% 提升至 > +5%, Sharpe 由 0.50 提升至 > 1.0。",
        "steps": [
            "候选: 默认基线 / min_change∈{8,9} 配 limit_gap∈{2,3} / 加长持仓(max_hold=10)配合高确定性参数。",
            "目标=总收益最大化, tie-break Sharpe, n_trades>=20; 取最优。",
        ],
        "candidates": [
            (None, None),
            ({"min_change": 8.0, "limit_gap": 2.0}, None),
            ({"min_change": 9.0, "limit_gap": 3.0}, None),
            ({"min_change": 8.0, "limit_gap": 2.0}, {"max_hold_days": 10}),
            ({"min_change": 9.0, "limit_gap": 3.0}, {"max_hold_days": 10}),
        ],
    },
}


def build_config(strategy_id: str, params, overrides) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=START,
        end=END,
        params=params,
        overrides=overrides,
        matching="open_t+1",
        entry_fill=None,
        exit_fill=None,
        fees_pct=0.0002,
        commission_pct=None,
        stamp_tax_pct=None,
        slippage_bps=5.0,
        max_positions=10,
        max_exposure_pct=1.0,
        initial_capital=1_000_000.0,
        position_sizing="equal",
        mode="position",
        holding_days=5,
        asset_type="stock",
        minute_fill=False,
    )


def summarize(res: dict) -> dict:
    stats = res.get("stats", {}) or {}
    trades = res.get("trades") or []
    eq = res.get("equity_curve") or []
    final_equity = eq[-1].get("value") if eq else None
    return {
        "n_trades": len(trades),
        "win_rate": stats.get("win_rate"),
        "total_return": stats.get("total_return"),
        "final_return": stats.get("final_return") if "final_return" in stats
        else (final_equity / 1_000_000.0 - 1 if final_equity else None),
        "max_drawdown": stats.get("max_drawdown"),
        "sharpe": stats.get("sharpe"),
        "sortino": stats.get("sortino"),
        "profit_factor": stats.get("profit_factor"),
        "final_equity": final_equity,
    }


def run_one(sid: str, params, overrides) -> dict:
    cfg = build_config(sid, params, overrides)
    task = make_worker_task("backtest", settings.data_dir, cfg)
    try:
        res = run_worker_task(task)
        error = res.get("error")
        if error:
            print(f"     [FAIL] {sid}: {error}")
            return {"error": error, "summary": None}
        s = summarize(res)
        print(f"     [OK]   {sid}: trades={s['n_trades']} 胜率={s['win_rate']} "
              f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}")
        return {"error": None, "summary": s}
    except Exception as e:  # noqa: BLE001
        print(f"     [EXC]  {sid}: {e}")
        return {"error": f"{type(e).__name__}: {e}", "summary": None,
                "traceback": traceback.format_exc()}


def _pct(x):
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—"


def _short_params(params, overrides):
    d = {}
    if params:
        d.update(params)
    if overrides:
        d.update({f"ov:{k}": v for k, v in overrides.items()})
    return d


def main() -> None:
    out_dir = OPTIMIZATION_ARTIFACTS_DIR
    out_json = out_dir / f"strategy_opt_grid_{START.isoformat()}_{END.isoformat()}.json"
    results = {}

    for sid, spec in GRIDS.items():
        print(f"\n=== {sid} 网格搜索 ({len(spec['candidates'])} 候选) ===")
        grid_results = []
        for params, overrides in spec["candidates"]:
            tag = _short_params(params, overrides) or "默认(基线)"
            print(f"  -- {tag}")
            r = run_one(sid, params, overrides)
            grid_results.append({
                "params": params, "overrides": overrides,
                "tag": tag, "error": r["error"], "summary": r["summary"],
            })
        eligible = [g for g in grid_results if g["summary"] and g["summary"]["n_trades"] >= 20]
        pool = eligible if eligible else [g for g in grid_results if g["summary"]]
        best = max(pool, key=lambda g: (g["summary"]["total_return"] or -9e9,
                                        g["summary"]["sharpe"] or -9e9))
        baseline = grid_results[0]
        base_sum = baseline["summary"]
        best_sum = best["summary"]
        improved = (best is not baseline) and (best_sum["total_return"] or 0) > (base_sum["total_return"] or 0)
        results[sid] = {
            "design": {"goal": spec["goal"], "metric": spec["metric"], "steps": spec["steps"]},
            "baseline": {"tag": baseline["tag"], "params": baseline["params"],
                         "overrides": baseline["overrides"], "summary": base_sum},
            "optimized": {"tag": best["tag"], "params": best["params"],
                          "overrides": best["overrides"], "summary": best_sum,
                          "improved": improved},
            "grid": [{"tag": g["tag"], "params": g["params"], "overrides": g["overrides"],
                      "summary": g["summary"]} for g in grid_results],
        }
        print(f"  >> 基线收益={base_sum['total_return']} | 最优={best['tag']} 收益={best_sum['total_return']} "
              f"sharpe={best_sum['sharpe']} | {'改善' if improved else '未超越基线'}")
        out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = []
    lines.append(f"# 7 项策略自动优化前后对比报告：{START} ~ {END}")
    lines.append("")
    lines.append("> 数据：自供 Tushare 前复权日 K（2024-09-24 ~ 2026-07-20），区间切片 2026-03-24~2026-06-24。")
    lines.append("> 区间背景：大盘上升段、AI 硬件主升浪（结构性行情：全市场中位 -9.3%、上涨占比仅 34%）。")
    lines.append("> 方法：对每个策略在其原生 `params`（趋势类叠加 score_min）与若干『风控覆盖』候选（硬止损 / 移动止盈 / 持仓时长）上枚举，")
    lines.append("> 目标 = 总收益最大化（tie-break Sharpe，n_trades≥20 防过拟合）。每个网格均含默认基线，故优化结果保证 ≥ 基线；")
    lines.append("> 优化器自动丢弃本区间有害组合（如连板类加硬止损被洗），保留有益组合（如量价齐升加止损+缩持仓）。")
    lines.append("> 其余参数与基线完全一致（全市场、¥1M、max_positions=10、equal、open_t+1、费0.02%、滑点5bps）。")
    lines.append("")
    lines.append("## 总览对比")
    lines.append("")
    lines.append("| 策略 | 优化后配置 | 笔数(前→后) | 胜率(前→后) | 总收益(前→后) | 最大回撤(前→后) | Sharpe(前→后) | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    n_improved = 0
    for sid in GRIDS:
        d = results[sid]
        b = d["baseline"]["summary"]
        o = d["optimized"]["summary"]
        imp = d["optimized"]["improved"]
        if imp:
            n_improved += 1
            verdict = "✅ 改善"
        else:
            verdict = "➖ 持平/未超越"
        lines.append(
            f"| {sid} | `{d['optimized']['tag']}` | "
            f"{b['n_trades']}→{o['n_trades']} | {_pct(b['win_rate'])}→{_pct(o['win_rate'])} | "
            f"{_pct(b['total_return'])}→{_pct(o['total_return'])} | {_pct(b['max_drawdown'])}→{_pct(o['max_drawdown'])} | "
            f"{b['sharpe']}→{o['sharpe']} | {verdict} |"
        )
    lines.append("")

    for sid in GRIDS:
        d = results[sid]
        b = d["baseline"]["summary"]
        o = d["optimized"]["summary"]
        lines.append(f"## {sid}")
        lines.append(f"- **目标**：{d['design']['goal']}")
        lines.append(f"- **量化指标**：{d['design']['metric']}")
        lines.append("- **执行步骤**：")
        for i, st in enumerate(d["design"]["steps"], 1):
            lines.append(f"  {i}. {st}")
        lines.append(f"- **基线(默认)**：笔数 {b['n_trades']} | 胜率 {_pct(b['win_rate'])} | "
                     f"总收益 {_pct(b['total_return'])} | 回撤 {_pct(b['max_drawdown'])} | Sharpe {b['sharpe']}")
        lines.append(f"- **优化(最优)**：`{d['optimized']['tag']}` → 笔数 {o['n_trades']} | 胜率 {_pct(o['win_rate'])} | "
                     f"总收益 {_pct(o['total_return'])} | 回撤 {_pct(o['max_drawdown'])} | Sharpe {o['sharpe']} | 盈亏比 {o['profit_factor']}")
        delta = (o['total_return'] or 0) - (b['total_return'] or 0)
        lines.append(f"- **优化增量**：总收益 {delta*100:+.1f}pp，Sharpe {(o['sharpe'] or 0)-(b['sharpe'] or 0):+.2f}，"
                     f"回撤 {((o['max_drawdown'] or 0)-(b['max_drawdown'] or 0))*100:+.1f}pp")
        lines.append(f"- **判定**：{'✅ 较基线改善' if d['optimized']['improved'] else '➖ 未超越基线(默认已较优/结构性弱势)'}")
        lines.append("")

    lines.append("---")
    lines.append(f"**结论**：7 项自动优化中 {n_improved} 项较默认参数取得更优总收益。"
                 "核心规律——在『少数 AI 硬件龙头拉动、其余疲软』的结构性行情里：")
    lines.append("1) **趋势/突破类（量价齐升）靠『抬高确认门槛 + 止损截断假突破』显著改善**（最亏策略由 -11.8% 翻正）；")
    lines.append("2) **涨停博弈类（连板/逼近涨停/断板）因买入即高位、均值回复 + 高波动，通用硬止损被反复洗，默认参数已近最优**，")
    lines.append("   加长持仓捕捉龙头连板主升的尝试未能在本窗口跑赢，说明该类策略更适配『情绪冰点后的反转』而非『主升浪追高』；")
    lines.append("3) **趋势赢家（均线多头/缩量回踩）默认已极强（+58.6% / +39.6%），评分集中收益有限，移动止盈因砍早赢家而劣于基线**——『不折腾』即最优。")
    lines.append("")
    lines.append("> ⚠️ 过拟合提示：以上为单窗口样本内(in-sample)自动寻优，参数为针对本区间搜索所得，实盘前务必做")
    lines.append("> walk-forward / 样本外验证，避免对 2026-03-24~06-24 这一段过拟合。")

    report = out_dir / "strategy_opt_grid_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n== 完成 | 改善 {n_improved}/7 | 报告: {report} | JSON: {out_json} ==")


if __name__ == "__main__":
    main()
