from __future__ import annotations

import io
import threading
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api.settings import PluginKeyIn, save_plugin_key
from app.custom.source_packages.routes import router
from app.data_providers.base import ProviderRequestError
from app.data_providers.custom import loader, packages
from app.data_providers.custom.managed import ManagedProvider
from app.services import preferences

CODE = '''from types import SimpleNamespace
from .client import price
name = "demo_source"
def probe_api_key(key):
    return key == "valid-key", "ok" if key == "valid-key" else "invalid"
class Provider:
    name = "demo_source"
    config = SimpleNamespace(datasets={"realtime": {}})
    def get_realtime(self):
        return [{"symbol": "600000.SH", "last": price, "change_pct": 0.02}]
    def close(self):
        pass
'''


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(packages.settings, "data_dir", tmp_path)
    monkeypatch.setattr(loader, "plugins_dir", lambda: tmp_path / "builtins")
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(packages, "_started", set())
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    monkeypatch.setattr(preferences, "load", lambda: {})
    return tmp_path


def bundle(version="1.0.0", *, code=CODE, extra=None, **changes):
    doc = dict(name="demo_source", display_name="演示数据源", version=version,
               plugin_api_version=1, entry="provider:Provider", datasets=["realtime"], runtime="none")
    doc.update(changes)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("plugin.yaml", yaml.safe_dump(doc))
        archive.writestr("provider.py", code)
        archive.writestr("client.py", f"price = {int(version.split('.')[0])}\n")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return buf.getvalue()


def install(data):
    preview, _ = packages.inspect_archive(data)
    return packages.install(data, preview["digest"])


def cold_restart():
    loader._PROVIDERS = {}
    loader._PLUGIN_STATUS = {}
    packages._started.clear()
    packages.discover()


def test_inspection_does_not_execute_code(isolated):
    data = bundle(code="raise RuntimeError('must not execute during preview')")
    preview, _ = packages.inspect_archive(data)
    assert preview["manifest"]["name"] == "demo_source"
    assert not loader.names()
    with pytest.raises(ValueError, match="加载失败"):
        packages.install(data, preview["digest"])
    assert packages.read_state("demo_source") is None


def test_install_routes_and_reload_keeps_instance(isolated):
    install(bundle())
    provider = loader.get_provider("demo_source")
    assert provider.get_realtime()[0]["last"] == 1
    assert loader.provider_has_dataset("demo_source", "realtime")
    assert not loader.is_builtin("demo_source")
    assert loader.list_sources() == []
    loader.load_all()
    assert loader.get_provider("demo_source") is provider
    assert loader.list_plugins()[0]["origin"] == "external"


def test_key_first_probe_then_save_and_only_refresh_target(isolated):
    other = SimpleNamespace(close=lambda: pytest.fail("unrelated provider closed"))
    loader._PROVIDERS["other"] = other
    install(bundle(api_key_env="DEMO_KEY"))
    handle = loader.get_provider("demo_source")
    with pytest.raises(ProviderRequestError):
        handle.get_realtime()
    assert not save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="bad"))["ok"]
    assert not secrets_store.load()
    result = save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="valid-key"))
    assert result["ok"] and result["plugin_available"]
    assert handle.get_realtime()[0]["last"] == 1
    assert loader.get_provider("other") is other
    assert secrets_store.load()["demo_source_api_key"] == "valid-key"


def test_upgrade_restart_rollback_and_failed_upgrade(isolated):
    install(bundle())
    install(bundle("2.0.0"))
    loader.load_all()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    assert loader.list_plugins()[0]["pending_version"] == "2.0.0"
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 2
    packages.manage("demo_source", "rollback")
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    install(bundle("3.0.0", code="raise RuntimeError('broken')"))
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    assert "失败" in loader.list_plugins()[0]["update_error"]


def test_same_version_collision_and_preview_digest(isolated):
    data = bundle()
    install(data)
    assert "已安装" in install(data)["message"]
    with pytest.raises(ValueError, match="相同版本"):
        install(bundle(extra={"readme.md": "different"}))
    with pytest.raises(ValueError, match="预览不一致"):
        packages.install(bundle("2.0.0"), "0" * 64)


def test_pending_replacement_preview_rejects_stale_install_state(isolated):
    install(bundle())
    next_data = bundle("3.0.0")
    preview, _ = packages.inspect_archive(next_data)
    install(bundle("2.0.0"))
    with pytest.raises(ValueError, match="状态已变化"):
        packages.install(next_data, preview["digest"], preview["revision"])
    current, _ = packages.inspect_archive(next_data)
    assert current["installed_version"] == "1.0.0"
    assert current["pending_version"] == "2.0.0"


@pytest.mark.parametrize("filename", ["../escape.py", "/escape.py", "a\\b.py", "a/../b.py", "CON.py", "client.PY", "x.dll", "a./b.py"])
def test_archive_paths_and_payload_limits(isolated, filename):
    with pytest.raises(ValueError):
        packages.inspect_archive(bundle(extra={filename: "pass"}))
    assert not (isolated.parent / "escape.py").exists()


def test_manifest_contract_and_dependency_compatibility(isolated):
    for changes in ({"plugin_api_version": 9}, {"entry": "app.other:Provider"}, {"datasets": ["invented"]}):
        with pytest.raises(ValueError):
            packages.inspect_archive(bundle(**changes))
    preview, _ = packages.inspect_archive(bundle(requires_python=">=99"))
    assert preview["errors"]
    with pytest.raises(ValueError, match="requires_python"):
        packages.install(bundle(requires_python=">=99"), preview["digest"])


def test_name_conflict_and_yaml_mutation_rejected(isolated):
    loader.plugins_dir().mkdir()
    (loader.plugins_dir() / "demo_source").mkdir()
    with pytest.raises(ValueError, match="冲突"):
        install(bundle())
    (loader.plugins_dir() / "demo_source").rmdir()
    install(bundle())
    with pytest.raises(ValueError):
        loader.save_config("demo_source", {})
    with pytest.raises(ValueError):
        loader.delete_config("demo_source")


def test_disable_uninstall_blocks_all_routing_references(isolated, monkeypatch):
    install(bundle())
    monkeypatch.setattr(preferences, "load", lambda: {"depth5_data_provider": "demo_source"})
    for action in ("disable", "remove"):
        with pytest.raises(ValueError, match="先切换"):
            packages.manage("demo_source", action)
    monkeypatch.setattr(preferences, "load", lambda: {})
    secrets_store.save({"demo_source_api_key": "keep-me"})
    packages.manage("demo_source", "disable")
    assert not loader.list_plugins()[0]["available"]
    with pytest.raises(ProviderRequestError):
        loader.get_provider("demo_source").get_realtime()
    packages.manage("demo_source", "enable")
    assert loader.list_plugins()[0]["available"]
    packages.manage("demo_source", "remove")
    cold_restart()
    assert not loader.names()
    assert not packages.directory("demo_source").exists()
    assert secrets_store.load()["demo_source_api_key"] == "keep-me"


def test_handle_delays_close_until_stream_consumed():
    closed = []
    class Source:
        def iter_daily(self):
            yield 1
            yield 2
        def close(self):
            closed.append(self)
    old, new = Source(), Source()
    handle = ManagedProvider(old)
    stream = handle.iter_daily()
    handle.replace(new)
    assert not closed
    assert list(stream) == [1, 2]
    assert closed == [old]
    handle.close()
    assert closed == [old, new]


def test_handle_delays_close_during_concurrent_request():
    started, finish = threading.Event(), threading.Event()
    closed = []
    class Source:
        def get_realtime(self):
            started.set()
            assert finish.wait(3)
            return [1]
        def close(self):
            closed.append(self)
    old, new = Source(), Source()
    handle = ManagedProvider(old)
    worker = threading.Thread(target=handle.get_realtime)
    worker.start()
    assert started.wait(3)
    handle.replace(new)
    assert not closed
    finish.set()
    worker.join(3)
    assert closed == [old]


def test_management_auth_and_upload_api(isolated, monkeypatch):
    from app.services import auth
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/custom/source-packages/inspect", files={"file": ("source.zip", bundle())}, headers={"Origin": "https://evil.example"}).status_code == 403
        preview = client.post("/api/custom/source-packages/inspect", files={"file": ("source.zip", bundle())})
        assert preview.status_code == 200
        result = client.post("/api/custom/source-packages/install", files={"file": ("source.zip", bundle())}, data={"digest": preview.json()["digest"]})
        assert result.status_code == 200
        assert client.post("/api/custom/source-packages/demo_source/manage", json={"action": "disable"}).status_code == 200
    with TestClient(app, base_url="http://localhost", client=("192.168.1.3", 50000)) as client:
        assert client.post("/api/custom/source-packages/inspect", files={"file": ("s.zip", bundle())}).status_code == 403


def test_valid_key_with_failed_initialization_restores_original(isolated):
    code = CODE.replace('    def get_realtime(self):', '''    def __init__(self):
        from app import secrets_store
        if secrets_store.load().get("demo_source_api_key") == "valid-key":
            raise RuntimeError("constructor failed")
    def get_realtime(self):''')
    install(bundle(code=code, api_key_env="DEMO_KEY"))
    secrets_store.save({"demo_source_api_key": "old-key"})
    loader.refresh_plugin("demo_source")
    original = loader.get_provider("demo_source")
    assert original.get_realtime()
    result = save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="valid-key"))
    assert not result["ok"]
    assert secrets_store.load()["demo_source_api_key"] == "old-key"
    assert original.get_realtime()
    assert loader.list_plugins()[0]["available"]


def test_disabled_plugin_does_not_execute_pending_version_on_start(isolated):
    install(bundle())
    packages.manage("demo_source", "disable")
    install(bundle("2.0.0", code="raise RuntimeError('must not run')"))
    cold_restart()
    assert loader.list_plugins()[0]["pending_version"] == "2.0.0"
    assert loader.list_plugins()[0]["version"] == "1.0.0"
    assert not loader.list_plugins()[0]["available"]


def test_bad_package_does_not_hide_healthy_provider(isolated):
    install(bundle())
    path = packages.root() / "broken"
    path.mkdir()
    (path / "state.json").write_text('{"schema": 999}')
    packages.discover()
    assert loader.get_provider("demo_source").get_realtime()
    assert not next(p for p in loader.list_plugins() if p["name"] == "broken")["available"]
    assert loader.provider_has_dataset("broken", "daily")
    with pytest.raises(ProviderRequestError):
        loader.get_provider("broken").get_daily([])


def test_zip_symlink_is_rejected(isolated):
    data = io.BytesIO(bundle())
    with zipfile.ZipFile(data, "a") as archive:
        info = zipfile.ZipInfo("link.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ValueError, match="符号链接"):
        packages.inspect_archive(data.getvalue())


def test_external_capability_matrix_marks_unconfigured_route_unusable(isolated):
    from app.data_providers.capabilities import build_capability_matrix
    install(bundle(api_key_env="DEMO_KEY"))
    matrix = build_capability_matrix({"realtime_data_provider": "demo_source"})
    realtime = next(row for row in matrix["capabilities"] if row["id"] == "realtime")
    assert realtime["current"] == "demo_source"
    assert not realtime["usable"]
    assert realtime["pending"][0]["name"] == "demo_source"


def test_reload_picks_up_manually_changed_credentials(isolated):
    install(bundle(api_key_env="DEMO_KEY"))
    handle = loader.get_provider("demo_source")
    assert not loader.list_plugins()[0]["available"]
    secrets_store.save({"demo_source_api_key": "valid-key"})
    loader.load_all()
    assert loader.get_provider("demo_source") is handle
    assert loader.list_plugins()[0]["available"]
    secrets_store.clear("demo_source_api_key")
    loader.load_all()
    assert not loader.list_plugins()[0]["available"]
    with pytest.raises(ProviderRequestError):
        handle.get_realtime()
