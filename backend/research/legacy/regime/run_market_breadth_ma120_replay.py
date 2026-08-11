"""Legacy historical replay; not a current regime entrypoint.

P0+P1 落地：市场宽度(regime)过滤的回测层验证。
- 用全市场 close 算 MA120 宽度：每天站上 MA120 的个股占比 >50% 判为牛市(regime=多)，否则空仓。
- 同时算等权市场基准(equal-weight universe)作为 benchmark。
- 门控测试：把策略成交单中"入场日在熊市"的整笔剔除(那段空仓)，重算净值，对比 不过滤 vs 过滤。
- 严格不重新寻优：直接套用已知配置(mp=5 稳健 / mp=1 单只集中)，在 8 段 walk-forward 窗口 + 目标窗口上验证。
数据：全部来自 data/，无需重新拉取、不改策略源码。
"""
import json
import glob
import os
import time
from datetime import date as _date

import polars as pl

from app.config import settings
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from research.paths import DATA_DIR, REGIME_ARTIFACTS_DIR

WINDOWS = [
    ("2025-03-24", "2025-06-24"),
    ("2025-05-24", "2025-08-24"),
    ("2025-07-24", "2025-10-24"),
    ("2025-09-24", "2025-12-24"),
    ("2025-11-24", "2026-02-24"),
    ("2026-01-24", "2026-04-24"),
    ("2026-03-24", "2026-06-24"),
    ("2026-05-24", "2026-07-20"),
]

CACHE = DATA_DIR / ".regime_cache"
CACHE.mkdir(parents=True, exist_ok=True)
WIDE_PARQUET = CACHE / "wide_close.parquet"
RETS_PARQUET = CACHE / "wide_rets.parquet"


def build_wide():
    """返回 (wide_close, wide_rets) 都是 date x symbol 的 DataFrame，按 date 排序。"""
    if WIDE_PARQUET.exists() and RETS_PARQUET.exists():
        wide = pl.read_parquet(WIDE_PARQUET)
        rets = pl.read_parquet(RETS_PARQUET)
        return wide, rets
    files = glob.glob(str(DATA_DIR / "kline_daily_enriched/**/*.parquet"), recursive=True)
    frames = [pl.read_parquet(f).select("symbol", "date", "close") for f in files]
    alld = pl.concat(frames)
    wide = alld.pivot(values="close", index="date", on="symbol").sort("date")
    syms = [c for c in wide.columns if c != "date"]
    rets = wide.with_columns([(pl.col(c) / pl.col(c).shift(1) - 1).alias(c) for c in syms])
    wide.write_parquet(WIDE_PARQUET)
    rets.write_parquet(RETS_PARQUET)
    return wide, rets


def compute_regime(wide):
    """返回 (regime_dict: date->bool, breadth_df)。breadth = 站上 MA120 的占比。"""
    close_long = wide.unpivot(index="date", variable_name="symbol", value_name="close")
    close_long = close_long.sort(["symbol", "date"])
    close_long = close_long.with_columns(
        pl.col("close").rolling_mean(120).over("symbol", order_by="date").alias("ma120"))
    close_long = close_long.with_columns((pl.col("close") > pl.col("ma120")).alias("above"))
    br = close_long.group_by("date").agg(pl.col("above").mean().alias("breadth")).sort("date")
    regime = {row["date"]: (row["breadth"] is not None and row["breadth"] > 0.5)
              for row in br.iter_rows(named=True)}
    return regime, br


def benchmark_return(rets):
    """等权市场基准：每天对所有个股收益取均值(满仓)，复利。"""
    syms = [c for c in rets.columns if c != "date"]
    daily = rets.select(pl.mean_horizontal([pl.col(c) for c in syms]).alias("daily"))["daily"]
    eq = (1.0 + daily.fill_null(0.0)).cum_prod()
    return float(eq[-1] - 1.0)


def run_engine(sid, max_positions, position_sizing, start, end):
    cfg = StrategyBacktestConfig(
        strategy_id=sid, symbols=None,
        start=_date.fromisoformat(start), end=_date.fromisoformat(end),
        params=None, overrides=None, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=max_positions, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing=position_sizing,
        mode="position", holding_days=5, asset_type="stock", minute_fill=False,
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    if res.get("error"):
        return None, res["error"]
    return res.get("trades") or [], None


def simulate(trades, wide, rets, regime, gate, max_positions):
    """回测层门控重算。gate=True 时剔除入场日在熊市的成交单。返回 (总收益, n_used, n_dropped)。"""
    dates = wide["date"].to_list()
    date_pos = {d: i for i, d in enumerate(dates)}
    syms = [c for c in wide.columns if c != "date"]
    openw = {}
    ret_map = {}
    used = dropped = 0
    for t in trades:
        ed_raw = t.get("entry_date")
        xd_raw = t.get("exit_date")
        ed = _date.fromisoformat(ed_raw) if isinstance(ed_raw, str) else ed_raw
        xd = _date.fromisoformat(xd_raw) if isinstance(xd_raw, str) else xd_raw
        if ed not in date_pos or xd not in date_pos:
            continue
        bull = regime.get(ed, False)
        if gate and not bull:
            dropped += 1
            continue
        used += 1
        seg = rets.filter((pl.col("date") >= ed) & (pl.col("date") <= xd)).select("date", t["symbol"])
        dl, rl = seg["date"].to_list(), seg[t["symbol"]].to_list()
        for d, r in zip(dl, rl):
            ret_map[(d, t["symbol"])] = r if r is not None else 0.0
        w = t.get("position_pct") or (1.0 / max_positions)
        s, e = date_pos[ed], date_pos[xd]
        for i in range(s, e + 1):
            openw.setdefault(dates[i], []).append((t["symbol"], w))
    eq = 1.0
    for d in dates:
        c = 0.0
        for (sym, w) in openw.get(d, []):
            c += w * ret_map.get((d, sym), 0.0)
        eq *= (1.0 + c)
    return float(eq - 1.0), used, dropped


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    t0 = time.time()
    wide, rets = build_wide()
    regime, br = compute_regime(wide)
    bench_full = benchmark_return(rets)
    print(f"[setup] wide={wide.shape} 基准(等权全市场)全区间={pct(bench_full)} "
          f"牛市交易日占比={sum(regime.values())}/{len(regime)}", flush=True)

    out_dir = REGIME_ARTIFACTS_DIR
    jpath = out_dir / "strategy_regime.json"
    mpath = out_dir / "strategy_regime_report.md"

    records = []
    # mp=5 稳健增强：8 段窗口
    for (s, e) in WINDOWS:
        trades, err = run_engine("pullback_to_support", 5, "score_weight", s, e)
        if err:
            print(f"  [FAIL] mp5 {s}~{e}: {err}", flush=True)
            continue
        unf, u, d = simulate(trades, wide, rets, regime, False, 5)
        gat, u2, d2 = simulate(trades, wide, rets, regime, True, 5)
        print(f"  mp5 {s}~{e}: 不过滤={pct(unf)} 门控={pct(gat)} "
              f"(剔除{d2}笔/用{u2}笔) 基准段={pct(seg_bench(rets, s, e))}", flush=True)
        records.append({"window": f"{s}~{e}", "config": "pullback_mp5",
                        "unfiltered": unf, "gated": gat,
                        "dropped": d2, "used": u2,
                        "benchmark": seg_bench(rets, s, e)})
    # mp=1 单只集中：目标窗口 headline
    ts, te = "2026-03-24", "2026-06-24"
    trades, err = run_engine("pullback_to_support", 1, "score_weight", ts, te)
    if not err:
        unf, u, d = simulate(trades, wide, rets, regime, False, 1)
        gat, u2, d2 = simulate(trades, wide, rets, regime, True, 1)
        print(f"  mp1 {ts}~{te}: 不过滤={pct(unf)} 门控={pct(gat)} "
              f"(剔除{d2}笔/用{u2}笔)", flush=True)
        records.append({"window": f"{ts}~{te}", "config": "pullback_mp1",
                        "unfiltered": unf, "gated": gat,
                        "dropped": d2, "used": u2,
                        "benchmark": seg_bench(rets, ts, te)})

    jpath.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # markdown
    L = ["# Regime（市场宽度）过滤验证报告", ""]
    L.append("> **P0+P1 落地**：用全市场 MA120 宽度判断牛/熊，熊市整段空仓，验证能否把下跌段亏损填平。")
    L.append("> **方法**：回测层门控——剔除'入场日在熊市'的成交单后重算净值（不改策略源码、不重新寻优）。")
    L.append("> **基准**：等权全市场指数（由现有数据计算）。全区间基准收益 "
             f"**{pct(bench_full)}**；牛市交易日占比 "
             f"{sum(regime.values())}/{len(regime)}。")
    L.append("")
    L.append("## 一、mp=5 稳健增强（8 段窗口）：不过滤 vs 熊市空仓")
    L.append("")
    L.append("| 窗口 | 不过滤 | 门控(熊市空仓) | 变化 | 剔除笔数 | 等权基准 |")
    L.append("|---|---|---|---|---|---|")
    for r in records:
        if r["config"] != "pullback_mp5":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"| {r['window']} | {pct(r['unfiltered'])} | **{pct(r['gated'])}** | "
                 f"{'+' if delta >= 0 else ''}{pct(delta)} | {r['dropped']} | {pct(r['benchmark'])} |")
    L.append("")
    L.append("## 二、mp=1 单只集中（目标窗口 headline）")
    L.append("")
    for r in records:
        if r["config"] != "pullback_mp1":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"- **{r['window']}**：不过滤 **{pct(r['unfiltered'])}** → 门控 "
                 f"**{pct(r['gated'])}**（{'+' if delta >= 0 else ''}{pct(delta)}），"
                 f"剔除 {r['dropped']} 笔、保留 {r['used']} 笔。")
    L.append("")
    L.append("## 三、结论")
    L.append("")
    mp5 = [r for r in records if r["config"] == "pullback_mp5"]
    g_improved = sum(1 for r in mp5 if r["gated"] > r["unfiltered"])
    L.append(f"- mp=5 在 {g_improved}/{len(mp5)} 段上门控后收益提升；"
             "若下跌段(Benchmark 为负)的门控能把亏损转为空仓(0)，即证明 regime 过滤有效——"
             "把'只在主升浪赚钱'升级为'主升浪赚、熊市空仓不亏'。")
    L.append("- 这是**回测层近似**：整笔剔除=该段完全空仓，未做仓位再平衡；作为方向性验证足够，"
             "实盘需把 regime 信号写进策略(engine 层)才能真正'空仓不交易'。")
    L.append("- 下一步：将 regime 信号落进策略源码(matrix filter)做 engine 级空仓，"
             "并在 TRAIN 上寻优、TEST 上评估（真正的样本外），再用 Deflated Sharpe 校正多重检验。")
    L.append("")
    mpath.write_text("\n".join(L), encoding="utf-8")
    print(f"\n== 完成 | 报告已写 | 用时 {time.time()-t0:.0f}s ==", flush=True)


def seg_bench(rets, s, e):
    sd = _date.fromisoformat(s) if isinstance(s, str) else s
    ed = _date.fromisoformat(e) if isinstance(e, str) else e
    seg = rets.filter((pl.col("date") >= sd) & (pl.col("date") <= ed))
    syms = [c for c in seg.columns if c != "date"]
    if seg.height == 0:
        return None
    daily = seg.select(pl.mean_horizontal([pl.col(c) for c in syms]).alias("daily"))["daily"].fill_null(0.0)
    eq = (1.0 + daily).cum_prod()
    return float(eq[-1] - 1.0)


if __name__ == "__main__":
    main()
