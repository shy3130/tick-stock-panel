"""策略包 (bundle) — 依赖闭包、导出/导入往返与安全边界。

覆盖: 自包含/因子引用(SCORING·META.scoring)/信号引用(composite 成员递归) 三类
闭包; 缺失引用只告警不阻断; 往返恢复 + 冲突两态; 越界条目拒绝 (策略包不能
变成配置走私通道)。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.factors import store as factor_store
from app.factors.registry import unregister_factor
from app.services import strategy_bundle
from app.strategy.engine import StrategyEngine

META_COMMON = '"asset_types": ["stock"], "timeframes": ["1d"]'

STANDALONE = f'''\
META = {{"id": "custom_standalone", "name": "独立", "description": "d", "tags": [], {META_COMMON}}}

import polars as pl


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

FACTOR_RANK = f'''\
META = {{"id": "custom_rank", "name": "排名", "description": "d", "tags": [], {META_COMMON},
       "scoring": {{"uf_meta": 1.0, "momentum_5d": 0.5}}}}

import polars as pl

SCORING = {{"uf_dep": 1.0, "momentum_5d": 0.5}}


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

SIG_STRAT = f'''\
META = {{"id": "custom_sigstrat", "name": "信号", "description": "d", "tags": [], {META_COMMON}}}

import polars as pl

ENTRY_SIGNALS = ["csg_sig_dep", "signal_builtin_x"]


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

MISSING_REFS = f'''\
META = {{"id": "custom_missing", "name": "缺依赖", "description": "d", "tags": [], {META_COMMON}}}

import polars as pl

SCORING = {{"uf_gone": 1.0}}
ENTRY_SIGNALS = ["csg_gone"]


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

COMPOSITE = f'''\
META = {{"id": "custom_comp", "name": "叠加", "description": "d", "tags": [], {META_COMMON},
       "children": [{{"strategy_id": "custom_standalone", "weight": 1.0}}]}}

EXECUTION_BACKEND = "composite"
'''

UF_DEP = {"id": "uf_dep", "label": "依赖因子", "kind": "custom", "formula": "close", "lang": "dsl"}
UF_META = {"id": "uf_meta", "label": "META因子", "kind": "custom", "formula": "close", "lang": "dsl"}
UF_DEP2 = {"id": "uf_dep2", "label": "信号因子", "kind": "custom", "formula": "close", "lang": "dsl"}
SIG_DEP = {
    "id": "sig_dep",
    "name": "信号依赖",
    "kind": "entry",
    "timeframe": "daily",
    "conditions": [{"left": "close", "op": ">", "right": "field:uf_dep2"}],
}


def _write(path: Path, content: str | bytes | dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    elif isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def bundle_env(tmp_path, monkeypatch):
    """源环境: data_dir 指到 tmp, 策略/因子/信号/调参齐备, 注册表同步后建引擎。"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    strat_dir = tmp_path / "strategies" / "custom"
    _write(strat_dir / "custom_standalone.py", STANDALONE)
    _write(strat_dir / "custom_rank.py", FACTOR_RANK)
    _write(strat_dir / "custom_sigstrat.py", SIG_STRAT)
    _write(strat_dir / "custom_missing.py", MISSING_REFS)
    _write(strat_dir / "custom_comp.py", COMPOSITE)
    _write(tmp_path / "user_data" / "custom_factors" / "uf_dep.json", UF_DEP)
    _write(tmp_path / "user_data" / "custom_factors" / "uf_meta.json", UF_META)
    _write(tmp_path / "user_data" / "custom_factors" / "uf_dep2.json", UF_DEP2)
    _write(tmp_path / "user_data" / "custom_signals" / "sig_dep.json", SIG_DEP)
    _write(
        tmp_path / "user_data" / "strategy_overrides" / "custom_standalone.json",
        '{"params": {"n": 5}}',
    )
    loaded = factor_store.load_into_registry(tmp_path)
    yield tmp_path
    for fid in loaded:
        unregister_factor(fid)


@pytest.fixture()
def engine(bundle_env):
    return StrategyEngine(strategy_dirs=[bundle_env / "strategies" / "custom"])


def _zip_names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename for i in zf.infolist() if not i.is_dir()}


def test_standalone_bundle_carries_override(bundle_env, engine):
    data, filename, summary = strategy_bundle.build_bundle(
        bundle_env, engine, "custom_standalone"
    )
    names = _zip_names(data)
    assert "strategies/custom/custom_standalone.py" in names
    assert "user_data/strategy_overrides/custom_standalone.json" in names
    assert filename == "tsp-strategy-custom_standalone.zip"
    assert summary.counts == {"strategies": 1, "strategy_overrides": 1}
    assert summary.warnings == []


def test_scoring_factor_closure(bundle_env, engine):
    data, _, summary = strategy_bundle.build_bundle(bundle_env, engine, "custom_rank")
    names = _zip_names(data)
    # META.scoring 与模块 SCORING 两处的自定义因子都进包; 内置因子 momentum_5d 不进
    assert "user_data/custom_factors/uf_dep.json" in names
    assert "user_data/custom_factors/uf_meta.json" in names
    assert not any(n.endswith("momentum_5d.json") for n in names)
    assert summary.counts["custom_factors"] == 2
    assert summary.warnings == []


def test_signal_closure_recursive(bundle_env, engine):
    data, _, summary = strategy_bundle.build_bundle(bundle_env, engine, "custom_sigstrat")
    names = _zip_names(data)
    # csg_ 引用 → 信号定义; 信号条件又引用 uf_dep2 → 因子定义
    assert "user_data/custom_signals/sig_dep.json" in names
    assert "user_data/custom_factors/uf_dep2.json" in names
    # signal_builtin_x 未命中且属内置命名空间 → 静默跳过 (不产生告警)
    assert summary.warnings == []
    assert summary.counts["custom_signals"] == 1


def test_missing_refs_only_warn(bundle_env, engine):
    data, _, summary = strategy_bundle.build_bundle(bundle_env, engine, "custom_missing")
    names = _zip_names(data)
    assert "strategies/custom/custom_missing.py" in names
    assert not any("uf_gone" in n for n in names)
    assert not any("csg_gone" in n for n in names)
    joined = "\n".join(summary.warnings)
    assert "uf_gone" in joined
    assert "csg_gone" in joined


def test_composite_children_included(bundle_env, engine):
    data, _, _ = strategy_bundle.build_bundle(bundle_env, engine, "custom_comp")
    names = _zip_names(data)
    assert "strategies/custom/custom_comp.py" in names
    assert "strategies/custom/custom_standalone.py" in names
    assert "user_data/strategy_overrides/custom_standalone.json" in names


def test_unknown_strategy_rejected(bundle_env, engine):
    with pytest.raises(strategy_bundle.StrategyBundleError, match="unknown strategy"):
        strategy_bundle.build_bundle(bundle_env, engine, "custom_nope")


def test_roundtrip_import_and_conflict(bundle_env, engine, tmp_path_factory, monkeypatch):
    from app.config import settings as app_settings

    data, _, _ = strategy_bundle.build_bundle(bundle_env, engine, "custom_sigstrat")

    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setattr(app_settings, "data_dir", fresh)

    preview = strategy_bundle.inspect_bundle(data)
    assert preview["strategy_id"] == "custom_sigstrat"
    assert {e["action"] for e in preview["entries"]} == {"create"}
    assert preview["missing_deps"] == []

    result = strategy_bundle.apply_bundle(data)
    assert (fresh / "strategies/custom/custom_sigstrat.py").is_file()
    assert (fresh / "user_data/custom_signals/sig_dep.json").is_file()
    assert (fresh / "user_data/custom_factors/uf_dep2.json").is_file()
    by_key = {r["key"]: r for r in result["categories"]}
    assert by_key["strategies"]["imported"] == 1
    assert by_key["custom_signals"]["imported"] == 1
    assert by_key["custom_factors"]["imported"] == 1
    assert result["reload_hints"]["strategies"] is True

    # 已存在 → 默认跳过; allow_overwrite=True → 覆盖
    again = strategy_bundle.apply_bundle(data)
    by_key = {r["key"]: r for r in again["categories"]}
    assert by_key["strategies"]["imported"] == 0
    assert by_key["strategies"]["skipped_conflict"] == 1
    preview2 = strategy_bundle.inspect_bundle(data)
    assert {e["action"] for e in preview2["entries"]} == {"conflict"}
    third = strategy_bundle.apply_bundle(data, allow_overwrite=True)
    by_key = {r["key"]: r for r in third["categories"]}
    assert by_key["strategies"]["imported"] == 1


def test_inspect_reports_missing_deps(bundle_env, engine, tmp_path_factory, monkeypatch):
    from app.config import settings as app_settings

    data, _, _ = strategy_bundle.build_bundle(bundle_env, engine, "custom_missing")
    fresh = tmp_path_factory.mktemp("fresh2")
    monkeypatch.setattr(app_settings, "data_dir", fresh)
    preview = strategy_bundle.inspect_bundle(data)
    joined = "\n".join(preview["missing_deps"])
    assert "uf_gone" in joined
    assert "csg_gone" in joined


def _bundle_zip(entries: dict[str, bytes], manifest: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
        if manifest is None:
            manifest = {
                "format": strategy_bundle.BUNDLE_FORMAT,
                "version": strategy_bundle.BUNDLE_VERSION,
                "strategy_id": "custom_standalone",
                "label": "x",
            }
        zf.writestr("manifest.json", json.dumps(manifest))
    return buf.getvalue()


def test_out_of_scope_entries_rejected(bundle_env):
    data = _bundle_zip(
        {
            "strategies/custom/custom_standalone.py": STANDALONE.encode(),
            "user_data/watchlist.parquet": b"fake",
        }
    )
    with pytest.raises(strategy_bundle.StrategyBundleError, match="不允许的条目"):
        strategy_bundle.parse_bundle(data)


def test_bad_format_rejected(bundle_env):
    data = _bundle_zip(
        {"strategies/custom/custom_standalone.py": STANDALONE.encode()},
        manifest={"format": "tick-config-backup", "version": 1},
    )
    with pytest.raises(strategy_bundle.StrategyBundleError, match="不是本应用的策略包"):
        strategy_bundle.parse_bundle(data)
    garbage = b"not a zip"
    with pytest.raises(strategy_bundle.StrategyBundleError):
        strategy_bundle.parse_bundle(garbage)
