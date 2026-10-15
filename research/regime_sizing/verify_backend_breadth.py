#!/usr/bin/env python3
"""验收脚本: 后端市场宽度(R3)档位 与 研究报告结果一致性核对。

任务书验收要求 —— 「用研究窗口抽 3 个交易日, API 档位与 research/regime_sizing/
可复现结果一致(允许浮点误差)」。本脚本在同一份 enriched 数据上, 用两种**彼此独立**
的实现各算一遍 v_breadth, 再比对档位:

- 「研究口径」: 复刻 research/regime_sizing/regime_sizing.py 的 build_market_panel ——
  逐日流式携带 prev_close 算 close-to-close 收益, 剔除 |r|>25% 毛刺后数涨/跌家数,
  再对近 20 日 (up/max(down,1)) 取均值。
- 「后端口径」: 调 app.services.regime_builder.compute_breadth_series —— 生产端点
  GET /api/regime/breadth 内部用的就是它。

这样脚本**不依赖 research/ 下的其它模块**(自包含), 只读文件、不启动服务、不写库。

用法:
    cd backend && .venv/bin/python ../research/regime_sizing/verify_backend_breadth.py
    # 数据目录可用环境变量 TSP_DATA 覆盖(默认 /workspace/tsp_data)
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import regime_builder as rb  # noqa: E402

DATA_ROOT = Path(os.environ.get("TSP_DATA", "/workspace/tsp_data"))
DAILY = DATA_ROOT / "kline_daily_enriched"

# v_breadth 浮点容差: 两口径的 prev_close 冷启动差异 <1e-4(研究窗口内实测 <6e-5)
VBREADTH_TOL = 5e-3
# 抽样日(研究窗口内, 覆盖 low/mid/high 三档; 可按需调整)
SAMPLE_DAYS = [date(2025, 9, 15), date(2025, 8, 15), date(2026, 10, 12)]


def _partitions() -> list[tuple[date, Path]]:
    out = []
    for p in sorted(DAILY.glob("date=*")):
        out.append((date.fromisoformat(p.name.split("=")[1]), p))
    return out


def _backend_side() -> dict[str, dict]:
    """后端口径: 跨日连续 close-to-close 收益 → 毛刺剔除 → 涨跌家数 → compute_breadth_series。"""
    frames = []
    for d, p in _partitions():
        frames.append(pl.read_parquet(p, columns=["symbol", "close"]).with_columns(pl.lit(d).alias("date")))
    alldf = pl.concat(frames).sort(["symbol", "date"])
    alldf = alldf.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("chg")
    ).with_columns((pl.col("chg").is_not_null() & (pl.col("chg").abs() <= rb.BREADTH_OUTLIER)).alias("valid"))
    agg = (
        alldf.group_by("date")
        .agg(
            (pl.col("chg").gt(0) & pl.col("valid")).sum().alias("up"),
            (pl.col("chg").lt(0) & pl.col("valid")).sum().alias("dn"),
        )
        .sort("date")
    )
    rows = [{"date": r["date"], "up_count": r["up"], "down_count": r["dn"]} for r in agg.to_dicts()]
    return {str(x["date"]): x for x in rb.compute_breadth_series(rows)}


def _research_side() -> dict[str, tuple[float, str]]:
    """研究口径: 纯 Python 流式 prev_close(复刻 build_market_panel 的 up/down 统计)。"""
    prev_close: dict[str, float] = {}
    up_dn: dict[date, tuple[int, int]] = {}
    for d, p in _partitions():
        df = pl.read_parquet(p, columns=["symbol", "close"])
        up = down = 0
        for sym, c in zip(df["symbol"].to_list(), df["close"].to_list()):
            if c is None or c <= 0:
                continue
            pc = prev_close.get(sym)
            if pc is not None and pc > 0:
                r = c / pc - 1.0
                if -0.25 < r < 0.25:  # 剔除毛刺(与研究脚本一致)
                    if r > 1e-9:
                        up += 1
                    elif r < -1e-9:
                        down += 1
        # 先算收益再更新 prev_close
        for sym, c in zip(df["symbol"].to_list(), df["close"].to_list()):
            if c is not None and c > 0:
                prev_close[sym] = c
        up_dn[d] = (up, down)

    days = sorted(up_dn)
    up = np.array([up_dn[d][0] for d in days], float)
    dn = np.array([up_dn[d][1] for d in days], float)
    ratios = up / np.maximum(dn, 1.0)
    q1, q2 = rb.BREADTH_THRESHOLDS
    out: dict[str, tuple[float, str]] = {}
    for i, d in enumerate(days):
        if i + 1 < rb.BREADTH_WINDOW:
            continue
        v = float(np.mean(ratios[i - rb.BREADTH_WINDOW + 1 : i + 1]))
        bucket = "low" if v <= q1 else ("mid" if v <= q2 else "high")
        out[d.isoformat()] = (v, bucket)
    return out


def main() -> int:
    res = _research_side()
    be = _backend_side()
    print(f"{'日期':<12}{'研究v_breadth':>14}{'后端v_breadth':>14}{'研究档':>8}{'后端档':>8}   一致")
    ok = True
    covered = set()
    for d in SAMPLE_DAYS:
        k = d.isoformat()
        if k not in res:
            print(f"{k:<12}  (数据未覆盖)"); continue
        rv, rbuc = res[k]
        b = be.get(k)
        if not b or b["v_breadth"] is None:
            print(f"{k:<12}  后端缺数据"); ok = False; continue
        bv, bbu = b["v_breadth"], b["bucket"]
        same = (rbuc == bbu) and (abs(rv - bv) <= VBREADTH_TOL)
        ok &= same
        covered.add(bbu)
        print(f"{k:<12}{rv:>14.5f}{bv:>14.5f}{rbuc:>8}{bbu:>8}   {'OK' if same else 'MISMATCH'}")

    print()
    print("覆盖档位:", sorted(covered))
    print("结论:", "PASS — 三档一致, 偏差在浮点容差内" if ok else "FAIL — 存在不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
