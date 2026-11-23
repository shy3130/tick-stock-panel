"""P0/P1a 基准: 动态 filter_history 策略的依赖解析宽度对比。

对同一策略比较两种依赖声明下的 ResolvedFeaturePlan:
  legacy   — required_features 为空 (P1a 之前的行为, 触发全量指标兜底)
  inferred — 引擎 AST 推导结果 (P1a 之后, 不可推导的策略无此行)

记录解析耗时与 indicator/base 列宽。数据加载 IO 大致随列宽线性增长,
宽度比即回测数据准备阶段的近似提速上界 (非端到端回测耗时)。

用法:
  cd backend && uv run python scripts/bench_feature_resolution.py [strategy_id ...]
  不传 id 时扫描全部 filter_history 策略。--json PATH 另存结果。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# legacy 臂会故意触发全量兜底 WARNING, 基准输出只保留表格。
logging.getLogger("app.backtest.strategy").setLevel(logging.ERROR)

from app.backtest.strategy import StrategyDependencyResolver  # noqa: E402
from app.config import settings  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402


def _build_engine() -> StrategyEngine:
    dirs = [
        Path(__file__).resolve().parents[1] / "app" / "strategy" / "research",
        settings.data_dir / "strategies" / "custom",
        settings.data_dir / "strategies" / "ai",
        settings.data_dir / "strategies" / "composite",
    ]
    return StrategyEngine(strategy_dirs=[d for d in dirs if d.exists()])


def _resolve_quiet(resolver: StrategyDependencyResolver, strategy, required: frozenset):
    """给定 required_features 解析 FeaturePlan, 返回 (耗时ms, plan)。"""
    patched = dataclasses.replace(strategy, required_features=required)
    t0 = time.perf_counter()
    plan = resolver.resolve(
        patched,
        params={p.get("id"): p.get("default") for p in strategy.meta.get("params", [])},
        basic_filter=strategy.basic_filter or {"enabled": False},
        entry_signals=strategy.entry_signals,
        exit_signals=strategy.exit_signals,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return elapsed_ms, plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*", help="策略 id; 缺省扫描全部动态策略")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    engine = _build_engine()
    resolver = StrategyDependencyResolver()
    strategies = [
        s for s in engine.list_strategies_full() if s.filter_history_fn is not None
    ] if hasattr(engine, "list_strategies_full") else []
    if not strategies:
        # list 接口名差异兜底: 直接遍历注册表
        strategies = [
            s for s in engine._strategies.values() if s.filter_history_fn is not None
        ]
    if args.ids:
        strategies = [s for s in strategies if s.meta.get("id") in set(args.ids)]

    rows: list[dict] = []
    for s in strategies:
        sid = s.meta.get("id", "<unknown>")
        declared = bool(s.required_features)
        legacy_ms, legacy_plan = _resolve_quiet(resolver, s, frozenset())
        inferred_ms, inferred_plan = _resolve_quiet(resolver, s, s.required_features)
        legacy_w = len(legacy_plan.indicator_columns)
        inferred_w = len(inferred_plan.indicator_columns)
        rows.append({
            "strategy": sid,
            "declared_required_features": declared,
            "legacy": {
                "resolve_ms": round(legacy_ms, 2),
                "indicator_cols": legacy_w,
                "base_cols": len(legacy_plan.base_columns),
            },
            "inferred": {
                "resolve_ms": round(inferred_ms, 2),
                "indicator_cols": inferred_w,
                "base_cols": len(inferred_plan.base_columns),
            },
            "indicator_width_ratio": round(legacy_w / inferred_w, 2) if inferred_w else None,
        })

    print(f"{'strategy':36} {'依赖':4} {'legacy列宽':>9} {'当前列宽':>8} {'宽度比':>6}")
    for r in rows:
        # 依赖列非空 = 显式声明或 P1a 推导; 空的唯一情形是不可证明 (保持全量兜底)。
        has_deps = "有" if r["declared_required_features"] else "无"
        ratio = r["indicator_width_ratio"]
        print(
            f"{r['strategy']:36} {has_deps:4} "
            f"{r['legacy']['indicator_cols']:>9} {r['inferred']['indicator_cols']:>8} "
            f"{(f'{ratio:.1f}x') if ratio else '—':>6}"
        )
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入 {args.json_path}")


if __name__ == "__main__":
    main()
