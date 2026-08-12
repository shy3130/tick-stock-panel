"""Legacy historical replay; not a current regime entrypoint.

引擎级 regime 门控验证：直接走 app.backtest 生产路径(StrategyBacktestConfig + make_worker_task)。
验证两点：
1) 无 regime_filter 时，结果与旧 leader-index replay 的 unfiltered 基线一致(无回归)；
2) 有 regime_filter={"type":"leader_index"} 时，熊段空仓、牛段保留，且与回测层门控数字接近(引擎级真生效)。
不重算龙头指数(复用 .regime_cache/leader_index.parquet)。
"""
import json
import time
from datetime import date as _date

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import REGIME_ARTIFACTS_DIR

WINDOWS = [
    ("2025-09-24", "2025-12-24"),   # 普跌段：应显著减亏
    ("2026-01-24", "2026-04-24"),   # 普跌段
    ("2026-03-24", "2026-06-24"),   # 目标窗口(结构牛)：应基本保留
    ("2026-05-24", "2026-07-20"),   # 普跌段
]


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
    # 用 trades 的平仓盈亏近似总收益
    if not trades:
        return 0.0, None
    eq = 1.0
    for t in trades:
        pnl = t.get("pnl_pct")
        if pnl is None:
            # 退化为 entry/exit 价计算
            ep, xp = t.get("entry_price"), t.get("exit_price")
            pnl = xp / ep - 1.0 if ep and xp else 0.0
        eq *= (1.0 + pnl)
    return float(eq - 1.0), None


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    t0 = time.time()
    print("[engine-regime] 启动验证 MA60 vs MA20 (复用 leader_index.parquet)", flush=True)
    records = []
    for (s, e) in WINDOWS:
        unf, err0 = run_engine("pullback_to_support", 5, "score_weight", s, e, regime=None)
        g60, err1 = run_engine("pullback_to_support", 5, "score_weight", s, e,
                               regime={"type": "leader_index", "ma": 60})
        g20, err2 = run_engine("pullback_to_support", 5, "score_weight", s, e,
                               regime={"type": "leader_index", "ma": 20})
        if err0 or err1 or err2:
            print(f"  [FAIL] {s}~{e}: no={err0} ma60={err1} ma20={err2}", flush=True)
            continue
        print(f"  {s}~{e}: 无={pct(unf)} MA60={pct(g60)} MA20={pct(g20)}", flush=True)
        records.append({"window": f"{s}~{e}", "no_regime": unf,
                        "ma60": g60, "ma20": g20,
                        "delta60": g60 - unf, "delta20": g20 - unf})
    out = REGIME_ARTIFACTS_DIR / "strategy_engine_regime_verify.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s | 结果 {out} ==", flush=True)


if __name__ == "__main__":
    main()
