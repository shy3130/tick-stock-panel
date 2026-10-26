"""统一定义契约 (P1b) — Store 基类 + kind 注册表的契约测试。

验收对象 (docs/ext-architecture-plan.md P1b):
  - JsonDefinitionStore 收口四处同型持久化 (信号/因子/监控规则/批次);
  - 新增一种 kind = 注册一个 DefinitionKind 描述器, 即复用同一
    校验/注册/依赖提取路径, 不再平行实现持久化与分发。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.definitions import (
    DefinitionKind,
    JsonDefinitionStore,
    definition_dependencies,
    get_kind,
    register_kind,
    registered_kinds,
    validate_definition,
)

# ── Store 基类 ───────────────────────────────────────────


def test_store_roundtrip_and_delete(tmp_path: Path) -> None:
    store = JsonDefinitionStore("user_data/things", label="thing")
    store.save_one(tmp_path, {"id": "t1", "v": 1})
    assert [d["id"] for d in store.load_all(tmp_path)] == ["t1"]
    assert store.load_one(tmp_path, "t1") == {"id": "t1", "v": 1}
    assert store.load_one(tmp_path, "missing") is None
    assert store.delete_one(tmp_path, "t1") is True
    assert store.delete_one(tmp_path, "t1") is False
    assert store.load_all(tmp_path) == []


def test_store_corrupt_file_skipped_not_fatal(tmp_path: Path) -> None:
    store = JsonDefinitionStore("user_data/things", label="thing")
    store.save_one(tmp_path, {"id": "t1", "v": 1})
    (tmp_path / "user_data" / "things" / "bad.json").write_text("{broken", encoding="utf-8")
    assert [d["id"] for d in store.load_all(tmp_path)] == ["t1"]


def test_store_normalize_and_glob_pattern(tmp_path: Path) -> None:
    store = JsonDefinitionStore(
        "user_data/things",
        glob_pattern="thing_*.json",
        label="thing",
        normalize=lambda d: {**d, "filled": True},
    )
    store.save_one(tmp_path, {"id": "thing_1"})
    store.save_one(tmp_path, {"id": "other"})  # 不匹配 glob, 不进读取面
    loaded = store.load_all(tmp_path)
    assert [d["id"] for d in loaded] == ["thing_1"]
    assert loaded[0]["filled"] is True


def test_store_fingerprint_tracks_changes(tmp_path: Path) -> None:
    store = JsonDefinitionStore("user_data/things", label="thing")
    fp0 = store.fingerprint(tmp_path)
    store.save_one(tmp_path, {"id": "t1"})
    fp1 = store.fingerprint(tmp_path)
    assert fp1 != fp0
    # mtime 精度内编辑也要能感知: 保存另一文件必然改变签名
    store.save_one(tmp_path, {"id": "t2"})
    assert store.fingerprint(tmp_path) != fp1
    store.delete_one(tmp_path, "t1")
    store.delete_one(tmp_path, "t2")
    assert store.fingerprint(tmp_path) == ()


# ── 内置 kind: 校验与依赖提取走统一分发 ──────────────────


def test_builtin_kinds_registered() -> None:
    kinds = registered_kinds()
    assert "signal" in kinds
    assert "factor" in kinds


def test_get_kind_lazy_imports_domain_module() -> None:
    """冷进程只导入 app.definitions 时, get_kind 惰性导入领域模块完成注册。

    已导入模块不会重新执行 (import 缓存), 因此不能用"弹出再取"模拟,
    必须以子进程冷启动验证。
    """
    import subprocess
    import sys

    code = (
        "from app.definitions import get_kind, registered_kinds;"
        "assert 'signal' in registered_kinds();"
        "assert get_kind('factor').kind == 'factor';"
        "print('lazy-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    )
    assert "lazy-ok" in result.stdout


def test_signal_validate_dispatch(tmp_path: Path) -> None:
    good = {
        "id": "test_sig", "name": "t", "kind": "both", "timeframe": "daily",
        "conditions": [{"left": "close", "op": ">", "right": "ma20"}],
    }
    validate_definition("signal", good)  # 不抛
    bad = dict(good, id="BAD")
    with pytest.raises(ValueError, match="id 非法"):
        validate_definition("signal", bad)


def test_signal_dependencies_reuse_compile_path() -> None:
    """信号依赖提取 = 编译表达式 root_names (与回测/管线同源)。

    symbol 来自日期偏移的 .over("symbol") 分组列 — 与
    pipeline.get_signal_dependencies 的既有口径一致。
    """
    sig = {
        "id": "test_sig", "name": "t", "kind": "both", "timeframe": "daily",
        "conditions": [
            {"left": "close", "op": ">", "right": "field:ma20", "rightDays": 1},
            {"left": "rsi_14", "op": "<", "right": 30},
        ],
    }
    deps = definition_dependencies("signal", sig)
    assert deps == frozenset({"close", "ma20", "rsi_14", "symbol"})


def test_signal_dependencies_intraday_branch() -> None:
    sig = {
        "id": "test_ia", "name": "t", "kind": "entry", "timeframe": "intraday",
        "conditions": [{"left": "price", "op": ">", "right": 0}],
    }
    deps = definition_dependencies("signal", sig)
    assert "price" in deps  # 盘中分支走 build_intraday_expressions


def test_factor_dependencies_reuse_dsl_compile() -> None:
    definition = {"id": "uf_x", "label": "x", "kind": "custom",
                  "formula": "close / ts_mean(turnover_rate, 20)"}
    assert definition_dependencies("factor", definition) == frozenset({"close", "turnover_rate"})
    with pytest.raises(ValueError, match="id 必须匹配"):
        validate_definition("factor", {**definition, "id": "zz"})


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="未知 definition kind"):
        validate_definition("nope", {})


def test_duplicate_kind_with_different_descriptor_rejected() -> None:
    with pytest.raises(ValueError, match="描述器不同"):
        register_kind(DefinitionKind(
            kind="signal",
            store=JsonDefinitionStore("user_data/x", label="x"),
            id_pattern=re.compile(r"^x$"),
            validate=lambda d: None,
        ))


# ── P1b 验收: 新增第三种 kind 复用同一管线 ────────────────
# 不写任何新的持久化/分发代码 — 只注册描述器, 存取/校验/依赖提取全部复用。


def test_new_kind_reuses_same_pipeline(tmp_path: Path) -> None:
    store = JsonDefinitionStore("user_data/scoring_defs", label="scoring def")
    seen: list[dict] = []

    def _validate(defn: dict) -> None:
        if not defn.get("weights"):
            raise ValueError("评分定义 weights 不能为空")

    def _deps(defn: dict) -> frozenset[str]:
        seen.append(defn)
        return frozenset(defn.get("weights", {}))

    register_kind(DefinitionKind(
        kind="scoring_test",
        store=store,
        id_pattern=re.compile(r"^sc_[a-z0-9_]{1,40}$"),
        validate=_validate,
        dependencies=_deps,
    ))
    try:
        # 存储: 同一 Store 基类
        definition = {"id": "sc_vol", "weights": {"close": 0.6, "volume": 0.4}}
        store.save_one(tmp_path, definition)
        assert store.load_all(tmp_path) == [definition]
        # 校验: 同一分发入口, 领域错误信息原样透出
        validate_definition("scoring_test", definition)
        with pytest.raises(ValueError, match="weights 不能为空"):
            validate_definition("scoring_test", {"id": "sc_x", "weights": {}})
        # 依赖提取: 同一分发入口
        assert definition_dependencies("scoring_test", definition) == frozenset({"close", "volume"})
        assert seen == [definition]
        # 描述器可从注册表取回 (P4 统一选择器/表单的消费形态)
        assert get_kind("scoring_test").store is store
    finally:
        import app.definitions as defs

        defs._KINDS.pop("scoring_test", None)


def test_kind_without_extractor_returns_empty(tmp_path: Path) -> None:
    """无依赖提取器的 kind (纯记录类) → 空集, 不报错。"""
    register_kind(DefinitionKind(
        kind="record_test",
        store=JsonDefinitionStore("user_data/records", label="record"),
        id_pattern=re.compile(r"^r_"),
        validate=lambda d: None,
    ))
    try:
        assert definition_dependencies("record_test", {"id": "r_1"}) == frozenset()
    finally:
        import app.definitions as defs

        defs._KINDS.pop("record_test", None)


# ── 四处存量收口的回归锚点 (公开签名不变, 行为等价) ────────


def test_existing_modules_share_store_base(tmp_path: Path) -> None:
    """信号/因子/监控规则/批次的持久化函数全部委托统一 Store。"""
    from app.factors import store as factor_store
    from app.strategy import custom_signals, lots, monitor_rules

    assert isinstance(custom_signals._STORE, JsonDefinitionStore)
    assert isinstance(factor_store._STORE, JsonDefinitionStore)
    assert isinstance(monitor_rules._RULE_STORE, JsonDefinitionStore)
    assert isinstance(lots._LOT_STORE, JsonDefinitionStore)

    # 批次语义: 非 lot_ 前缀文件不入读取面 (glob 参数化生效)
    lots.save_one(tmp_path, {
        "id": "lot_1", "symbol": "000001", "name": "x",
        "buy_date": "2026-01-05", "qty": 100, "cost": 10.0, "target_pct": 20,
    })
    (tmp_path / "user_data" / "lots" / "stray.json").write_text(json.dumps({"id": "stray"}), encoding="utf-8")
    assert [lot["id"] for lot in lots.load_all(tmp_path)] == ["lot_1"]

    # 监控规则语义: 读取归一化默认值照常回填
    monitor_rules.save_one(tmp_path, {
        "id": "r1", "type": "price", "symbols": ["000001"],
        "op": "above", "field": "close", "threshold": 10,
    })
    rule = monitor_rules.load_one(tmp_path, "r1")
    assert rule["enabled"] is True and rule["scope"] == "symbols"
