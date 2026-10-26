"""P2 基准: 默认后端 (polars_expr) 参数网格优化的调参循环耗时。

对同一策略 + 同一参数网格跑 StrategyOptimizer.optimize, 记录准备/试算/总耗时。
P2a (prepare-once 面板复用) 前后各跑一次同一脚本对比, 验收标准:
调参循环提速 ≥2× (docs/ext-architecture-plan.md P2)。

用法:
  cd backend && uv run python scripts/bench_optimizer_prepare.py [--strategy ID]
  默认策略 custom_v1_kanban_strict (动态 filter_history, 默认执行后端)。
  --json PATH 另存结果。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.getLogger("app").setLevel(logging.WARNING)

from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.optimizer import StrategyOptimizer  # noqa: E402
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService  # noqa: E402
from app.config import settings  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

STRATEGY_ID = "consolidation_limit_pullback"
PARAM_GRID = {
    "box_days": [10, 15, 20, 25, 30],
    "box_amplitude": [0.10, 0.20, 0.25],
}
START, END = date(2026, 1, 1), date(2026, 9, 30)


def _build() -> StrategyOptimizer:
    from app.strategy import config as strategy_config

    data_dir = Path(settings.data_dir)
    dirs = [
        Path(__file__).resolve().parents[1] / "app" / "strategy" / "research",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]
    engine = StrategyEngine(
        strategy_dirs=[d for d in dirs if d.exists()],
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    repo = KlineRepository(DataStore(data_dir))
    service = StrategyBacktestService(BacktestEngine(repo), engine)
    return StrategyOptimizer(service, engine)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=STRATEGY_ID)
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    optimizer = _build()
    combos = [{"box_days": d, "box_amplitude": a} for d in PARAM_GRID["box_days"] for a in PARAM_GRID["box_amplitude"]]
    cfgs = [
        StrategyBacktestConfig(
            strategy_id=args.strategy, symbols=None, start=START, end=END,
            params=combo, mode="fast",
        )
        for combo in combos
    ]

    # 交错 A/B (同进程, 每组交替先后顺序): 消除页缓存/预热顺序偏置。
    # plain 臂 = 基线行为 (每组独立全量准备); shared 臂 = P2a (一次 prepare + 复用)。
    t0 = time.perf_counter()
    prepared_panel = optimizer.service.prepare_panel_optimization(cfgs)
    prepare_s = time.perf_counter() - t0

    plain_times: list[float] = []
    shared_times: list[float] = []
    plain_stats: list = []
    shared_stats: list = []
    for i, cfg in enumerate(cfgs):
        order = ("plain", "shared") if i % 2 == 0 else ("shared", "plain")
        for arm in order:
            t = time.perf_counter()
            res = optimizer.service.run(cfg, prepared_panel=prepared_panel if arm == "shared" else None)
            elapsed = time.perf_counter() - t
            assert res.error is None, f"combo {i} {arm}: {res.error}"
            (plain_times if arm == "plain" else shared_times).append(elapsed)
            (plain_stats if arm == "plain" else shared_stats).append(res.stats or {})

    plain_total = sum(plain_times)
    shared_total = sum(shared_times)
    row = {
        "strategy": args.strategy,
        "combos": len(cfgs),
        "panel_shape": list(prepared_panel.panel.shape),
        "prepare_s": round(prepare_s, 2),
        "plain_trials_s": round(plain_total, 2),
        "shared_trials_s": round(shared_total, 2),
        "plain_plus_prepare_s": round(plain_total, 2),
        "shared_plus_prepare_s": round(prepare_s + shared_total, 2),
        "trials_speedup": round(plain_total / shared_total, 2) if shared_total else None,
        "incl_prepare_speedup": round(plain_total / (prepare_s + shared_total), 2) if shared_total else None,
        "stats_identical": (
            [{k: v for k, v in st.items() if k != "timing_ms"} for st in plain_stats]
            == [{k: v for k, v in st.items() if k != "timing_ms"} for st in shared_stats]
        ),
        "per_combo_plain_s": [round(t, 2) for t in plain_times],
        "per_combo_shared_s": [round(t, 2) for t in shared_times],
    }
    print(json.dumps(row, ensure_ascii=False, indent=2))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
