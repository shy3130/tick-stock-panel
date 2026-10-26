"""L2 内容寻址因子列缓存 (P2b) 契约测试。

验收对象 (docs/ext-architecture-plan.md P2): 新增自定义因子后, 重复求值
的热路径耗时不变。正确性契约:
  - 命中 (进程内 / 磁盘) 与现算结果逐元素一致;
  - 定义变更 / 输入值变更 → 键变 → 自动失效 (内容寻址即失效传播);
  - 一切缓存故障 (目录只读 / parquet 损坏) 回退现算 (fail-open);
  - 小帧旁路 (哈希+IO 比现算贵, 不缓存)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.config import settings
from app.factors import materialize_cache
from app.factors.registry import unregister_factor
from app.factors.store import register_definition

_REGISTERED: list[str] = []


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    """缓存写到临时目录; 每个测试从全空缓存开始。

    收尾同时注销本测试注册的 uf_* 因子 — 注册表是进程级全局,
    残留会污染 test_factor_registry 的快照断言 (累积运行必挂)。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    materialize_cache.invalidate(disk=True)
    yield
    import contextlib

    for fid in _REGISTERED:
        with contextlib.suppress(ValueError):
            unregister_factor(fid)
    _REGISTERED.clear()
    materialize_cache.invalidate(disk=True)


def _frame(n: int = 60_000, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    return pl.DataFrame({
        "symbol": ["600000.SH"] * n,
        "date": np.arange(n, dtype=np.int64),
        "close": rng.normal(10, 1, n),
        "volume": rng.uniform(1e4, 1e6, n),
    })


def _register(fid: str, formula: str, version: int = 1) -> None:
    import contextlib

    with contextlib.suppress(ValueError):
        unregister_factor(fid)
    register_definition({
        "id": fid, "label": fid, "kind": "custom",
        "formula": formula, "status": "active", "version": version,
    })
    _REGISTERED.append(fid)


def test_hit_matches_fresh_computation_lru_and_disk() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    _register("uf_c1", "ts_zscore(close, 20)")
    frame = _frame()
    cold = materialize_scoring_columns(frame, ["uf_c1"])
    assert materialize_cache.stats()["miss"] == 1

    warm = materialize_scoring_columns(frame, ["uf_c1"])
    assert materialize_cache.stats()["lru_hit"] == 1
    assert warm["uf_c1"].equals(cold["uf_c1"])

    materialize_cache.invalidate()  # 清 LRU, 磁盘保留
    disk_warm = materialize_scoring_columns(frame, ["uf_c1"])
    assert materialize_cache.stats()["disk_hit"] == 1
    assert disk_warm["uf_c1"].equals(cold["uf_c1"])


def test_input_value_change_invalidates() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    _register("uf_c2", "ts_mean(close, 30)")
    base = materialize_scoring_columns(_frame(seed=1), ["uf_c2"])
    changed = materialize_scoring_columns(_frame(seed=1).with_columns(pl.col("close") * 2.0), ["uf_c2"])
    assert not base["uf_c2"].equals(changed["uf_c2"])
    assert materialize_cache.stats()["miss"] == 2  # 两次都是未命中 (键不同)


def test_formula_change_invalidates() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    frame = _frame()
    _register("uf_c3", "ts_zscore(close, 20)")
    v1 = materialize_scoring_columns(frame, ["uf_c3"])
    _register("uf_c3", "ts_zscore(close, 60)", version=2)
    v2 = materialize_scoring_columns(frame, ["uf_c3"])
    assert not v1["uf_c3"].equals(v2["uf_c3"])


def test_corrupt_disk_cache_falls_back_to_recompute() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    _register("uf_c4", "ts_std(close, 20)")
    frame = _frame()
    cold = materialize_scoring_columns(frame, ["uf_c4"])
    materialize_cache.invalidate()  # 磁盘上留有 key.parquet

    root = Path(settings.data_dir) / "cache" / "factor_columns"
    files = list(root.glob("*.parquet"))
    assert len(files) == 1
    files[0].write_bytes(b"broken-not-parquet")

    recovered = materialize_scoring_columns(frame, ["uf_c4"])
    assert recovered["uf_c4"].equals(cold["uf_c4"])  # 损坏 → 回退现算, 结果一致
    assert materialize_cache.stats()["error"] == 1


def test_small_frame_bypasses_cache() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    _register("uf_c5", "ts_mean(volume, 10)")
    small = _frame(n=1_000)
    result = materialize_scoring_columns(small, ["uf_c5"])
    assert "uf_c5" in result.columns
    assert materialize_cache.stats()["bypass_small"] == 1
    assert materialize_cache.stats()["miss"] == 0  # 小帧完全没进缓存路径


def test_staged_temp_columns_do_not_leak() -> None:
    """现算与命中两条路径的输出帧列集合一致 (DSL 分阶段临时列不泄漏)。"""
    from app.strategy.scoring import materialize_scoring_columns

    # 嵌套窗口公式会生成分阶段临时列
    _register("uf_c6", "ts_mean(ts_zscore(close, 20), 10)")
    frame = _frame()
    cold = materialize_scoring_columns(frame, ["uf_c6"])
    warm = materialize_scoring_columns(frame, ["uf_c6"])
    assert cold.columns == warm.columns
    assert set(warm.columns) - set(frame.columns) == {"uf_c6"}
