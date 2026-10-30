"""统一资源传输: 多根依赖闭包、选择性导入、预检与失败回滚。"""
from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import definitions as definitions_api
from app.config import settings
from app.factors import registry, store
from app.services import resource_bundle
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine


def strategy(sid: str, *, factor: str = "uf_shared") -> str:
    return f'''import polars as pl
META = {{"id": "{sid}", "name": "{sid}", "description": "资源传输测试", "tags": [],
        "asset_types": ["stock"], "timeframes": ["1d"], "scoring": {{"{factor}": 1}}}}
ENTRY_SIGNALS = ["csg_shared"]
def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


FACTOR = {"id": "uf_shared", "label": "共享因子", "kind": "custom", "formula": "close", "lang": "dsl"}
SIGNAL = {"id": "shared", "name": "共享信号", "kind": "entry", "enabled": True,
          "conditions": [{"left": "uf_shared", "op": ">", "right": 0}]}
ROOTS = [{"kind": "strategy", "id": "custom_a"}, {"kind": "strategy", "id": "custom_b"}]


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value, encoding="utf-8")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    write(tmp_path / "strategies/custom/custom_a.py", strategy("custom_a"))
    write(tmp_path / "strategies/custom/custom_b.py", strategy("custom_b"))
    write(tmp_path / "user_data/custom_factors/uf_shared.json", FACTOR)
    write(tmp_path / "user_data/custom_signals/shared.json", SIGNAL)
    write(tmp_path / "user_data/strategy_overrides/custom_a.json", {"params": {"n": 5}})
    old = registry.unregister_factor("uf_shared")
    store.load_into_registry(tmp_path)
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / s for s in ("custom", "ai", "composite")])
    yield tmp_path, engine
    registry.unregister_factor("uf_shared")
    registry.unregister_factor("cf_mix")
    if old is not None:
        registry.register_factor(old)


def export(env, roots=None):
    return resource_bundle.build_resources(env[0], env[1], roots or ROOTS)[0]


def fresh(tmp_path, monkeypatch):
    dest = tmp_path / "destination"
    dest.mkdir()
    monkeypatch.setattr(settings, "data_dir", dest)
    registry.unregister_factor("uf_shared")
    return dest


def inspect(blob, **kwargs):
    return resource_bundle.inspect_resources([("resources.zip", blob)], **kwargs)


def loose_zip(files: dict[str, bytes]) -> bytes:
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
        return buffer.getvalue()


def apply(blob, preview, *, selected=None, overwrite=None, engine=None):
    return resource_bundle.apply_resources(
        [("resources.zip", blob)], selected=selected or preview["roots"],
        overwrite=overwrite or [], revision=preview["revision"], engine=engine,
    )


def test_bulk_export_deduplicates_shared_dependencies(env):
    blob = export(env)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == "tick-resource-bundle"
        assert len(manifest["roots"]) == 2
        assert zf.namelist().count("user_data/custom_factors/uf_shared.json") == 1
        assert zf.namelist().count("user_data/custom_signals/shared.json") == 1
        assert "user_data/strategy_overrides/custom_a.json" in zf.namelist()


@pytest.mark.parametrize("backend_in_meta", [False, True])
@pytest.mark.parametrize("module", ["research", "builtin"])
def test_matrix_generated_signal_labels_are_not_external_definitions(env, tmp_path, monkeypatch, backend_in_meta, module):
    meta = {"id": "custom_rank", "name": "5日动量排名策略", "description": "排名", "tags": [],
            "asset_types": ["stock"], "timeframes": ["1d"], "scoring": {}}
    if backend_in_meta:
        meta["execution_backend"] = "matrix_native"
    code = (
        f"from app.strategy.{module}.factor_rank_research import FactorRankResearchMatrixStrategy\n"
        f"META: dict = {meta!r}\n"
        + ("" if backend_in_meta else 'EXECUTION_BACKEND: str = "matrix_native"\n')
        + 'ENTRY_SIGNALS = ["signal_factor_rank_entry"]\nEXIT_SIGNALS = ["signal_factor_rank_exit"]\n'
        + 'SCORING = {"uf_shared": 1.0}\nMATRIX_STRATEGY = FactorRankResearchMatrixStrategy(SCORING)\n'
    )
    write(env[0] / "strategies/custom/custom_rank.py", code)
    write(env[0] / "user_data/strategy_overrides/custom_rank.json", {
        "entry_signals": ["signal_factor_rank_entry"], "exit_signals": ["signal_factor_rank_exit"],
    })
    # 同名用户信号也不应被当成该矩阵策略的输入依赖打包。
    write(env[0] / "user_data/custom_signals/factor_rank_entry.json", {**SIGNAL, "id": "factor_rank_entry"})
    env[1].reload()
    blob = export(env, [{"kind": "strategy", "id": "custom_rank"}])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert "user_data/custom_factors/uf_shared.json" in archive.namelist()
        assert "user_data/custom_signals/factor_rank_entry.json" not in archive.namelist()
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert not any(entry["issues"] for entry in preview["entries"])
    apply(blob, preview)
    loaded = StrategyEngine(strategy_dirs=[dest / "strategies/custom"]).get("custom_rank")
    assert loaded.execution_backend == "matrix_native"
    assert loaded.entry_signals == ["signal_factor_rank_entry"]


def test_matrix_explicit_input_signals_still_require_definitions(env):
    code = (
        'META = {"id": "custom_rank", "name": "排名", "scoring": {}}\n'
        'EXECUTION_BACKEND = "matrix_native"\n'
        'ENTRY_SIGNALS = ["signal_factor_rank_entry"]\n'
        'REQUIRED_FEATURES = ["signal_factor_rank_entry"]\n'
    )
    preview = resource_bundle.inspect_resources([("custom_rank.py", code.encode())])
    item = next(entry for entry in preview["entries"] if entry["kind"] == "strategy")
    assert "缺少依赖: signal_factor_rank_entry" in item["issues"]


def test_non_matrix_signals_are_not_exempted_by_their_names(env):
    code = strategy("custom_a").replace('"csg_shared"', '"signal_factor_rank_entry"')
    preview = resource_bundle.inspect_resources([("custom_a.py", code.encode())])
    item = next(entry for entry in preview["entries"] if entry["kind"] == "strategy")
    assert "缺少依赖: signal_factor_rank_entry" in item["issues"]


def test_select_one_strategy_brings_only_its_dependencies(env, tmp_path, monkeypatch):
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert not any(e["issues"] for e in preview["entries"])
    result = apply(blob, preview, selected=["strategy:custom_a"])
    assert result["imported"] == 4
    assert (dest / "strategies/custom/custom_a.py").exists()
    assert not (dest / "strategies/custom/custom_b.py").exists()
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()
    assert (dest / "user_data/custom_signals/shared.json").exists()


@pytest.mark.parametrize("root", [{"kind": "signal", "id": "shared"}, {"kind": "factor", "id": "uf_shared"}])
def test_signal_and_factor_only_bundles(env, tmp_path, monkeypatch, root):
    blob = export(env, [root])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert preview["roots"] == [f"{root['kind']}:{root['id']}"]
    apply(blob, preview)
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()
    assert not (dest / "strategies").exists()


@pytest.mark.parametrize("selected, imported", [("factor:uf_shared", 1), ("signal:shared", 2)])
def test_extract_page_resource_from_strategy_zip_without_importing_strategies(env, tmp_path, monkeypatch, selected, imported):
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert all(root.startswith("strategy:") for root in preview["roots"])
    result = apply(blob, preview, selected=[selected])
    assert result["imported"] == imported
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()
    assert (dest / "user_data/custom_signals/shared.json").exists() == selected.startswith("signal:")
    assert not (dest / "strategies").exists()
    assert not (dest / "user_data/strategy_overrides").exists()


def test_identical_definitions_reuse_and_different_content_requires_explicit_replace(env, tmp_path, monkeypatch):
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    write(dest / "user_data/custom_factors/uf_shared.json", {**FACTOR, "formula": "open"})
    write(dest / "user_data/custom_signals/shared.json", SIGNAL)
    preview = inspect(blob)
    actions = {e["key"]: e["action"] for e in preview["entries"]}
    assert actions["signal:shared"] == "reuse"
    assert actions["factor:uf_shared"] == "conflict"
    with pytest.raises(resource_bundle.ResourceBundleError, match="冲突"):
        apply(blob, preview)
    assert not (dest / "strategies/custom/custom_a.py").exists()
    result = apply(blob, preview, overwrite=["factor:uf_shared"])
    assert result["reused"] == 1
    assert json.loads((dest / "user_data/custom_factors/uf_shared.json").read_text())["formula"] == "close"
    assert registry.get_factor("uf_shared").formula_text == "close"


def test_changed_local_file_invalidates_preview(env, tmp_path, monkeypatch):
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    write(dest / "user_data/custom_factors/uf_shared.json", {**FACTOR, "formula": "high"})
    with pytest.raises(resource_bundle.ResourceBundleError, match="重新预览"):
        apply(blob, preview, overwrite=["factor:uf_shared"])
    assert not (dest / "strategies/custom/custom_a.py").exists()


def test_write_failure_rolls_back_the_entire_selected_batch(env, tmp_path, monkeypatch):
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    original = {**FACTOR, "formula": "open"}
    write(dest / "user_data/custom_factors/uf_shared.json", original)
    preview = inspect(blob)
    real_write = resource_bundle.config_backup._write_payload

    def fail_signal(path, payload):
        if path.name == "shared.json":
            raise OSError("磁盘写入失败")
        real_write(path, payload)

    monkeypatch.setattr(resource_bundle.config_backup, "_write_payload", fail_signal)
    with pytest.raises(resource_bundle.ResourceBundleError, match="回滚"):
        apply(blob, preview, overwrite=["factor:uf_shared"])
    assert json.loads((dest / "user_data/custom_factors/uf_shared.json").read_text()) == original
    assert not (dest / "strategies/custom/custom_a.py").exists()
    assert not (dest / "user_data/custom_signals/shared.json").exists()


def test_missing_dependency_blocks_only_the_selected_resource(env, tmp_path, monkeypatch):
    blob = export(env, [{"kind": "factor", "id": "uf_shared"}])
    fresh(tmp_path, monkeypatch)
    uploads = [("factor.zip", blob), ("signal.json", json.dumps({**SIGNAL, "conditions": [{"left": "uf_missing", "op": ">", "right": 0}]}).encode())]
    preview = resource_bundle.inspect_resources(uploads)
    assert next(e for e in preview["entries"] if e["key"] == "signal:shared")["issues"]
    result = resource_bundle.apply_resources(uploads, selected=["factor:uf_shared"], overwrite=[], revision=preview["revision"])
    assert result["imported"] == 1


def test_composite_preserves_member_definitions_and_preview_does_not_mutate_registry(env, tmp_path, monkeypatch):
    definition = {"id": "cf_mix", "label": "组合", "kind": "composite", "members": {"uf_shared": 1, "momentum_5d": 1}}
    write(env[0] / "user_data/custom_factors/cf_mix.json", definition)
    store.register_definition(definition)
    blob = export(env, [{"kind": "factor", "id": "cf_mix"}])
    fresh(tmp_path, monkeypatch)
    registry.unregister_factor("cf_mix")
    preview = inspect(blob)
    assert registry.get_factor("uf_shared") is None
    assert registry.get_factor("cf_mix") is None
    assert not any(e["issues"] for e in preview["entries"])
    apply(blob, preview)
    assert registry.get_factor("cf_mix") is not None


def test_import_accepts_old_strategy_package(env, tmp_path, monkeypatch):
    from app.services import strategy_bundle

    blob = strategy_bundle.build_bundle(env[0], env[1], "custom_a")[0]
    fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert preview["roots"] == ["strategy:custom_a"]
    apply(blob, preview)


def test_multiple_loose_files_are_inspected_together(env, tmp_path, monkeypatch):
    fresh(tmp_path, monkeypatch)
    uploads = [("custom_a.py", strategy("custom_a").encode()),
               ("factor.json", json.dumps(FACTOR).encode()), ("signal.json", json.dumps(SIGNAL).encode())]
    preview = resource_bundle.inspect_resources(uploads)
    assert len(preview["roots"]) == 3
    assert not any(e["issues"] for e in preview["entries"])
    result = resource_bundle.apply_resources(uploads, selected=["strategy:custom_a"], overwrite=[], revision=preview["revision"])
    assert result["imported"] == 3


@pytest.mark.parametrize("folder", ["", "策略分享/", "下载/策略分享/"])
def test_plain_zip_discovers_resources_without_a_manifest(env, tmp_path, monkeypatch, folder):
    files = {f"{folder}策略{i}.py": strategy(f"custom_{i}").encode() for i in range(25)}
    files.update({f"{folder}因子.json": json.dumps(FACTOR).encode(),
                  f"{folder}信号.json": json.dumps(SIGNAL).encode(),
                  f"{folder}README.md": b"Usage notes", f"{folder}.DS_Store": b"metadata",
                  f"__MACOSX/{folder}._策略0.py": b"not Python"})
    blob = loose_zip(files)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert preview["counts"]["strategy"] == 25
    assert len(preview["roots"]) == 27
    assert not any(entry["issues"] for entry in preview["entries"])
    engine = StrategyEngine(strategy_dirs=[dest / "strategies/custom"])
    result = apply(blob, preview, selected=["strategy:custom_7"], engine=engine)
    assert result["imported"] == 3
    assert engine.get("custom_7").meta["name"] == "custom_7"
    assert not (dest / "strategies/custom/custom_8.py").exists()
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()
    assert (dest / "user_data/custom_signals/shared.json").exists()


def test_plain_zip_preserves_exported_paths_and_saved_settings(env, tmp_path, monkeypatch):
    write(env[0] / "strategies/custom/_helper.py", 'import polars as pl\ndef helper(df):\n    return df\n')
    env[1].reload()
    with zipfile.ZipFile(io.BytesIO(export(env))) as archive:
        blob = loose_zip({f"分享/{name}": archive.read(name) for name in archive.namelist() if name != "manifest.json"})
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    apply(blob, preview, selected=["strategy:custom_a"])
    assert json.loads((dest / "user_data/strategy_overrides/custom_a.json").read_text()) == {"params": {"n": 5}}
    assert (dest / "strategies/custom/_helper.py").exists()
    assert not (dest / "strategies/custom/custom_b.py").exists()


def test_plain_zip_can_supply_dependencies_for_separately_uploaded_strategy(env, tmp_path, monkeypatch):
    fresh(tmp_path, monkeypatch)
    uploads = [("策略.py", strategy("custom_a").encode()),
               ("依赖.zip", loose_zip({"因子.json": json.dumps(FACTOR).encode(), "信号.json": json.dumps(SIGNAL).encode()}))]
    preview = resource_bundle.inspect_resources(uploads)
    result = resource_bundle.apply_resources(uploads, selected=["strategy:custom_a"], overwrite=[], revision=preview["revision"])
    assert result["imported"] == 3


@pytest.mark.parametrize("name", ["../策略.py", "/策略.py", "包/../策略.py", "包\\策略.py"])
def test_plain_zip_rejects_unsafe_paths(env, name):
    with pytest.raises(resource_bundle.ResourceBundleError, match=r"路径|条目"):
        inspect(loose_zip({name: strategy("custom_a").encode()}))


def test_plain_zip_rejects_duplicate_entries(env):
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("策略.py", strategy("custom_a"))
            with pytest.warns(UserWarning):
                archive.writestr("策略.py", strategy("custom_b"))
        with pytest.raises(resource_bundle.ResourceBundleError, match="重复"):
            inspect(buffer.getvalue())


def test_plain_zip_rejects_symlink_members(env):
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            link = zipfile.ZipInfo("策略.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, strategy("custom_a"))
        with pytest.raises(resource_bundle.ResourceBundleError, match="符号链接"):
            inspect(buffer.getvalue())


@pytest.mark.parametrize("limit", ["MAX_ENTRIES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
def test_plain_zip_obeys_archive_limits(env, monkeypatch, limit):
    monkeypatch.setattr(resource_bundle.config_backup, limit, 1)
    with pytest.raises(resource_bundle.ResourceBundleError, match="上限"):
        inspect(loose_zip({"策略.py": strategy("custom_a").encode(), "因子.json": json.dumps(FACTOR).encode()}))


@pytest.mark.parametrize("files, message", [({"README.md": b"notes"}, "没有可导入"),
                                           ({"程序.exe": b"bad"}, "不支持"),
                                           ({"无关.json": b'{}'}, "无法识别")])
def test_plain_zip_reports_unusable_contents(env, files, message):
    with pytest.raises(resource_bundle.ResourceBundleError, match=message):
        inspect(loose_zip(files))


@pytest.mark.parametrize("manifest", [b"bad json", b'{"format":"other","version":1}',
                                     b'{"format":"tick-resource-bundle","version":999}'])
def test_declared_invalid_manifests_are_not_treated_as_loose_files(env, manifest):
    with pytest.raises(resource_bundle.ResourceBundleError, match=r"JSON|策略包|版本"):
        inspect(loose_zip({"manifest.json": manifest, "custom_a.py": strategy("custom_a").encode()}))


def test_empty_selection_and_unknown_overwrite_keys_rejected(env, tmp_path, monkeypatch):
    blob = export(env)
    fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    for selected, overwrite in [([], []), (["strategy:unknown"], []), (preview["roots"], ["factor:unknown"])]:
        with pytest.raises(resource_bundle.ResourceBundleError):
            resource_bundle.apply_resources([("resources.zip", blob)], selected=selected, overwrite=overwrite, revision=preview["revision"])


@pytest.mark.parametrize("without_manifest", [False, True])
def test_api_bulk_export_inspect_and_apply(env, tmp_path, monkeypatch, without_manifest):
    app = FastAPI()
    app.include_router(definitions_api.router)
    app.state.strategy_engine = env[1]
    client = TestClient(app)
    catalog = client.get("/api/definitions/resources").json()["items"]
    assert {i["id"] for i in catalog} >= {"custom_a", "custom_b", "shared", "uf_shared"}
    response = client.post("/api/definitions/resources/export", json={"resources": ROOTS})
    assert response.status_code == 200
    blob = response.content
    if without_manifest:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            blob = loose_zip({name: archive.read(name) for name in archive.namelist() if name != "manifest.json"})
    fresh(tmp_path, monkeypatch)
    # 导入时接收方引擎不应继续使用源环境的策略目录。
    app.state.strategy_engine = StrategyEngine(strategy_dirs=[settings.data_dir / "strategies/custom"])
    files = [("files", ("resources.zip", blob, "application/zip"))]
    response = client.post("/api/definitions/resources/inspect", files=files)
    assert response.status_code == 200
    preview = response.json()
    response = client.post("/api/definitions/resources/apply", files=files,
                           data={"selected": json.dumps(["strategy:custom_a"]), "overwrite": "[]", "revision": preview["revision"]})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 4
    assert app.state.strategy_engine.get("custom_a")


def test_conflict_preview_shows_transitive_local_consumers_and_content(env):
    blob = export(env)
    write(env[0] / "user_data/custom_factors/uf_shared.json", {**FACTOR, "formula": "open"})
    preview = inspect(blob)
    factor = next(e for e in preview["entries"] if e["key"] == "factor:uf_shared")
    assert factor["referenced_by"] == ["信号「共享信号」", "策略「custom_a」", "策略「custom_b」"]
    assert '"open"' in factor["comparison"]["local"]
    assert '"close"' in factor["comparison"]["incoming"]


def test_annotated_metadata_preserves_dependency_graph(env, tmp_path, monkeypatch):
    write(env[0] / "strategies/custom/custom_a.py", strategy("custom_a").replace("META =", "META: dict ="))
    env[1].reload()
    blob = export(env)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    apply(blob, preview, selected=["strategy:custom_a"])
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()


def test_helper_source_factor_references_travel_with_strategy(env, tmp_path, monkeypatch):
    helper = 'import polars as pl\ndef helper(df):\n    return df.filter(pl.col("uf_shared") > 0)\n'
    write(env[0] / "strategies/custom/_helper.py", helper)
    code = strategy("custom_a").replace('"scoring": {"uf_shared": 1}', '"scoring": {}').replace('ENTRY_SIGNALS = ["csg_shared"]', 'ENTRY_SIGNALS = []')
    write(env[0] / "strategies/custom/custom_a.py", code)
    env[1].reload()
    blob = export(env, [ROOTS[0]])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    apply(blob, preview)
    assert (dest / "strategies/custom/_helper.py").exists()
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()


def test_disabled_signal_can_be_imported_without_enabling_it(env, tmp_path, monkeypatch):
    fresh(tmp_path, monkeypatch)
    uploads = [("factor.json", json.dumps(FACTOR).encode()), ("signal.json", json.dumps({**SIGNAL, "enabled": False}).encode())]
    preview = resource_bundle.inspect_resources(uploads)
    resource_bundle.apply_resources(uploads, selected=["signal:shared"], overwrite=[], revision=preview["revision"])
    assert json.loads((settings.data_dir / "user_data/custom_signals/shared.json").read_text())["enabled"] is False


@pytest.mark.parametrize("name", ["../escape.json", "/tmp/escape.json", "user_data/custom_factors/../escape.json", "user_data/preferences.json"])
def test_out_of_scope_paths_rejected_before_writing(env, name):
    blob = resource_bundle._zip({name: json.dumps(FACTOR).encode()}, ["factor:uf_shared"])
    with pytest.raises(resource_bundle.ResourceBundleError):
        inspect(blob)


def test_symlink_target_is_rejected(env, tmp_path, monkeypatch):
    blob = export(env, [{"kind": "factor", "id": "uf_shared"}])
    dest = fresh(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "user_data").mkdir()
    try:
        (dest / "user_data/custom_factors").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(resource_bundle.ResourceBundleError, match="符号链接"):
        inspect(blob)
    assert not list(outside.iterdir())


def test_duplicate_entries_are_rejected(env):
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format": "tick-resource-bundle", "version": 1, "roots": ["factor:uf_shared"]}))
            with pytest.warns(UserWarning):
                for _ in range(2):
                    archive.writestr("user_data/custom_factors/uf_shared.json", json.dumps(FACTOR))
        with pytest.raises(resource_bundle.ResourceBundleError, match="重复"):
            inspect(buffer.getvalue())


def test_load_failure_restores_files_and_the_previous_runtime(env, tmp_path, monkeypatch):
    blob = export(env, [ROOTS[0]])
    dest = fresh(tmp_path, monkeypatch)
    engine = StrategyEngine(strategy_dirs=[dest / "strategies/custom"])
    real_reload = engine.reload
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("加载失败")
        real_reload()

    monkeypatch.setattr(engine, "reload", fail_once)
    preview = inspect(blob)
    with pytest.raises(resource_bundle.ResourceBundleError, match="回滚"):
        apply(blob, preview, engine=engine)
    assert not (dest / "strategies/custom/custom_a.py").exists()
    assert registry.get_factor("uf_shared") is None
    assert not engine.list_strategies()


def test_registry_preview_is_isolated_from_other_threads(env):
    from concurrent.futures import ThreadPoolExecutor

    original = registry.get_factor("uf_shared")
    with registry.isolated_registry():
        registry.unregister_factor("uf_shared")
        assert registry.get_factor("uf_shared") is None
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(registry.get_factor, "uf_shared").result() is original
    assert registry.get_factor("uf_shared") is original


def test_unselected_factor_is_neither_written_nor_registered(env, tmp_path, monkeypatch):
    fresh(tmp_path, monkeypatch)
    other = {**FACTOR, "id": "uf_unselected", "label": "未选择"}
    uploads = [("factor.json", json.dumps(FACTOR).encode()), ("other.json", json.dumps(other).encode())]
    preview = resource_bundle.inspect_resources(uploads)
    resource_bundle.apply_resources(uploads, selected=["factor:uf_shared"], overwrite=[], revision=preview["revision"])
    assert registry.get_factor("uf_unselected") is None
    assert not (settings.data_dir / "user_data/custom_factors/uf_unselected.json").exists()


def test_export_preview_revision_is_stable_and_refuses_changed_content(env):
    app = FastAPI()
    app.include_router(definitions_api.router)
    app.state.strategy_engine = env[1]
    client = TestClient(app)
    body = {"resources": ROOTS}
    first = client.post("/api/definitions/resources/export/preview", json=body).json()
    second = client.post("/api/definitions/resources/export/preview", json=body).json()
    assert first["revision"] == second["revision"]
    write(env[0] / "user_data/custom_factors/uf_shared.json", {**FACTOR, "formula": "high"})
    response = client.post("/api/definitions/resources/export", json={**body, "revision": first["revision"]})
    assert response.status_code == 409
    assert "重新预览" in response.json()["detail"]


def test_cache_refresh_failure_rolls_back_the_selected_batch(env, tmp_path, monkeypatch):
    blob = export(env, [ROOTS[0]])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)

    def fail_refresh():
        raise OSError("无法清理旧缓存")

    with pytest.raises(resource_bundle.ResourceBundleError, match="回滚"):
        resource_bundle.apply_resources([("resources.zip", blob)], selected=preview["roots"], overwrite=[], revision=preview["revision"], refresh=fail_refresh)
    assert not (dest / "strategies/custom/custom_a.py").exists()
    assert registry.get_factor("uf_shared") is None


def test_runtime_declared_features_are_preserved_in_package_dependency_metadata(env, tmp_path, monkeypatch):
    code = strategy("custom_a").replace('"scoring": {"uf_shared": 1}', '"scoring": {}').replace('ENTRY_SIGNALS = ["csg_shared"]', 'ENTRY_SIGNALS = []')
    write(env[0] / "strategies/custom/custom_a.py", code + '\nREQUIRED_FEATURES = {"uf_" + "shared"}\n')
    env[1].reload()
    blob = export(env, [ROOTS[0]])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    apply(blob, preview)
    assert (dest / "user_data/custom_factors/uf_shared.json").exists()


def test_user_ai_drafts_can_be_exported_and_remain_drafts_after_import(env, tmp_path, monkeypatch):
    code = strategy("ai_draft").replace('"tags": []', '"tags": [], "research_only": True')
    write(env[0] / "strategies/ai/ai_draft.py", code)
    env[1].reload()
    catalog = resource_bundle.list_resources(env[0], env[1])
    assert next(item for item in catalog if item["id"] == "ai_draft")["draft"] is True
    blob = export(env, [{"kind": "strategy", "id": "ai_draft"}])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    result = apply(blob, preview)
    assert result["drafts"] == ["ai_draft"]
    assert AIStrategyGenerator._extract_meta((dest / "strategies/ai/ai_draft.py").read_text())["research_only"] is True


def test_loose_ai_file_without_publish_state_defaults_to_draft(env, tmp_path, monkeypatch):
    dest = fresh(tmp_path, monkeypatch)
    uploads = [("ai_loose.py", strategy("ai_loose").encode()), ("factor.json", json.dumps(FACTOR).encode()), ("signal.json", json.dumps(SIGNAL).encode())]
    preview = resource_bundle.inspect_resources(uploads)
    resource_bundle.apply_resources(uploads, selected=["strategy:ai_loose"], overwrite=[], revision=preview["revision"])
    assert AIStrategyGenerator._extract_meta((dest / "strategies/ai/ai_loose.py").read_text())["research_only"] is True


def test_inspect_never_executes_source_and_runtime_failure_rolls_back(env, tmp_path, monkeypatch):
    dest = fresh(tmp_path, monkeypatch)
    code = strategy("custom_broken", factor="close").replace('ENTRY_SIGNALS = ["csg_shared"]', 'ENTRY_SIGNALS = []') + '\nsentinel = 1 / 0\n'
    uploads = [("broken.py", code.encode())]
    preview = resource_bundle.inspect_resources(uploads)
    assert not any(entry["issues"] for entry in preview["entries"])
    engine = StrategyEngine(strategy_dirs=[dest / "strategies/custom"])
    with pytest.raises(resource_bundle.ResourceBundleError, match="回滚"):
        resource_bundle.apply_resources(uploads, selected=preview["roots"], overwrite=[], revision=preview["revision"], engine=engine)
    assert not (dest / "strategies/custom/custom_broken.py").exists()


def test_unsafe_strategy_is_rejected_without_writing(env, tmp_path, monkeypatch):
    dest = fresh(tmp_path, monkeypatch)
    code = strategy("custom_unsafe", factor="close").replace('ENTRY_SIGNALS = ["csg_shared"]', 'ENTRY_SIGNALS = []') + '\nimport os\n'
    uploads = [("unsafe.py", code.encode())]
    preview = resource_bundle.inspect_resources(uploads)
    assert preview["entries"][0]["issues"]
    with pytest.raises(resource_bundle.ResourceBundleError, match="校验失败"):
        resource_bundle.apply_resources(uploads, selected=preview["roots"], overwrite=[], revision=preview["revision"])
    assert not (dest / "strategies/custom/custom_unsafe.py").exists()


def test_historical_strategy_filename_can_differ_from_its_stable_id(env, tmp_path, monkeypatch):
    original = env[0] / "strategies/custom/custom_a.py"
    original.rename(original.with_name("older_filename.py"))
    env[1].reload()
    blob = export(env, [ROOTS[0]])
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    engine = StrategyEngine(strategy_dirs=[dest / "strategies/custom"])
    apply(blob, preview, engine=engine)
    assert engine.get("custom_a").file_path.name == "older_filename.py"


def test_changed_manifest_invalidates_a_previously_inspected_upload(env, tmp_path, monkeypatch):
    blob = export(env)
    fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payloads = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    manifest["inputs"] = {"strategy:custom_a": ["uf_missing"]}
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, payload in payloads.items():
                archive.writestr(name, payload)
            archive.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(resource_bundle.ResourceBundleError, match="重新预览"):
            apply(buffer.getvalue(), preview)


@pytest.mark.parametrize(("filename", "payload"), [("bad.py", b"not python ???"), ("bad.zip", b"not a zip"), ("bad.json", b"[]")])
def test_apply_without_inspect_still_returns_400_for_malformed_files(env, filename, payload):
    app = FastAPI()
    app.include_router(definitions_api.router)
    client = TestClient(app)
    response = client.post("/api/definitions/resources/apply", files=[("files", (filename, payload))],
                           data={"selected": '["strategy:custom_a"]', "overwrite": "[]", "revision": "invalid"})
    assert response.status_code == 400


@pytest.mark.parametrize("strategy_root", [False, True])
def test_intraday_signals_use_the_minute_feature_contract(env, tmp_path, monkeypatch, strategy_root):
    signal = {"id": "minute_cross", "name": "盘中均价上穿", "kind": "entry", "timeframe": "intraday", "enabled": True,
              "conditions": [{"left": "price", "op": "cross_up", "right": "field:vwap"}]}
    write(env[0] / "user_data/custom_signals/minute_cross.json", signal)
    if strategy_root:
        code = strategy("custom_a", factor="close").replace('["1d"]', '["1m"]').replace('ENTRY_SIGNALS = ["csg_shared"]', 'ENTRY_SIGNALS = ["csgi_minute_cross"]').replace('def filter(', 'def filter_minute_history(').replace('-> pl.Expr:', '-> pl.DataFrame:').replace('return pl.lit(True)', 'return df')
        write(env[0] / "strategies/custom/custom_a.py", code + '\nEXECUTION_BACKEND = "minute_filter"\n')
        env[1].reload()
    roots = [ROOTS[0]] if strategy_root else [{"kind": "signal", "id": "minute_cross"}]
    blob = export(env, roots)
    dest = fresh(tmp_path, monkeypatch)
    preview = inspect(blob)
    assert not any(entry["issues"] for entry in preview["entries"])
    apply(blob, preview)
    assert (dest / "user_data/custom_signals/minute_cross.json").exists()
