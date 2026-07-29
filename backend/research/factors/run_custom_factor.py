"""custom_factor 因子策略 —— 引擎实盘式回测验证。

复用 tickflow 真实 Polars 回测引擎（费用/滑点/风控全部生效），
把 AlphaGPT DSL 因子公式（StackVM 编译）接入横截面选股，对比
pullback_to_support / bullish_alignment 基准策略。
"""

import json
import time
from datetime import date as _date

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import FACTOR_ARTIFACTS_DIR


# 目标窗口（与历史研究一致）+ 一段普跌段看稳健性
WINDOWS = [
    ("2026-03-24", "2026-06-24", "结构牛(目标)"),
    ("2025-09-24", "2025-12-24", "普跌段"),
]

# 待测因子公式（来自 run_factor_search 的 Top 双正因子 + 已知策略等价公式）
FORMULAS = {
    "rand_1002": "MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD",
    "rand_107": "MA60_DEV MOM5 VOL_RATIO DELAY1 MOM5 SUB ADD MUL MA60_DEV ADD",
    "momentum20": "MOM20",
    "pullback_ma20": "MA20_DEV",
}

BASE_OVERRIDES = {
    "stop_loss": -0.05,
    "take_profit": None,
    "trailing_stop": None,
    "max_hold_days": 20,
}


def run_engine(sid, max_positions, position_sizing, start, end,
               params=None, overrides=None, regime=None, mp_weight=None):
    cfg = StrategyBacktestConfig(
        strategy_id=sid, symbols=None,
        start=_date.fromisoformat(start), end=_date.fromisoformat(end),
        params=params, overrides=overrides, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=max_positions, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing=position_sizing,
        mode="position", holding_days=20, asset_type="stock", minute_fill=False,
        regime_filter=regime,
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    if res.get("error"):
        return None, res["error"]
    trades = res.get("trades") or []
    if not trades:
        return 0.0, None, 0
    eq = 1.0
    for t in trades:
        pnl = t.get("pnl_pct")
        if pnl is None:
            ep, xp = t.get("entry_price"), t.get("exit_price")
            pnl = (xp / ep - 1.0) if ep and xp else 0.0
        eq *= (1.0 + pnl)
    win = sum(1 for t in trades if (t.get("pnl_pct") or 0) > 0)
    return float(eq - 1.0), None, len(trades), round(win / len(trades) * 100, 1)


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    t0 = time.time()
    print("[custom-factor] 启动引擎实盘式回测验证", flush=True)
    records = []
    # 基准策略
    benchmarks = [
        ("pullback_to_support", "score_weight", 5, None),
        ("bullish_alignment", "score_weight", 10, None),
    ]
    # 因子策略
    factor_cfgs = [
        ("custom_factor", "score_weight", 10, FORMULAS[f])
        for f in FORMULAS
    ]

    for (s, e, label) in WINDOWS:
        print(f"\n===== 窗口 {s}~{e} ({label}) =====", flush=True)
        row = {"window": f"{s}~{e}", "label": label, "runs": []}
        for (sid, ps, mp, formula) in factor_cfgs:
            ret, err, n, win = run_engine(
                sid, mp, ps, s, e,
                params={"factor_formula": formula}, overrides=dict(BASE_OVERRIDES))
            if err:
                print(f"  [FAIL] {sid}/{formula[:20]}..: {err}", flush=True)
                continue
            print(f"  custom_factor[{formula[:28]:<28}] ret={pct(ret)} 笔数={n} 胜率={win}%", flush=True)
            row["runs"].append({"name": f"cf:{formula[:18]}", "formula": formula,
                                 "ret": ret, "n": n, "win": win})
        for (sid, ps, mp, _) in benchmarks:
            ret, err, n, win = run_engine(
                sid, mp, ps, s, e, params=None, overrides=dict(BASE_OVERRIDES))
            if err:
                print(f"  [FAIL] {sid}: {err}", flush=True)
                continue
            print(f"  {sid:<18} ret={pct(ret)} 笔数={n} 胜率={win}%", flush=True)
            row["runs"].append({"name": sid, "formula": "(builtin)",
                                 "ret": ret, "n": n, "win": win})
        records.append(row)

    out = FACTOR_ARTIFACTS_DIR / "strategy_custom_factor_verify.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n== 完成 | 用时 {time.time()-t0:.0f}s | 结果 {out} ==", flush=True)


if __name__ == "__main__":
    main()
