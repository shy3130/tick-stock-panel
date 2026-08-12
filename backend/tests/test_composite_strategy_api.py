"""叠加策略 API 与删除防护测试 (M2)。

覆盖:
- _render_composite_code 渲染声明式 .py 正确
- _save_composite_strategy 创建/更新/校验(前缀/嵌套/子策略不存在/重复创建)
- _strategy_detail 回显 composite_children
- delete_strategy 删除被引用子策略时 409 fail-closed

复用 test_strategy_code_save.py 的 SimpleNamespace 构造模式。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.strategy import (
    CompositeChildItem,
    StrategyCompositeSaveRequest,
    _render_composite_code,
    _save_composite_strategy,
    _strategy_detail,
)
from app.strategy.engine import StrategyEngine


def _filter_code(strategy_id: str) -> str:
    meta = (
        f'{{"id": "{strategy_id}", "name": "{strategy_id}", '
        '"asset_types": ["stock"], "timeframes": ["1d"]}'
    )
    return f'''import polars as pl
META = {meta}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
'''


def _setup_engine(tmp_path):
    """构造含 custom + composite 目录的 engine 和假 request。"""
    custom_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    custom_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    engine = StrategyEngine(strategy_dirs=[custom_dir, comp_dir])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine))
    )
    return engine, request, custom_dir, comp_dir


def _composite_req(sid, children, **kw):
    return StrategyCompositeSaveRequest(
        strategy_id=sid,
        name=kw.get("name", sid),
        description=kw.get("description", ""),
        children=[CompositeChildItem(strategy_id=c, weight=w) for c, w in children],
        merge_mode=kw.get("merge_mode", "union"),
        min_confirm=kw.get("min_confirm", 0),
        mode=kw.get("mode", "create"),
    )


# ───────────────────────── 渲染 ─────────────────────────


def test_render_composite_code_contains_children_and_backend():
    code = _render_composite_code(
        "composite_blend", "我的叠加", "描述",
        [{"strategy_id": "child_a", "weight": 0.4}, {"strategy_id": "child_b", "weight": 0.6}],
        "union", 0,
    )
    assert 'EXECUTION_BACKEND = "composite"' in code
    assert "composite_blend" in code
    assert "child_a" in code
    assert "child_b" in code
    assert "0.4" in code


# ───────────────────────── 保存 ─────────────────────────


def test_save_composite_creates_and_loads(tmp_path):
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    engine.reload()

    result = _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 1.0)]), request
    )
    assert result["ok"] is True
    assert result["source"] == "composite"
    assert engine.has("composite_blend")
    blend = engine.get("composite_blend")
    assert blend.execution_backend == "composite"
    assert blend.composite.children[0].strategy_id == "child_a"


def test_save_composite_rejects_missing_composite_prefix(tmp_path):
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    engine.reload()

    with pytest.raises(ValueError, match="composite_"):
        _save_composite_strategy(
            _composite_req("bad_prefix", [("child_a", 1.0)]), request
        )


def test_save_composite_rejects_missing_child(tmp_path):
    _engine, request, _, _ = _setup_engine(tmp_path)

    with pytest.raises(ValueError, match="不存在"):
        _save_composite_strategy(
            _composite_req("composite_blend", [("ghost", 1.0)]), request
        )


def test_save_composite_rejects_nested_composite(tmp_path):
    engine, request, custom_dir, _comp_dir = _setup_engine(tmp_path)
    (custom_dir / "leaf.py").write_text(_filter_code("leaf"), encoding="utf-8")
    engine.reload()
    # 先创建一个合法 composite
    _save_composite_strategy(
        _composite_req("composite_inner", [("leaf", 1.0)]), request
    )

    with pytest.raises(ValueError, match="嵌套"):
        _save_composite_strategy(
            _composite_req("composite_outer", [("composite_inner", 1.0)]), request
        )


def test_save_composite_update_mode(tmp_path):
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    (custom_dir / "child_b.py").write_text(_filter_code("child_b"), encoding="utf-8")
    engine.reload()
    _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 1.0)]), request
    )

    # 更新: 改子策略
    result = _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 0.5), ("child_b", 0.5)], mode="update"),
        request,
    )
    assert result["ok"]
    blend = engine.get("composite_blend")
    assert {c.strategy_id for c in blend.composite.children} == {"child_a", "child_b"}


def test_save_composite_update_rejects_non_composite(tmp_path):
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    # 一个普通(polars_expr)策略, 用 composite_ 前缀以通过 id 校验进入后续检查
    (custom_dir / "composite_plain.py").write_text(_filter_code("composite_plain"), encoding="utf-8")
    engine.reload()

    with pytest.raises(ValueError, match="不是叠加策略"):
        _save_composite_strategy(
            _composite_req("composite_plain", [], mode="update"), request
        )


# ───────────────────────── _strategy_detail ─────────────────────────


def test_strategy_detail_shows_composite_children(tmp_path):
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    (custom_dir / "child_b.py").write_text(_filter_code("child_b"), encoding="utf-8")
    engine.reload()
    _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 0.4), ("child_b", 0.6)]), request
    )

    detail = _strategy_detail(engine.get("composite_blend"))
    assert detail["execution_backend"] == "composite"
    assert detail["composite_children"] is not None
    children_map = {c["id"]: c["weight"] for c in detail["composite_children"]}
    assert children_map == {"child_a": 0.4, "child_b": 0.6}


def test_strategy_detail_non_composite_has_null_children(tmp_path):
    engine, _request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    engine.reload()

    detail = _strategy_detail(engine.get("child_a"))
    assert detail["composite_children"] is None


# ───────────────────────── 删除防护 ─────────────────────────


def test_delete_referenced_child_blocked(tmp_path):
    """删除被 composite 引用的子策略 → 409 fail-closed。"""
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    engine.reload()
    _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 1.0)]), request
    )

    # 直接调端点函数(delete_strategy 需要 request)
    from app.api.strategy import delete_strategy

    with pytest.raises(HTTPException) as exc_info:
        delete_strategy("child_a", request)
    assert exc_info.value.status_code == 409
    assert "composite_blend" in exc_info.value.detail


def test_delete_composite_itself_succeeds(tmp_path):
    """删除 composite 本身(不被引用)应成功。"""
    engine, request, custom_dir, _ = _setup_engine(tmp_path)
    (custom_dir / "child_a.py").write_text(_filter_code("child_a"), encoding="utf-8")
    engine.reload()
    _save_composite_strategy(
        _composite_req("composite_blend", [("child_a", 1.0)]), request
    )

    from app.api.strategy import delete_strategy

    result = delete_strategy("composite_blend", request)
    assert result["ok"] is True
    assert not engine.has("composite_blend")
