"""区间回测脚本 — 复用回测 worker 的 exact 代码路径 (in-process)。

对一组策略在指定日期区间跑真实回测, 结果同时输出:
  - backtest_range_<start>_<end>.json   (完整 stats + 配置 + 摘要)
  - backtest_range_report.md            (人类可读对比报告)

用法:
  cd backend
  TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.legacy.validation.run_structural_bull_range_replay
"""
from __future__ import annotations

import json
import traceback
from datetime import date

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import VALIDATION_ARTIFACTS_DIR

# ── 区间与策略 ───────────────────────────────────────────────
START = date(2026, 3, 24)
END = date(2026, 6, 24)

STRATEGIES = [
    "pullback_to_support",
    "bullish_alignment",
    "volume_price_surge",
    "limit_up_momentum",
    "consecutive_limit_ups",
    "near_limit_up",
    "broken_board_recovery",
]

# ── 回测参数 (与前端默认一致) ───────────────────────────────
def build_config(strategy_id: str) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,                 # 全市场
        start=START,
        end=END,
        params=None,
        overrides=None,
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


def _fmt(v):
    if isinstance(v, float):
        return round(v, 4)
    return v


def main() -> None:
    data_dir = settings.data_dir
    out_dir = VALIDATION_ARTIFACTS_DIR
    out_json = out_dir / f"backtest_range_{START.isoformat()}_{END.isoformat()}.json"

    results = {}
    print(f"== 区间回测: {START} ~ {END} | data_dir={data_dir} | 策略数={len(STRATEGIES)} ==")
    for sid in STRATEGIES:
        cfg = build_config(sid)
        task = make_worker_task("backtest", data_dir, cfg)
        try:
            res = run_worker_task(task)
            error = res.get("error")
            stats = res.get("stats", {}) or {}
            eq = res.get("equity_curve") or []
            trades = res.get("trades") or []
            final_equity = eq[-1].get("value") if eq else None
            bench = res.get("benchmark_curve") or []
            bench_ret = None
            if len(bench) >= 2 and bench[0].get("close") and bench[0]["close"] > 0:
                bench_ret = bench[-1]["close"] / bench[0]["close"] - 1
            results[sid] = {
                "error": error,
                "stats": stats,
                "config": res.get("config"),
                "strategy_info": res.get("strategy_info"),
                "elapsed_ms": res.get("elapsed_ms"),
                "summary": {
                    "n_trades": len(trades),
                    "final_equity": final_equity,
                    "benchmark_return": bench_ret,
                    "win_rate": stats.get("win_rate"),
                    "total_return": stats.get("total_return"),
                    "final_return_pct": (stats.get("final_return") if "final_return" in stats
                                          else (final_equity / cfg.initial_capital - 1 if final_equity else None)),
                    "max_drawdown": stats.get("max_drawdown"),
                    "sharpe": stats.get("sharpe"),
                    "sortino": stats.get("sortino"),
                    "profit_factor": stats.get("profit_factor"),
                    "equity_points": len(eq),
                    "per_symbol_count": len(res.get("per_symbol_stats") or []),
                },
                "top_trades": sorted(
                    [
                        {
                            "symbol": t.get("symbol"), "name": t.get("name"),
                            "entry_date": t.get("entry_date"), "exit_date": t.get("exit_date"),
                            "pnl_pct": t.get("pnl_pct"), "exit_reason": t.get("exit_reason"),
                        }
                        for t in trades
                    ],
                    key=lambda x: (x.get("pnl_pct") or -1e9), reverse=True,
                )[:10],
            }
            if error:
                print(f"[FAIL] {sid}: {error}")
            else:
                s = results[sid]["summary"]
                print(f"[OK]   {sid}: trades={s['n_trades']} 胜率={s['win_rate']} "
                      f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}")
        except Exception as e:  # noqa: BLE001
            results[sid] = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
            print(f"[EXC]  {sid}: {e}")

    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成, 结果已写入: {out_json} ==")

    # ── 生成对比报告 ──
    lines = []
    lines.append(f"# 区间回测报告：{START} ~ {END}")
    lines.append("")
    lines.append("> 数据：自供 Tushare 前复权日 K（2024-09-24 ~ 2026-07-20）。")
    lines.append("> 区间背景：用户指定为大盘上升段、AI 硬件主升浪。")
    lines.append("> 参数：全市场、初始资金 ¥1,000,000、max_positions=10、持仓=equal、")
    lines.append("> matching=open_t+1、手续费 0.02%、滑点 5bps、holding_days=5。")
    lines.append("")
    lines.append("| 策略 | 笔数 | 胜率 | 总收益 | 最大回撤 | Sharpe | 基准(上证)收益 | 超额 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for sid in STRATEGIES:
        r = results.get(sid, {})
        if r.get("error") and "summary" not in r:
            lines.append(f"| {sid} | — | — | — | — | — | — | 错误: {r['error']} |")
            continue
        s = r.get("summary", {})
        tr = s.get("total_return")
        br = s.get("benchmark_return")
        excess = (tr - br) if (tr is not None and br is not None) else None
        def pct(x):
            return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—"
        lines.append(
            f"| {sid} | {s.get('n_trades','—')} | {pct(s.get('win_rate'))} | "
            f"{pct(tr)} | {pct(s.get('max_drawdown'))} | {s.get('sharpe','—')} | "
            f"{pct(br)} | {pct(excess)} |"
        )
    lines.append("")
    # 逐策略明细 + Top 交易
    for sid in STRATEGIES:
        r = results.get(sid, {})
        lines.append(f"## {sid}")
        if r.get("error") and "summary" not in r:
            lines.append(f"- 错误：{r['error']}")
            lines.append("")
            continue
        s = r.get("summary", {})
        lines.append(f"- 笔数：{s.get('n_trades')}　胜率：{s.get('win_rate')}　总收益：{s.get('total_return')}")
        lines.append(f"- 最大回撤：{s.get('max_drawdown')}　Sharpe：{s.get('sharpe')}　"
                     f"Sortino：{s.get('sortino')}　盈亏比：{s.get('profit_factor')}")
        lines.append(f"- 期末净值：{s.get('final_equity')}　基准(上证)收益：{s.get('benchmark_return')}")
        top = r.get("top_trades", [])
        if top:
            lines.append("- 收益最高 Top 5：")
            for t in top[:5]:
                lines.append(f"  - {t.get('symbol')} {t.get('name')}：{t.get('pnl_pct')} "
                             f"({t.get('entry_date')} → {t.get('exit_date')}, {t.get('exit_reason')})")
        lines.append("")

    report = out_dir / "backtest_range_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"== 报告已写入: {report} ==")


if __name__ == "__main__":
    main()
