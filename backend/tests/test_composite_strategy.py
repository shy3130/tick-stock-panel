"""叠加策略 (composite) 加载、引用校验与选股合并测试。

适配当前掩码驱动架构: 不依赖 matrix 模块, 子策略为 polars_expr。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.strategy.engine import (
    MAX_COMPOSITE_CHILDREN,
    StrategyEngine,
    _parse_composite_children,
)


def _filter_strategy_code(strategy_id: str, body: str = "return pl.lit(True)") -> str:
    """生成一个 polars_expr 策略文件(filter 始终命中全部标的)。"""
    return f'''"""策略 {strategy_id}。"""
import polars as pl

META = {{
    "id": {strategy_id!r},
    "name": {strategy_id!r},
    "asset_types": ["stock"],
    "scoring": {{"close": 1.0}},
    "limit": 100,
}}

def filter(df, params):
    {body}
'''


def _composite_code(
    sid: str,
    children: list[dict],
    *,
    asset_types: list[str] | None = None,
    merge_mode: str = "union",
    min_confirm: int = 0,
) -> str:
    """生成一个声明式 composite 策略文件。"""
    import json as _json

    children_json = ",\n        ".join(
        _json.dumps({"strategy_id": c["strategy_id"], "weight": c["weight"]}, ensure_ascii=False)
        for c in children
    )
    return f'''"""叠加策略 {sid}（测试生成）。"""
META = {{
    "id": {sid!r},
    "name": {sid!r},
    "asset_types": {asset_types or ["stock"]!r},
    "params": [
        {{"id": "merge_mode", "type": "select", "options": ["union", "intersect"], "default": {merge_mode!r}}},
        {{"id": "min_confirm", "type": "int", "default": {int(min_confirm)!r}, "min": 0}},
    ],
    "children": [
        {children_json}
    ],
}}
EXECUTION_BACKEND = "composite"
'''


def _engine(tmp_path: Path, panel: pl.DataFrame | None = None) -> StrategyEngine:
    cdir = tmp_path / "custom"
    cdir.mkdir(parents=True, exist_ok=True)
    df = panel if panel is not None else pl.DataFrame()
    return StrategyEngine(lambda _: df, strategy_dirs=[cdir])


# ───────────────────────── 加载与引用校验 ─────────────────────────


def test_composite_loads_and_infers_source(tmp_path):
    (tmp_path / "custom").mkdir(parents=True)
    (tmp_path / "custom" / "child_a.py").write_text(
        _filter_strategy_code("child_a"), encoding="utf-8"
    )
    (tmp_path / "custom" / "child_b.py").write_text(
        _filter_strategy_code("child_b"), encoding="utf-8"
    )
    (tmp_path / "custom" / "composite_x.py").write_text(
        _composite_code(
            "composite_x",
            [{"strategy_id": "child_a", "weight": 0.5}, {"strategy_id": "child_b", "weight": 0.5}],
        ),
        encoding="utf-8",
    )
    eng = _engine(tmp_path)
    ids = {s["id"] for s in eng.list_strategies()}
    assert "composite_x" in ids
    s = eng.get("composite_x")
    assert s.execution_backend == "composite"
    assert s.source == "composite"
    assert s.composite is not None
    assert len(s.composite.children) == 2
    assert eng.load_errors() == []


def test_composite_missing_child_is_orphaned_without_blocking_others(tmp_path):
    (tmp_path / "custom").mkdir(parents=True)
    (tmp_path / "custom" / "child_a.py").write_text(
        _filter_strategy_code("child_a"), encoding="utf-8"
    )
    (tmp_path / "custom" / "ghost.py").write_text(
        _composite_code(
            "ghost", [{"strategy_id": "ghost_child", "weight": 1.0}]
        ),
        encoding="utf-8",
    )
    eng = _engine(tmp_path)
    ids = {s["id"] for s in eng.list_strategies()}
    assert "ghost" not in ids  # 孤儿被移除
    assert "child_a" in ids  # 但不影响其他策略
    errors = eng.load_errors()
    assert any("ghost" in e["error"] or "ghost_child" in e["error"] for e in errors)


def test_nested_composite_rejected(tmp_path):
    (tmp_path / "custom").mkdir(parents=True)
    (tmp_path / "custom" / "child_a.py").write_text(
        _filter_strategy_code("child_a"), encoding="utf-8"
    )
    (tmp_path / "custom" / "inner.py").write_text(
        _composite_code("inner", [{"strategy_id": "child_a", "weight": 1.0}]),
        encoding="utf-8",
    )
    (tmp_path / "custom" / "outer.py").write_text(
        _composite_code("outer", [{"strategy_id": "inner", "weight": 1.0}]),
        encoding="utf-8",
    )
    eng = _engine(tmp_path)
    ids = {s["id"] for s in eng.list_strategies()}
    assert "outer" not in ids
    errors = eng.load_errors()
    assert any("嵌套" in e["error"] or "nested" in e["error"].lower() for e in errors)


def test_composite_exceeds_child_limit_rejected(tmp_path):
    (tmp_path / "custom").mkdir(parents=True)
    for i in range(MAX_COMPOSITE_CHILDREN + 1):
        (tmp_path / "custom" / f"c{i}.py").write_text(
            _filter_strategy_code(f"c{i}"), encoding="utf-8"
        )
    children = [{"strategy_id": f"c{i}", "weight": 1.0} for i in range(MAX_COMPOSITE_CHILDREN + 1)]
    (tmp_path / "custom" / "big.py").write_text(
        _composite_code("big", children), encoding="utf-8"
    )
    eng = _engine(tmp_path)
    assert "big" not in {s["id"] for s in eng.list_strategies()}
    errors = eng.load_errors()
    assert any("limit" in e["error"] or "exceed" in e["error"] for e in errors)


# ───────────────────────── find_dependents ─────────────────────────


def test_find_dependents_locates_referencing_composites(tmp_path):
    (tmp_path / "custom").mkdir(parents=True)
    (tmp_path / "custom" / "child_a.py").write_text(
        _filter_strategy_code("child_a"), encoding="utf-8"
    )
    (tmp_path / "custom" / "composite_x.py").write_text(
        _composite_code("composite_x", [{"strategy_id": "child_a", "weight": 1.0}]),
        encoding="utf-8",
    )
    eng = _engine(tmp_path)
    assert eng.find_dependents("child_a") == ["composite_x"]
    assert eng.find_dependents("child_b") == []
    assert eng.find_dependents("nonexistent") == []


# ───────────────────────── 选股合并 ─────────────────────────


def _panel() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["S1", "S2", "S3", "S4"],
            "date": [date(2026, 1, 1)] * 4,
            "close": [5.0, 10.0, 3.0, 8.0],
            "amount": [2e8, 0.6e8, 3e8, 0.1e8],
            "name": ["A", "B", "C", "D"],
            "total_shares": [1e8] * 4,
            "float_shares": [1e8] * 4,
            "turnover_rate": [1.0] * 4,
        }
    )


def _composite_engine(tmp_path: Path):
    (tmp_path / "custom").mkdir(parents=True)
    (tmp_path / "custom" / "child_a.py").write_text(
        _filter_strategy_code("child_a", body='return pl.col("close") > 4'),
        encoding="utf-8",
    )
    (tmp_path / "custom" / "child_b.py").write_text(
        _filter_strategy_code("child_b", body='return pl.col("amount") > 0.5e8'),
        encoding="utf-8",
    )
    # 禁用 basic_filter 以隔离测试
    code = _composite_code(
        "ct",
        [{"strategy_id": "child_a", "weight": 0.6}, {"strategy_id": "child_b", "weight": 0.4}],
    ).replace('"children":', '"basic_filter": {"enabled": False}, "children":')
    (tmp_path / "custom" / "ct.py").write_text(code, encoding="utf-8")
    return StrategyEngine(lambda _: _panel(), strategy_dirs=[tmp_path / "custom"])


def test_composite_union_merge(tmp_path):
    eng = _composite_engine(tmp_path)
    res = eng.run("ct", date(2026, 1, 1), params={"merge_mode": "union", "min_confirm": 0})
    # composite score is weighted normalized child rank; worst ranked candidate may be 0.
    assert set(res.scores.keys()) == {"S1", "S2", "S3", "S4"}
    assert all(0 <= value <= 100 for value in res.scores.values())


def test_composite_intersect_merge(tmp_path):
    eng = _composite_engine(tmp_path)
    res = eng.run("ct", date(2026, 1, 1), params={"merge_mode": "intersect", "min_confirm": 2})
    # intersect min_confirm=2: S1,S2 同时被两个子策略命中
    assert set(res.scores.keys()) == {"S1", "S2"}


def test_composite_weighted_score_ranking(tmp_path):
    eng = _composite_engine(tmp_path)
    res = eng.run("ct", date(2026, 1, 1), params={"merge_mode": "union", "min_confirm": 0})
    # Both children rank by their declared score (close); weighted rank fusion must
    # preserve that order rather than treat a child-only hit as automatically best.
    assert res.scores["S2"] > res.scores["S4"] > res.scores["S1"] > res.scores["S3"]


def test_parse_composite_children_validates():
    spec = _parse_composite_children(
        [{"strategy_id": "a", "weight": 0.6}, {"strategy_id": "b", "weight": 0.4}]
    )
    assert len(spec.children) == 2
    assert spec.children[0].weight == 0.6


def test_parse_composite_children_rejects_duplicate():
    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        _parse_composite_children(
            [{"strategy_id": "a", "weight": 1.0}, {"strategy_id": "a", "weight": 1.0}]
        )
