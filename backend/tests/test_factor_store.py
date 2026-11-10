"""自定义/复合因子存储与 scoring 桥测试 (P3)。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors import store
from app.factors.registry import (
    FactorSpec,
    all_factors,
    factor_columns_view,
    get_factor,
    unregister_factor,
)
from app.strategy import scoring


@pytest.fixture()
def cleanup_registry():
    """测试注册的自定义因子在用例后清理, 不污染全局注册表。"""
    before = set()
    yield before
    for fid in before:
        unregister_factor(fid)


def _panel(n_days: int = 30) -> pl.DataFrame:
    rows = []
    volumes = {"A": 1000.0, "B": 3000.0, "C": 2000.0}
    for index in range(n_days):
        for symbol, close in (("A", 10.0 + index), ("B", 50.0 - index), ("C", 20.0 + index * 2)):
            rows.append({
                "symbol": symbol, "date": date(2026, 1, index + 1),
                "close": close, "volume": volumes[symbol] + index, "amount": (1000.0 + index) * close,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])

@pytest.fixture(autouse=True)
def _isolate_runtime_ext_factors(tmp_path, monkeypatch):
    """扩展因子按 settings.data_dir 惰性注册: 计数/顺序黄金断言必须与
    运行时 data/ 目录的扩展表配置隔离, 否则结果依赖本机数据。"""
    from app import config as app_config
    from app.factors import ext_factors

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    # 主动清掉其他测试泄漏进注册表的 ext_ 条目, 保证黄金断言密闭
    from app.factors.registry import _REGISTRY

    for fid in [k for k in list(_REGISTRY) if k.startswith(ext_factors.EXT_PREFIX)]:
        _REGISTRY.pop(fid, None)
    yield
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None



def test_custom_factor_definition_roundtrip(tmp_path, cleanup_registry) -> None:
    definition = {
        "id": "uf_test_rev", "kind": "custom", "version": 1, "label": "测试反转",
        "group": "自定义", "formula": "rank(-ts_sum(close / ts_delay(close, 1) - 1, 5))",
        "description": "", "direction": "low", "status": "draft",
    }
    spec = store.register_definition(definition)
    cleanup_registry.add("uf_test_rev")
    assert spec.kind == "custom"
    assert "close" in spec.dependencies
    assert spec.warmup_bars >= 6

    store.save_one(tmp_path, definition)
    loaded = store.load_all(tmp_path)
    assert len(loaded) == 1 and loaded[0]["id"] == "uf_test_rev"

    # 目录视图与 all_factors 追加动态因子
    ids = [item["id"] for item in factor_columns_view()]
    assert ids[:77] == [item["id"] for item in factor_columns_view()[:77]]
    assert "uf_test_rev" in ids and ids.index("uf_test_rev") >= 77
    assert any(s.id == "uf_test_rev" for s in all_factors())

    # 快照约束不受影响: 未注册动态因子时目录 = 77 内置
    unregister_factor("uf_test_rev")
    assert len(factor_columns_view()) == 77


def test_custom_factor_invalid_rejected(cleanup_registry) -> None:
    with pytest.raises(ValueError, match="E005"):
        store.register_definition({
            "id": "uf_bad", "kind": "custom", "label": "坏因子",
            "formula": "ts_delay(close, -3)", "status": "draft",
        })
    with pytest.raises(ValueError, match="uf_"):
        store.register_definition({
            "id": "wrong_prefix", "kind": "custom", "label": "坏前缀",
            "formula": "close", "status": "draft",
        })


def test_composite_definition_and_cycle_guard(cleanup_registry) -> None:
    definition = {
        "id": "cf_test_combo", "kind": "composite", "version": 1, "label": "测试组合",
        "members": {"momentum_20d": 0.6, "turnover_rate": 0.4}, "status": "draft",
    }
    spec = store.register_definition(definition)
    cleanup_registry.add("cf_test_combo")
    assert spec.kind == "composite"
    assert spec.components == (("momentum_20d", 0.6), ("turnover_rate", 0.4))
    assert spec.dependencies == frozenset({"momentum_20d", "turnover_rate"})

    # 自引用拒绝
    with pytest.raises(ValueError, match="自身"):
        store.to_spec({**definition, "id": "cf_self", "members": {"cf_self": 1.0, "close": 1.0}})


def test_scoring_bridge_composite(cleanup_registry) -> None:
    """复合因子经 scoring 物化: 截面加权 z 分可计算且依赖展开正确。"""
    store.register_definition({
        "id": "cf_ztest", "kind": "composite", "version": 1, "label": "桥接测试",
        "members": {"close": 0.5, "volume": 0.5}, "status": "active",
    })
    cleanup_registry.add("cf_ztest")

    deps = scoring.scoring_dependencies({"cf_ztest": 1.0})
    assert deps == {"close", "volume"}
    assert scoring.scoring_warmup_bars({"cf_ztest": 1.0}) >= 1

    frame = scoring.materialize_scoring_columns(_panel(), {"cf_ztest"})
    assert "cf_ztest" in frame.columns
    values = frame.filter(pl.col("date") == date(2026, 1, 10))["cf_ztest"]
    assert values.is_not_null().all()
    # 截面 z 之和的均值近似为 0 (等权两成员)
    assert abs(values.mean()) < 1e-9


def test_scoring_bridge_custom_materializes(cleanup_registry) -> None:
    """自定义 DSL 因子经 materialize_scoring_columns 物化 (与检验共用路径)。"""
    store.register_definition({
        "id": "uf_rank_close", "kind": "custom", "version": 1, "label": "价格排名",
        "formula": "rank(close)", "status": "draft",
    })
    cleanup_registry.add("uf_rank_close")
    frame = scoring.materialize_scoring_columns(_panel(), {"uf_rank_close"})
    assert "uf_rank_close" in frame.columns
    day = frame.filter(pl.col("date") == date(2026, 1, 1))
    assert day["uf_rank_close"].is_not_null().all()


def test_load_into_registry_isolated_failure(tmp_path, cleanup_registry) -> None:
    good = {
        "id": "uf_good", "kind": "custom", "version": 1, "label": "好因子",
        "formula": "close + 1", "status": "draft",
    }
    store.save_one(tmp_path, good)
    (tmp_path / "user_data" / "custom_factors" / "uf_broken.json").write_text(
        "{ not json", encoding="utf-8"
    )
    loaded = store.load_into_registry(tmp_path)
    assert loaded == ["uf_good"]
    cleanup_registry.add("uf_good")


def test_unregister_builtin_rejected() -> None:
    with pytest.raises(ValueError, match="内置"):
        unregister_factor("rsi_14")
    spec = get_factor("rsi_14")
    assert isinstance(spec, FactorSpec)


def test_load_custom_dependency_chain_independent_of_filename_order(tmp_path, cleanup_registry):
    for index in range(5):
        fid = f"uf_chain_{index}"
        formula = f"uf_chain_{index + 1} * 2" if index < 4 else "close"
        store.save_one(tmp_path, {"id": fid, "label": fid, "kind": "custom", "formula": formula})
        cleanup_registry.add(fid)
    assert set(store.load_into_registry(tmp_path)) == {f"uf_chain_{i}" for i in range(5)}
