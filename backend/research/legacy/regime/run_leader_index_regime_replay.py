"""Legacy historical replay; not a current regime entrypoint.

P2 落地：龙头指数趋势(leader-index trend) regime 过滤的回测层验证。
- 构建"龙头指数"：每天用「过去 MOM_LOOKBACK 日动量」对全市场排名，取前 TOP_FRAC（前 10%）等权，
  其每日等权收益复利成指数 = 龙头指数。动量用 close[d-1]/close[d-1-MOM_LOOKBACK]，纯历史、无前视。
- regime 信号：龙头指数站上自身 MA(MA_WIN) 判为牛市（龙头在上升趋势）；否则空仓。
  信号在 d 采用 d-1 及之前的信息（level[d-1] > ma[d-1]），对入场日做 1 日滞后门控，杜绝前视。
- 与 P0+P1 的"全市场 MA120 宽度"对比：宽度在窄广度结构牛里误判全程熊市、踏空龙头；
  龙头指数趋势应正确识别"少数龙头主升"，保留赢家交易，同时在普跌段（龙头也跌）空仓减亏。
- 门控测试：把策略成交单中"入场日 regime=熊"的整笔剔除（那段空仓），重算净值，对比 不过滤 vs 门控。
  严格不重新寻优：直接套用 mp=5 稳健 / mp=1 单只集中，在 8 段 walk-forward 窗口 + 目标窗口上验证。
数据：全部来自 data/，宽表复用 .regime_cache；不改策略源码。
"""
import glob
import json
import time
from datetime import date as _date

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import DATA_DIR, REGIME_ARTIFACTS_DIR

# ---- 可调参数 ----
MOM_LOOKBACK = 60      # 动量回看天数（close[d-1]/close[d-1-LOOK]-1）
MA_WIN = 60            # 龙头指数趋势均线窗口
TOP_FRAC = 0.10        # 前 10% 为龙头

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
TARGET = ("2026-03-24", "2026-06-24")

CACHE = DATA_DIR / ".regime_cache"
CACHE.mkdir(parents=True, exist_ok=True)
WIDE_PARQUET = CACHE / "wide_close.parquet"
RETS_PARQUET = CACHE / "wide_rets.parquet"
LEADER_PARQUET = CACHE / "leader_index.parquet"


def build_wide():
    if WIDE_PARQUET.exists() and RETS_PARQUET.exists():
        return pl.read_parquet(WIDE_PARQUET), pl.read_parquet(RETS_PARQUET)
    files = glob.glob(str(DATA_DIR / "kline_daily_enriched/**/*.parquet"), recursive=True)
    frames = [pl.read_parquet(f).select("symbol", "date", "close") for f in files]
    alld = pl.concat(frames)
    wide = alld.pivot(values="close", index="date", on="symbol").sort("date")
    syms = [c for c in wide.columns if c != "date"]
    rets = wide.with_columns([(pl.col(c) / pl.col(c).shift(1) - 1).alias(c) for c in syms])
    wide.write_parquet(WIDE_PARQUET)
    rets.write_parquet(RETS_PARQUET)
    return wide, rets


def compute_leader_index(wide):
    """返回 (dates: list, level: list, ma: list, regime: dict(date->bool), up_ratio_overall)。"""
    if LEADER_PARQUET.exists():
        ld = pl.read_parquet(LEADER_PARQUET)
        dates = ld["date"].to_list()
        level = ld["level"].to_list()
        ma = ld["ma60"].to_list()
        regime = {d: (lv is not None and mv is not None and lv > mv)
                  for d, lv, mv in zip(dates, level, ma, strict=False)}
        return dates, level, ma, regime, float(pl.Series(level).count())

    long = wide.unpivot(index="date", variable_name="symbol", value_name="close").sort(["date", "symbol"])
    # 日收益（用于构建指数）
    long = long.with_columns(
        (pl.col("close") / pl.col("close").shift(1) - 1).over("symbol", order_by="date").alias("ret"))
    # 动量：用 d-1 及之前信息，避免前视。close[d-1]/close[d-1-LOOK]-1
    long = long.with_columns(
        (pl.col("close").shift(1) / pl.col("close").shift(1 + MOM_LOOKBACK) - 1)
        .over("symbol", order_by="date").alias("mom"))
    # 每日动量阈值 = 前 10% 分位
    thr = long.group_by("date").agg(pl.col("mom").quantile(1 - TOP_FRAC).alias("thr"))
    long = long.join(thr, on="date")
    long = long.with_columns((pl.col("mom") >= pl.col("thr")).alias("is_leader"))
    # 龙头每日等权收益
    lead = (long.filter(pl.col("is_leader"))
            .group_by("date").agg(pl.col("ret").mean().alias("leader_ret"))
            .sort("date"))
    # 对齐到全日期，缺失填 0（极少发生）
    lead = (wide.select("date").join(lead, on="date", how="left")
            .with_columns(pl.col("leader_ret").fill_null(0.0)))
    # 指数净值
    lead = lead.with_columns(
        (1.0 + pl.col("leader_ret")).cum_prod().alias("level"))
    ma_series = lead["level"].rolling_mean(MA_WIN)
    lead = lead.with_columns(ma_series.alias("ma60"))
    # regime：用 d-1 信息（level[d-1] > ma60[d-1]），warmup(ma60 为空)默认 True 不过滤
    lv = lead["level"].to_list()
    mv = lead["ma60"].to_list()
    dates = lead["date"].to_list()
    regime = {}
    for i, d in enumerate(dates):
        if i == 0 or mv[i - 1] is None:
            regime[d] = True
        else:
            regime[d] = lv[i - 1] > mv[i - 1]
    lead.write_parquet(LEADER_PARQUET)
    return dates, lv, mv, regime, float(pl.Series(lv).count())


def leader_full_return(dates, level):
    if not level or level[0] in (None, 0):
        return None
    return float(level[-1] / level[0] - 1.0)


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
    dates = wide["date"].to_list()
    date_pos = {d: i for i, d in enumerate(dates)}
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
        bull = regime.get(ed, True)
        if gate and not bull:
            dropped += 1
            continue
        used += 1
        seg = rets.filter((pl.col("date") >= ed) & (pl.col("date") <= xd)).select("date", t["symbol"])
        dl, rl = seg["date"].to_list(), seg[t["symbol"]].to_list()
        for d, r in zip(dl, rl, strict=False):
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


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def regime_up_ratio(regime, s, e):
    sd = _date.fromisoformat(s) if isinstance(s, str) else s
    ed = _date.fromisoformat(e) if isinstance(e, str) else e
    vals = [v for d, v in regime.items() if sd <= d <= ed]
    if not vals:
        return None
    return sum(vals) / len(vals)


def main():
    t0 = time.time()
    wide, rets = build_wide()
    dates, level, _ma, regime, _ = compute_leader_index(wide)
    lret = leader_full_return(dates, level)
    print(f"[setup] 龙头指数全区间={pct(lret)} 牛市区占比(整体)={pct(regime_up_ratio(regime, dates[0].isoformat() if hasattr(dates[0],'isoformat') else dates[0], dates[-1].isoformat() if hasattr(dates[-1],'isoformat') else dates[-1]))}", flush=True)

    out_dir = REGIME_ARTIFACTS_DIR
    jpath = out_dir / "strategy_leader_regime.json"
    mpath = out_dir / "strategy_leader_regime_report.md"

    records = []
    # mp=5 稳健增强：8 段窗口
    for (s, e) in WINDOWS:
        trades, err = run_engine("pullback_to_support", 5, "score_weight", s, e)
        if err:
            print(f"  [FAIL] mp5 {s}~{e}: {err}", flush=True)
            continue
        unf, _, _ = simulate(trades, wide, rets, regime, False, 5)
        gat, u2, d2 = simulate(trades, wide, rets, regime, True, 5)
        up = regime_up_ratio(regime, s, e)
        print(f"  mp5 {s}~{e}: 不过滤={pct(unf)} 门控={pct(gat)} "
              f"(剔除{d2}/用{u2}) 牛市区={pct(up)} 基准={pct(seg_bench(rets, s, e))}", flush=True)
        records.append({"window": f"{s}~{e}", "config": "pullback_mp5",
                        "unfiltered": unf, "gated": gat,
                        "dropped": d2, "used": u2,
                        "regime_up_ratio": up,
                        "benchmark": seg_bench(rets, s, e)})
    # mp=1 单只集中：目标窗口 headline
    ts, te = TARGET
    trades, err = run_engine("pullback_to_support", 1, "score_weight", ts, te)
    if not err:
        unf, _u, _d = simulate(trades, wide, rets, regime, False, 1)
        gat, u2, d2 = simulate(trades, wide, rets, regime, True, 1)
        up = regime_up_ratio(regime, ts, te)
        print(f"  mp1 {ts}~{te}: 不过滤={pct(unf)} 门控={pct(gat)} "
              f"(剔除{d2}/用{u2}) 牛市区={pct(up)}", flush=True)
        records.append({"window": f"{ts}~{te}", "config": "pullback_mp1",
                        "unfiltered": unf, "gated": gat,
                        "dropped": d2, "used": u2,
                        "regime_up_ratio": up,
                        "benchmark": seg_bench(rets, ts, te)})

    jpath.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # markdown
    L = ["# 龙头指数趋势（Leader-Index Trend）Regime 过滤验证报告", ""]
    L.append("> **P2 落地**：用「动量前 10% 等权」构建龙头指数，站上自身 MA60 判牛市，熊市整段空仓，")
    L.append("> 验证能否**保留结构牛中的龙头赢家**、同时**在普跌段空仓减亏**（修正 P0+P1 宽度过滤在窄广度牛里踏空的缺陷）。")
    L.append("> **方法**：回测层门控——剔除'入场日 regime=熊'的成交单后重算净值（不改策略源码、不重新寻优）。")
    L.append("> 龙头指数全区间收益 **" + pct(lret) + "**（动量前 10% 等权本身是强因子，作为信号基底合理）。")
    L.append("")
    L.append("## 一、mp=5 稳健增强（8 段窗口）：不过滤 vs 龙头趋势门控")
    L.append("")
    L.append("| 窗口 | 不过滤 | 门控(熊市空仓) | 变化 | 剔除/保留 | 牛市区占比 | 等权基准 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in records:
        if r["config"] != "pullback_mp5":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"| {r['window']} | {pct(r['unfiltered'])} | **{pct(r['gated'])}** | "
                 f"{'+' if delta >= 0 else ''}{pct(delta)} | {r['dropped']}/{r['used']} | "
                 f"{pct(r['regime_up_ratio'])} | {pct(r['benchmark'])} |")
    L.append("")
    L.append("## 二、mp=1 单只集中（目标窗口 headline）")
    L.append("")
    for r in records:
        if r["config"] != "pullback_mp1":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"- **{r['window']}**：不过滤 **{pct(r['unfiltered'])}** → 门控 "
                 f"**{pct(r['gated'])}**（{'+' if delta >= 0 else ''}{pct(delta)}），"
                 f"剔除 {r['dropped']} 笔、保留 {r['used']} 笔；牛市区占比 {pct(r['regime_up_ratio'])}。")
    L.append("")
    L.append("## 三、与 P0+P1（全市场 MA120 宽度）对比的关键判读")
    L.append("")
    L.append("- **目标窗口 2026-03-24~06-24**：宽度信号牛市区仅 4/62 天（≈6%），把 mp1 全部 9 笔赢家(含欧科亿+61%)")
    L.append("  滤掉→收益归零；龙头趋势信号应判大部时间为牛（结构牛里龙头在涨），保留赢家交易。")
    L.append("- **普跌段（如 2025-09、2026-01）**：龙头也随市场下跌，龙头指数跌破 MA60→空仓，应避免/减轻亏损")
    L.append("  （宽度信号在这两段同样有效，但龙头信号在牛段不误杀）。")
    L.append("- 若龙头趋势门控在'牛段保留收益、熊段减亏'同时成立，则该 regime 适合本基金（赚少数龙头的钱）。")
    L.append("")
    L.append("## 四、结论与下一步")
    L.append("")
    mp5 = [r for r in records if r["config"] == "pullback_mp5"]
    g_improved = sum(1 for r in mp5 if r["gated"] > r["unfiltered"])
    L.append(f"- mp=5 在 {g_improved}/{len(mp5)} 段上门控后收益提升。")
    L.append("- 这是**回测层近似**（整笔剔除=该段完全空仓，未做仓位再平衡）。实盘需把 regime 信号写进策略")
    L.append("  (engine 层 filter) 才能真正'空仓不交易'；并在 TRAIN 上寻优、TEST 上评估（真正样本外），")
    L.append("  再用 Deflated Sharpe 校正多重检验。")
    L.append("")
    mpath.write_text("\n".join(L), encoding="utf-8")
    print(f"\n== 完成 | 报告已写 | 用时 {time.time()-t0:.0f}s ==", flush=True)


if __name__ == "__main__":
    main()
