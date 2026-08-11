"""
多段滚动 walk-forward（样本外）验证。
核心问题：pullback_to_support mp=1 score_weight 在 2026-03-24~2026-06-24 跑出 +108.7%，
这是"单只集中"的高过拟合形态。本脚本把它直接套到 8 个不同市场段做 OOS 测试，
不段内寻优，判定它到底是能力还是运气（对这段 AI 硬件主升浪过拟合）。

每个窗口独立：start 之前约 120 交易日用于指标预热（warmup），故信号不失真。
对照配置：
  - pullback_mp1_sw  : 待验证的高集中配置（原始窗口 +108.7%）
  - pullback_mp5_sw  : 稳健增强（原始窗口 +42.7%）
  - bullish_mp10_eq  : 稳健基准（原始窗口 +58.6%, Sharpe 4.10, 回撤 -9.9%）

用法: cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.legacy.validation.run_concentrated_pullback_multiperiod_replay
"""
import json
import traceback

from app.config import settings
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from research.paths import VALIDATION_ARTIFACTS_DIR

# ---- 8 个滚动窗口（window≈3个月, step≈2个月, 含原始窗口）----
WINDOWS = [
    ("2025-03-24", "2025-06-24"),
    ("2025-05-24", "2025-08-24"),
    ("2025-07-24", "2025-10-24"),
    ("2025-09-24", "2025-12-24"),
    ("2025-11-24", "2026-02-24"),
    ("2026-01-24", "2026-04-24"),
    ("2026-03-24", "2026-06-24"),   # 原始窗口（已知 pullback_mp1 = +108.7%）
    ("2026-05-24", "2026-07-20"),
]

CONFIGS = [
    {"tag": "pullback_mp1_sw", "sid": "pullback_to_support",
     "params": None, "overrides": None,
     "max_positions": 1, "position_sizing": "score_weight",
     "desc": "单只集中(待验证)"},
    {"tag": "pullback_mp5_sw", "sid": "pullback_to_support",
     "params": None, "overrides": None,
     "max_positions": 5, "position_sizing": "score_weight",
     "desc": "稳健增强"},
    {"tag": "bullish_mp10_eq", "sid": "bullish_alignment",
     "params": None, "overrides": None,
     "max_positions": 10, "position_sizing": "equal",
     "desc": "稳健基准"},
]


def build_config(sid, params, overrides, start, end, max_positions, position_sizing):
    return StrategyBacktestConfig(
        strategy_id=sid, symbols=None, start=start, end=end,
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


def run_one(c, start, end):
    from datetime import date as _date
    sd = _date.fromisoformat(start) if isinstance(start, str) else start
    ed = _date.fromisoformat(end) if isinstance(end, str) else end
    cfg = build_config(c["sid"], c["params"], c["overrides"], sd, ed,
                       c["max_positions"], c["position_sizing"])
    task = make_worker_task("backtest", settings.data_dir, cfg)
    try:
        res = run_worker_task(task)
        if res.get("error"):
            print(f"     [FAIL] {c['tag']} {start}~{end}: {res['error']}", flush=True)
            return {"error": res["error"], "summary": None}
        s = summarize(res)
        print(f"     [OK]   {c['tag']} {start}~{end}: n={s['n_trades']} 胜率={s['win_rate']} "
              f"收益={s['total_return']} 回撤={s['max_drawdown']} sharpe={s['sharpe']}", flush=True)
        return {"error": None, "summary": s}
    except Exception as e:  # noqa: BLE001
        print(f"     [EXC]  {c['tag']} {start}~{end}: {e}", flush=True)
        return {"error": f"{type(e).__name__}: {e}", "summary": None,
                "traceback": traceback.format_exc()}


def pct(x):
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—"


def main():
    out_dir = VALIDATION_ARTIFACTS_DIR
    out_json = out_dir / "strategy_walkforward.json"
    log_md = out_dir / "strategy_walkforward_report.md"

    # ---- sanity: 原窗口 pullback_mp1 应 ≈ +108.7% ----
    print("=== SANITY: 原窗口 pullback_mp1 ===", flush=True)
    sanity = run_one(CONFIGS[0], "2026-03-24", "2026-06-24")
    if sanity.get("summary"):
        r = sanity["summary"]["total_return"]
        print(f"   sanity 收益 = {pct(r)} (期望 ≈ +108.7%)", flush=True)
        if r is None or r < 0.5:
            print("   ⚠️ sanity 异常，停止全量以免框架错误", flush=True)
            return

    records = []
    for (s, e) in WINDOWS:
        print(f"\n=== 窗口 {s} ~ {e} ===", flush=True)
        row = {"window": f"{s}~{e}", "start": s, "end": e, "configs": {}}
        for c in CONFIGS:
            r = run_one(c, s, e)
            row["configs"][c["tag"]] = {
                "desc": c["desc"], "error": r["error"], "summary": r["summary"],
            }
        records.append(row)

    # ---- 写 JSON ----
    out_json.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")

    # ---- 计算判定 ----
    def col(tag):
        vals = []
        for row in records:
            sm = row["configs"].get(tag, {}).get("summary")
            if sm and sm.get("total_return") is not None:
                vals.append(sm["total_return"])
        return vals

    mp1 = col("pullback_mp1_sw")
    mp5 = col("pullback_mp5_sw")
    bu = col("bullish_mp10_eq")

    def stats(vals):
        if not vals:
            return (None, None, 0, 0)
        import statistics
        mean = statistics.mean(vals)
        med = statistics.median(vals)
        npos = sum(1 for v in vals if v > 0)
        return (mean, med, npos, len(vals))

    mp1s = stats(mp1); mp5s = stats(mp5); bus = stats(bu)

    # ---- 写 markdown ----
    L = ["# 多段滚动 Walk-Forward 验证报告", ""]
    L.append("> **目的**：判定 `pullback_to_support` mp=1 单只集中（+108.7% 样本内）是**能力**还是对 AI 硬件主升浪的**过拟合**。")
    L.append("> **方法**：把原始窗口最优配置**直接套到 8 个不同市场段**做样本外(OOS)测试，**不段内寻优**。每段 start 前约 120 交易日预热，信号不失真。")
    L.append("> **对照**：pullback mp=5 集中（稳健增强）、bullish mp=10 等权（稳健基准）。原始窗口(2026-03-24~2026-06-24)作为第 7 段复现。")
    L.append("")
    L.append("## 一、各段总收益矩阵（%）")
    L.append("")
    L.append("| 窗口 | pullback_mp1 (待验证) | pullback_mp5 (稳健增强) | bullish_mp10 (基准) |")
    L.append("|---|---|---|---|")
    for row in records:
        def g(tag):
            sm = row["configs"].get(tag, {}).get("summary")
            return pct(sm["total_return"]) if sm and sm.get("total_return") is not None else ("ERR" if row["configs"].get(tag, {}).get("error") else "—")
        L.append(f"| {row['window']} | {g('pullback_mp1_sw')} | {g('pullback_mp5_sw')} | {g('bullish_mp10_eq')} |")
    L.append("")
    L.append("## 二、详细指标（每段 × 配置）")
    L.append("")
    for row in records:
        L.append(f"### 窗口 {row['window']}")
        L.append("")
        L.append("| 配置 | 笔数 | 胜率 | 总收益 | 最大回撤 | Sharpe |")
        L.append("|---|---|---|---|---|---|")
        for c in CONFIGS:
            sm = row["configs"].get(c["tag"], {}).get("summary")
            if sm:
                L.append(f"| {c['tag']} ({c['desc']}) | {sm['n_trades']} | {pct(sm['win_rate']) if sm['win_rate'] is not None else '—'} | "
                         f"{pct(sm['total_return'])} | {pct(sm['max_drawdown'])} | {sm['sharpe']} |")
            else:
                L.append(f"| {c['tag']} | — | — | — | — | — |")
        L.append("")
    L.append("## 三、跨段统计与判定")
    L.append("")
    L.append(f"- **pullback_mp1（待验证）**：均值 {pct(mp1s[0]) if mp1s[0] else '—'}，中位数 {pct(mp1s[1]) if mp1s[1] else '—'}，"
             f"正收益段 {mp1s[2]}/{mp1s[3]}")
    L.append(f"- **pullback_mp5（稳健增强）**：均值 {pct(mp5s[0]) if mp5s[0] else '—'}，中位数 {pct(mp5s[1]) if mp5s[1] else '—'}，"
             f"正收益段 {mp5s[2]}/{mp5s[3]}")
    L.append(f"- **bullish_mp10（基准）**：均值 {pct(bus[0]) if bus[0] else '—'}，中位数 {pct(bus[1]) if bus[1] else '—'}，"
             f"正收益段 {bus[2]}/{bus[3]}")
    L.append("")
    # 判定
    orig = records[6]["configs"]["pullback_mp1_sw"]["summary"]
    orig_ret = orig["total_return"] if (orig and orig.get("total_return") is not None) else 0.0
    other_mp1 = [v for i, v in enumerate(mp1) if i != 6] if len(mp1) > 7 else list(mp1)
    other_mean = (sum(other_mp1)/len(other_mp1)) if other_mp1 else None
    L.append("### 判定")
    L.append("")
    if orig_ret and orig_ret > 0.8 and (other_mean is None or other_mean < 0.3):
        L.append(f"❌ **过拟合（运气）**：原始窗口爆发 +{orig_ret*100:.0f}%，但其余 {len(other_mp1)} 段均值仅 "
                 f"{pct(other_mean)}——收益几乎完全来自这一段 AI 硬件主升浪，换段时间无法复现。")
    elif other_mean is not None and other_mean > 0.4:
        L.append(f"✅ **具备泛化能力**：原始窗口 +{orig_ret*100:.0f}% 之外，其余段均值仍达 {pct(other_mean)}，多段稳定为正。")
    else:
        L.append(f"⚠️ **结论偏中性**：原始窗口 +{orig_ret*100:.0f}%，其余段均值 {pct(other_mean)}，需更多历史段确认。")
    L.append("")
    L.append("> 注：bullish_mp10 作为稳健基准，若其多段稳定为正、而 pullback_mp1 大起大落，进一步佐证后者方差来自单只集中而非策略本身。")
    L.append("")
    log_md.write_text("\n".join(L), encoding="utf-8")
    print("\n== 完成 | 报告已写 ==", flush=True)
    print(f"pullback_mp1 跨段: 均值={pct(mp1s[0])} 中位={pct(mp1s[1])} 正段={mp1s[2]}/{mp1s[3]}", flush=True)
    print(f"bullish_mp10 跨段: 均值={pct(bus[0])} 中位={pct(bus[1])} 正段={bus[2]}/{bus[3]}", flush=True)


if __name__ == "__main__":
    main()
