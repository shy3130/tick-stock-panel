"""P2b 基准: 自定义因子物化的热路径成本随因子数量的增长。

场景: 同一帧上反复物化 K 个自定义 (DSL) 因子 — 回测/筛选/评分的每次运行
都走 materialize_scoring_columns。验收 (docs/ext-architecture-plan.md P2):
新增 10 个自定义指标后热路径耗时不变 (内容寻址缓存命中时应近似平坦)。

用法: cd backend && uv run python scripts/bench_factor_materialize.py [--json PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

logging.getLogger("app").setLevel(logging.WARNING)

import polars as pl  # noqa: E402

from app.config import settings  # noqa: E402
from app.factors.registry import unregister_factor  # noqa: E402
from app.factors.store import register_definition  # noqa: E402

N_SYMBOLS = 4000
N_DAYS = 330


def _frame() -> pl.DataFrame:
    base = pl.datetime_range(
        __import__("datetime").date(2025, 1, 1),
        __import__("datetime").date(2026, 12, 31),
        "1d",
        eager=True,
    ).dt.date()
    days = base.head(N_DAYS)
    df = pl.DataFrame({"symbol": [f"{600000 + i:06d}.SH" for i in range(N_SYMBOLS)]}).join(
        pl.DataFrame({"date": days}), how="cross"
    )
    n = df.height
    import random

    rng = random.Random(7)
    closes = [10.0 + rng.uniform(-1, 1) + (i % N_DAYS) * 0.01 for i in range(n)]
    return df.with_columns(
        pl.Series("close", closes, dtype=pl.Float64),
        pl.Series("open", [c * 0.999 for c in closes], dtype=pl.Float64),
        pl.Series("volume", [rng.uniform(5e4, 5e6) for _ in range(n)], dtype=pl.Float64),
        pl.Series("amount", [rng.uniform(5e5, 5e7) for _ in range(n)], dtype=pl.Float64),
        pl.Series("turnover_rate", [rng.uniform(0.1, 15.0) for _ in range(n)], dtype=pl.Float64),
    ).sort(["symbol", "date"])


_FORMULAS = {
    "uf_bench_1": "ts_zscore(close, 20)",
    "uf_bench_2": "ts_mean(volume, 10) / ts_mean(volume, 60)",
    "uf_bench_3": "close / ts_mean(close, 60) - 1",
    "uf_bench_4": "ts_std(close, 20) / ts_mean(close, 20)",
    "uf_bench_5": "rank(ts_delta(close, 5))",
    "uf_bench_6": "ts_corr(close, volume, 20)",
    "uf_bench_7": "ts_sum(sign(close - ts_delay(close, 1)), 20)",
    "uf_bench_8": "ts_max(close, 20) / close - 1",
    "uf_bench_9": "ts_mean(turnover_rate, 5) / ts_mean(turnover_rate, 60)",
    "uf_bench_10": "ts_rank(close, 120)",
    "uf_bench_11": "winsorize(ts_zscore(amount, 20))",
    "uf_bench_12": "decay_linear(ts_delta(close, 5), 10)",
    "uf_bench_13": "zscore(ts_mean(volume, 20))",
}


def _register(names: list[str]) -> None:
    import contextlib

    for name in names:
        with contextlib.suppress(ValueError):
            unregister_factor(name)
        register_definition({
            "id": name, "label": name, "kind": "custom",
            "formula": _FORMULAS[name], "status": "active",
        })


def _time_materialize(frame: pl.DataFrame, names: list[str], repeats: int) -> list[float]:
    from app.strategy.scoring import materialize_scoring_columns

    out = []
    for _ in range(repeats):
        t = time.perf_counter()
        work = frame
        # 逐个物化 (与 scoring/backtest 调用同一条路径)
        result = materialize_scoring_columns(work, names)
        out.append(time.perf_counter() - t)
        assert all(name in result.columns for name in names)
        del result
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    from app.factors.materialize_cache import invalidate as _cache_invalidate

    _cache_invalidate(disk=True)  # 真冷启动 (磁盘缓存也清空)
    frame = _frame()
    row = {"rows": frame.height, "data_dir": str(settings.data_dir), "sets": {}}
    for label, names in (
        ("K3", list(_FORMULAS)[:3]),
        ("K13", list(_FORMULAS)),
    ):
        _register(names)
        times = _time_materialize(frame, names, repeats=4)
        row["sets"][label] = {
            "n_factors": len(names),
            "per_call_ms": [round(t * 1000, 1) for t in times],
            "warm_call_ms": round(sorted(times)[1] * 1000, 1),  # 第2-4次的代表值
        }
        # 清掉物化列, 下一组从干净帧开始
        frame = frame.select([c for c in frame.columns if not c.startswith("uf_")])
        for name in names:
            unregister_factor(name)

    row["warm_delta_ms_K3_to_K13"] = round(
        row["sets"]["K13"]["warm_call_ms"] - row["sets"]["K3"]["warm_call_ms"], 2
    )  # +10 因子的热路径绝对增量 (验收: 应近零)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
