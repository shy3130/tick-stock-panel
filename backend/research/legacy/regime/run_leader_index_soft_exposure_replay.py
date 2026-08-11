"""Legacy historical replay; not a current regime entrypoint.

引擎级 regime 软叠加验证：直接走 app.backtest 生产路径(StrategyBacktestConfig + make_worker_task)。
对 pullback_to_support mp=5 在 4 个窗口测 3 种配置：
  - none : 无门控
  - hard : regime_filter={"type":"leader_index","ma":60,"mode":"hard"}  (熊市日清零开仓)
  - soft : regime_filter={"type":"leader_index","ma":60,"mode":"soft","bear_weight":0.3} (熊市日 exposure×0.3)
验证软叠加能否既保住结构牛上行、又在普跌段减亏 (介于 none 与 hard 之间)。
复用 .regime_cache/leader_index.parquet，不复算龙头指数。
"""
import json
import time
from datetime import date as _date

import polars as pl

from app.config import settings
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from research.paths import REGIME_ARTIFACTS_DIR

WINDOWS = [
    ("2025-09-24", "2025-12-24"),   # 普跌段
    ("2026-01-24", "2026-04-24"),   # 普跌段
    ("2026-03-24", "2026-06-24"),   # 目标窗口(结构牛)
    ("2026-05-24", "2026-07-20"),   # 普跌段
]

CONFIGS = {
    "none": None,
    "hard": {"type": "leader_index", "ma": 60, "mode": "hard"},
    "soft": {"type": "leader_index", "ma": 60, "mode": "soft", "bear_weight": 0.3},
}


def run_engine(sid, max_positions, position_sizing, start, end, regime=None):
    cfg = StrategyBacktestConfig(
        strategy_id=sid, symbols=None,
        start=_date.fromisoformat(start), end=_date.fromisoformat(end),
        params=None, overrides=None, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=max_positions, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing=position_sizing,
        mode="position", holding_days=5, asset_type="stock", minute_fill=False,
        regime_filter=regime,
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    if res.get("error"):
        return None, res["error"]
    trades = res.get("trades") or []
    if not trades:
        return 0.0, None
    eq = 1.0
    for t in trades:
        pnl = t.get("pnl_pct")
        if pnl is None:
            ep, xp = t.get("entry_price"), t.get("exit_price")
            pnl = (xp / ep - 1.0) if (ep and xp) else 0.0
        eq *= (1.0 + pnl)
    return float(eq - 1.0), None


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    t0 = time.time()
    print("[engine-soft] 启动验证 (复用 leader_index.parquet)", flush=True)
    records = []
    for (s, e) in WINDOWS:
        row = {"window": f"{s}~{e}", "kind": _kind(s, e)}
        ok = True
        for name, regime in CONFIGS.items():
            v, err = run_engine("pullback_to_support", 5, "score_weight", s, e, regime=regime)
            if err:
                print(f"  [FAIL] {s}~{e} {name}: {err}", flush=True)
                row[name] = None
                ok = False
            else:
                row[name] = v
                print(f"  {s}~{e} [{name}] = {pct(v)}", flush=True)
        if ok:
            none_v = row.get("none") or 0.0
            row["hard_delta"] = (row.get("hard") or 0.0) - none_v
            row["soft_delta"] = (row.get("soft") or 0.0) - none_v
        records.append(row)
    out = REGIME_ARTIFACTS_DIR / "strategy_engine_soft_verify.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s | 结果 {out} ==", flush=True)


def _kind(s, e):
    if s == "2026-03-24":
        return "结构牛(目标)"
    return "普跌"


if __name__ == "__main__":
    main()
