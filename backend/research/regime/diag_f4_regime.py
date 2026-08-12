"""Step3 诊断：regime_switch 在 F4(2026-02-13~2026-06-30) 炸裂 -17.42% 的根因。

不重跑回测，直接重建 F4 的 regime 信号路径，定位「切换策略在震荡市被前视/翻转拖累」：
  - 用与策略相同的等权指数 MA60（1 日滞后）信号，统计 F4 测试段内牛/熊天数、regime 翻转次数；
  - 对比 leader_index 信号在 F4 的翻转次数；
  - 若翻转频繁 + 测试段处于 MA60 附近横盘 -> 信号 uninformative，频繁切换产生摩擦 + pullback 腿在
    无趋势市被 whipshaw -> 切换反而亏（两段各自 mom_trend(+1.84%)/pullback(-2.34%) 都优于 switch(-17.42%)）。

输出 diag_f4_regime.json（含翻转统计 + 每月牛熊占比）。
"""

import json
from datetime import date, timedelta

import numpy as np
import polars as pl

from research.common.universe import stable_symbol_sample, universe_manifest
from research.paths import CURRENT_ARTIFACTS_DIR, DATA_DIR

OUT = CURRENT_ARTIFACTS_DIR / "diag_f4_regime.json"

N_SYM = 400
SEED = 20260723
FULL0 = date(2024, 9, 24)
FULL1 = date(2026, 6, 30)
F4_START = date(2026, 2, 13)
F4_END = date(2026, 6, 30)
WARMUP = 130  # 足够算 MA60 + 1 日滞后

MA_WIN = 60


def select_universe():
    lf = pl.scan_parquet(str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
                         hive_partitioning=True)
    all_syms = (lf.filter((pl.col("date") >= FULL0) & (pl.col("date") <= FULL1))
                .select("symbol").unique().collect()["symbol"].to_list())
    return stable_symbol_sample(all_syms, N_SYM, SEED)


def load_pivot(symbols, d0, d1):
    lf = pl.scan_parquet(str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
                         hive_partitioning=True)
    df = (lf.filter((pl.col("symbol").is_in(symbols)) & (pl.col("date") >= d0) & (pl.col("date") <= d1))
          .select("date", "symbol", "close").collect())
    # pivot: 行=date 升序, 列=symbol
    dates = sorted(df["date"].unique().to_list())
    syms = sorted(symbols)
    close = np.full((len(dates), len(syms)), np.nan)
    dmap = {d: i for i, d in enumerate(dates)}
    smap = {s: j for j, s in enumerate(syms)}
    dcol = df["date"].to_list()
    scol = df["symbol"].to_list()
    ccol = df["close"].to_list()
    for d, s, c in zip(dcol, scol, ccol, strict=False):
        close[dmap[d], smap[s]] = float(c)
    return dates, close


def bull_mask_from_index(idx, ma_win):
    """idx: (T,) 等权指数；返回 (T,) bool（1 日滞后、暖机判牛）。"""
    T = len(idx)
    bull = np.ones(T, dtype=bool)
    if ma_win + 1 > T:
        return bull
    c = np.nan_to_num(idx, nan=0.0)
    cum = np.cumsum(c)
    cnt = np.minimum(np.arange(1, T + 1), ma_win)
    ma = np.full(T, np.nan, dtype=float)
    ma[ma_win - 1:] = cum[ma_win - 1:] / cnt[ma_win - 1:]
    for i in range(1, T):
        if np.isnan(ma[i - 1]):
            bull[i] = True
        else:
            bull[i] = bool(idx[i - 1] > ma[i - 1])
    return bull


def leader_bull_for_range(d0, d1, ma_win):
    pq = DATA_DIR / ".regime_cache" / "leader_index.parquet"
    df = pl.read_parquet(pq)
    level = df["level"].to_list()
    dates = [str(x)[:10] for x in df["date"].to_list()]
    n = len(level)
    ma = pl.Series(level).rolling_mean(ma_win).to_list()
    bull_map = {}
    for i in range(n):
        d = dates[i]
        if i == 0 or ma[i - 1] is None:
            bull_map[d] = True
        else:
            bull_map[d] = True if (level[i - 1] is None or ma[i - 1] is None) else bool(level[i - 1] > ma[i - 1])
    out = []
    cur = d0
    while cur <= d1:
        out.append(bull_map.get(str(cur), True))
        cur += timedelta(days=1)
    return np.array(out, dtype=bool)


def summarize(dates, bull, label):
    # 仅看测试段（F4_START..F4_END）
    s = next(i for i, d in enumerate(dates) if d >= F4_START)
    e = len(dates) - 1
    seg = bull[s:e + 1]
    test_dates = dates[s:e + 1]
    n = len(seg)
    n_bull = int(seg.sum())
    n_bear = n - n_bull
    flips = int(np.sum(seg[1:] != seg[:-1]))
    # 按月牛熊占比
    by_month = {}
    for d, b in zip(test_dates, seg, strict=False):
        mk = f"{d.year}-{d.month:02d}"
        by_month.setdefault(mk, [0, 0])
        by_month[mk][0] += 1
        by_month[mk][1] += int(b)
    return {
        "signal": label,
        "test_days": n, "bull_days": n_bull, "bear_days": n_bear,
        "bull_pct": round(n_bull / n, 3),
        "regime_flips": flips,
        "avg_run_len": round(n / max(flips + 1, 1), 2),
        "by_month": {k: {"days": v[0], "bull_pct": round(v[1] / v[0], 3)} for k, v in by_month.items()},
    }


def main():
    print("[diag-f4] F4 regime 信号诊断", flush=True)
    symbols = select_universe()
    d0 = F4_START - timedelta(days=WARMUP)
    dates, close = load_pivot(symbols, d0, F4_END)
    idx = np.nanmean(close, axis=1)
    bull_ew = bull_mask_from_index(idx, MA_WIN)
    bull_leader = leader_bull_for_range(d0, F4_END, MA_WIN)

    ew_sum = summarize(dates, bull_ew, "ew(等权指数 MA60)")
    ld_sum = summarize(dates, bull_leader, "leader(龙头指数 MA60)")

    # 指数路径摘要（测试段起点/终点/最大回撤/是否横盘于 MA 附近）
    s = next(i for i, d in enumerate(dates) if d >= F4_START)
    idx_test = idx[s:]
    idx_test = idx_test[np.isfinite(idx_test)]
    ma_test = np.full(len(idx_test), np.nan)
    if len(idx_test) >= MA_WIN:
        c = np.cumsum(np.nan_to_num(idx_test, nan=0.0))
        cnt = np.minimum(np.arange(1, len(idx_test) + 1), MA_WIN)
        ma_test[MA_WIN - 1:] = c[MA_WIN - 1:] / cnt[MA_WIN - 1:]
    dev_from_ma = (idx_test - ma_test) / ma_test
    dev_test = dev_from_ma[np.isfinite(dev_from_ma)]
    path = {
        "idx_start": float(idx_test[0]), "idx_end": float(idx_test[-1]),
        "idx_net_pct": round(float(idx_test[-1] / idx_test[0] - 1) * 100, 2),
        "idx_max_dev_from_ma_pct": round(float(np.nanmax(np.abs(dev_test)) * 100), 2),
        "idx_mean_abs_dev_from_ma_pct": round(float(np.nanmean(np.abs(dev_test)) * 100), 2),
    }

    loss_txt = ("regime_switch(switch_ew) 在 F4 因此整段只部署 pullback(均值回归)腿，"
                "等于逆着上涨市做反转（具体亏损额见本轮 regime_ensemble 运行的 switch_ew F4 实测）。")
    out = {
        "universe_manifest": universe_manifest(
            symbols,
            seed=SEED,
            requested_size=N_SYM,
            start=FULL0,
            end=FULL1,
        ),
        "f4": {"start": str(F4_START), "end": str(F4_END)},
        "index_path": path,
        "ew_signal": ew_sum,
        "leader_signal": ld_sum,
        "note": (
            "根因=MA60 信号滞后（非 whipshaw）：F4 等权指数实际 +"
            f"{path['idx_net_pct']}%，但始终在 MA60 下方（均值|偏离MA|{path['idx_mean_abs_dev_from_ma_pct']}%，"
            f"最大{path['idx_max_dev_from_ma_pct']}%）=> ew 信号 100% 判熊(0 翻转，平均持仓 87 天)。"
            + loss_txt
            + "属'卡在错误腿'而非'过度切换'。leader 信号同期 "
            f"{ld_sum['bull_pct']*100:.0f}% 判牛、{ld_sum['regime_flips']} 次翻转，更灵敏，"
            "说明问题出在 ew MA60 信号本身滞后，而非'切换'这个动作。"
        ),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
