from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.extensions.contracts import ExtensionContext
from app.extensions.packages import ExtensionPackageStore, start_managed_extensions
from app.extensions.registry import BackendExtensionRegistry


def bundle(version="1.0.0", *, broken=False, extra=None, **changes):
    doc = {
        "format": "tick-extension-package", "format_version": 1,
        "id": "demo.research", "display_name": "研究服务", "version": version,
        "extension_api_version": 1, "backend": "backend.extension",
        "frontend": "frontend/extension.mjs",
    }
    doc.update(changes)
    code = f'''
from fastapi import APIRouter
EXTENSION_ID = "demo.research"
EXTENSION_API_VERSION = 1
def setup(registrar):
    router = APIRouter(prefix="/api/extensions/demo.research")
    @router.get("/status")
    def status():
        return {{"version": "{version}"}}
    registrar.include_router(router)
def startup(context):
    {"raise RuntimeError('private-credential-must-not-leak')" if broken else "pass"}
'''
    files = {
        "extension.yaml": yaml.safe_dump(doc),
        "backend/extension.py": code,
        "frontend/extension.mjs": f"export default {{id:'demo.research',apiVersion:1,version:'{version}'}}",
    }
    files.update(extra or {})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return data.getvalue()


def install(store, data):
    preview = store.inspect(data)
    return store.install(data, preview["digest"], preview["revision"])


def boot(tmp_path):
    app = FastAPI()
    registry = BackendExtensionRegistry()
    registry.freeze()
    app.state.extension_registry = registry
    start_managed_extensions(app, ExtensionContext(1, tmp_path, SimpleNamespace()))
    return app, TestClient(app)


def test_inspection_and_install_never_execute_code(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    marker = tmp_path / "executed"
    data = bundle(extra={"backend/evil.py": f"open({str(marker)!r}, 'w').write('bad')"})
    preview = store.inspect(data)
    assert not preview["errors"] and preview["manifest"]["frontend"]
    assert not store.root.exists()
    store.install(data, preview["digest"], preview["revision"])
    assert not marker.exists()
    assert store.list_installed({})[0]["status"] == "待重启生效"
    app, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").json() == {"version": "1.0.0"}
    assert store.runtime_pages(app.state.managed_extension_versions)[0].enabled


def test_update_is_staged_and_failed_start_preserves_front_and_back(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    first, client = boot(tmp_path)
    install(store, bundle("2.0.0", broken=True))
    assert client.get("/api/extensions/demo.research/status").json()["version"] == "1.0.0"
    before = store.runtime_bundle("package:demo.research", first.state.managed_extension_versions)
    assert b"1.0.0" in before.read_bytes()
    failed, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").json()["version"] == "1.0.0"
    assert failed.state.managed_extension_versions == {"demo.research": "1.0.0"}
    item = store.list_installed(failed.state.managed_extension_versions)[0]
    assert "失败" in item["status"] and "保留" in item["error"]
    assert item["failed_version"] == "2.0.0"
    assert "private-credential" not in str(item)
    restarted, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").json()["version"] == "1.0.0"
    assert store.list_installed(restarted.state.managed_extension_versions)[0]["error"] == item["error"]
    install(store, bundle("3.0.0"))
    current, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").json()["version"] == "3.0.0"
    assert b"3.0.0" in store.runtime_bundle("package:demo.research", current.state.managed_extension_versions).read_bytes()


def test_disable_rollback_remove_are_applied_only_at_restart(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    boot(tmp_path)
    install(store, bundle("2.0.0"))
    app, client = boot(tmp_path)
    store.manage("demo.research", "disable")
    assert client.get("/api/extensions/demo.research/status").status_code == 200
    app, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").status_code == 404
    assert store.runtime_pages(app.state.managed_extension_versions) == []
    store.manage("demo.research", "rollback")
    boot(tmp_path)
    assert store.list_installed({})[0]["enabled"] is False
    store.manage("demo.research", "enable")
    app, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").json()["version"] == "1.0.0"
    store.manage("demo.research", "remove")
    assert client.get("/api/extensions/demo.research/status").status_code == 200
    app, client = boot(tmp_path)
    assert store.list_installed({}) == []
    assert client.get("/api/extensions/demo.research/status").status_code == 404


def test_digest_revision_version_and_compatibility_guards(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    data = bundle()
    p = store.inspect(data)
    with pytest.raises(ValueError, match="预览"):
        store.install(data, "0" * 64, p["revision"])
    install(store, data)
    with pytest.raises(ValueError, match="首次安装"):
        store.manage("demo.research", "cancel-update")
    store.manage("demo.research", "disable", store._revision(store._state("demo.research")))
    with pytest.raises(ValueError, match="变化"):
        store.manage("demo.research", "enable", p["revision"])
    with pytest.raises(ValueError, match="变化"):
        store.install(bundle("2.0.0"), store.inspect(bundle("2.0.0"))["digest"], p["revision"])
    with pytest.raises(ValueError, match="相同版本"):
        store.inspect(bundle(extra={"readme.md": "changed"}))
    future = bundle("2.0.0", requires_python=">=99")
    preview = store.inspect(future)
    assert preview["errors"]
    with pytest.raises(ValueError, match="requires_python"):
        store.install(future, preview["digest"], preview["revision"])


@pytest.mark.parametrize("name", ["../escape.py", "/tmp/evil.py", "a\\evil.py", "a/../x.py", "CON.py", "x.dll", "backend/extension.py/child.py"])
def test_invalid_archive_paths_and_payloads_are_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        ExtensionPackageStore(tmp_path).inspect(bundle(extra={name: "pass"}))


def test_symlink_and_corrupt_state_cannot_escape_or_block_other_packages(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    data = io.BytesIO(bundle())
    with zipfile.ZipFile(data, "a") as archive:
        info = zipfile.ZipInfo("link.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../outside.py")
    with pytest.raises(ValueError):
        store.inspect(data.getvalue())
    install(store, bundle())
    (store.root / "broken.extension").mkdir()
    (store.root / "broken.extension" / "state.json").write_text("{")
    app, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").status_code == 200
    assert len(store.list_installed(app.state.managed_extension_versions)) == 2
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "evil.extension").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.manage("evil.extension", "remove")
    assert outside.exists()


def test_route_namespace_conflict_and_startup_failures_never_partially_register(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle(extra={
        "backend/extension.py": '''
from fastapi import APIRouter
EXTENSION_ID = "demo.research"
EXTENSION_API_VERSION = 1
def setup(registrar):
    router = APIRouter()
    @router.get("/api/settings")
    def unsafe():
        return {"hijacked": True}
    registrar.include_router(router)
'''}))
    app, client = boot(tmp_path)
    assert client.get("/api/settings").status_code == 404
    assert not app.state.extension_registry.extension_ids()
    assert not app.state.managed_extension_versions


def test_preview_identifies_the_pending_version_that_will_be_replaced(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    boot(tmp_path)
    install(store, bundle("2.0.0"))
    preview = store.inspect(bundle("3.0.0"))
    assert preview["installed_version"] == "1.0.0"
    assert preview["pending_version"] == "2.0.0"
    assert preview["manifest"]["version"] == "3.0.0"


def test_modified_package_and_failed_state_write_do_not_publish_a_new_version(tmp_path, monkeypatch):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    boot(tmp_path)
    before = store._state("demo.research")
    monkeypatch.setattr(store, "_write", lambda *_: (_ for _ in ()).throw(PermissionError("无写入权限")))
    with pytest.raises(PermissionError):
        install(store, bundle("2.0.0"))
    assert store._state("demo.research") == before
    source = store._version_dir("demo.research", "1.0.0") / "backend/extension.py"
    source.write_text("raise RuntimeError('changed after install')")
    app, client = boot(tmp_path)
    assert client.get("/api/extensions/demo.research/status").status_code == 404
    assert not app.state.managed_extension_versions


def test_startup_cannot_overwrite_a_concurrent_management_change(tmp_path, monkeypatch):
    from app.extensions import packages

    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    boot(tmp_path)
    install(store, bundle("2.0.0"))
    original = packages._prepare

    def prepare(*args):
        result = original(*args)
        install(store, bundle("3.0.0"))
        return result

    monkeypatch.setattr(packages, "_prepare", prepare)
    app, client = boot(tmp_path)
    assert store._state("demo.research")["pending"] == "3.0.0"
    assert store._state("demo.research")["active"] == "1.0.0"
    assert client.get("/api/extensions/demo.research/status").status_code == 404
    assert not app.state.managed_extension_versions


def test_runtime_frontend_uses_existing_page_distribution_and_management_auth(tmp_path, monkeypatch):
    from app.api.ext_pages import router as pages_router
    from app.api.import_center import router
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: False)
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    app, _ = boot(tmp_path)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    app.include_router(pages_router)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))
    pages = client.get("/api/ext-pages").json()
    assert pages[0]["managed_by"] == "demo.research"
    assert client.get("/api/ext-pages/package:demo.research/bundle.js").status_code == 200
    assert client.put("/api/ext-pages/package:demo.research/enabled", json={"enabled": False}).status_code == 400
    assert client.post("/api/extension-packages/demo.research/manage", json={"action": "disable"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/extension-packages/demo.research/manage", json={"action": "disable"}, headers={"Authorization": "Bearer token"}).status_code == 403
    data = bundle("2.0.0")
    preview = store.inspect(data)
    result = client.post("/api/extension-packages/install", files={"file": ("bundle.zip", data)},
                         data={"digest": preview["digest"], "revision": preview["revision"], "trusted": "false"})
    assert result.status_code == 403


def test_full_app_registers_managed_packages_before_quote_service_starts(tmp_path):
    store = ExtensionPackageStore(tmp_path)
    install(store, bundle())
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>frontend shell</html>")
    script = """
from fastapi.testclient import TestClient
from app.main import app, QuoteService
original = QuoteService.boot_check
def check_ready(self):
    assert app.state.extension_registry.frozen
    assert "demo.research" in app.state.extension_registry.extension_ids()
    assert app.state.managed_extension_versions == {"demo.research": "1.0.0"}
    original(self)
QuoteService.boot_check = check_ready
with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
    assert "frontend shell" in client.get("/").text
    assert client.get("/api/extensions/demo.research/status").json() == {"version": "1.0.0"}
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path), "STATIC_DIR": str(static_dir), "TICKFLOW_API_KEY": "", "AI_API_KEY": "",
           "AUTH_PASSWORD": "", "WATCHDOG_ENABLED": "false", "BACKTEST_MATRIX_CACHE_PREWARM": "false"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]
