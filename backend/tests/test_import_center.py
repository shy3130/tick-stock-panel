from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import import_center
from app.config import settings
from app.data_providers.custom import loader
from app.services import auth, config_backup


def archive(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as package:
        for name, data in files.items():
            package.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    monkeypatch.setattr(loader, "plugins_dir", lambda: tmp_path / "builtins")
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(import_center.router)
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))


def inspect(client, name, data):
    return client.post("/api/import-center/inspect", files={"files": (name, data)})


def test_detects_by_package_contents_not_filename_without_writing(client, tmp_path):
    backup, _ = config_backup.build_backup(categories=["watchlist"])
    result = inspect(client, "not-named-backup.zip", backup)
    assert result.status_code == 200 and result.json()["kind"] == "backup"
    source = archive({
        "plugin.yaml": yaml.safe_dump({"name": "demo_source", "version": "1.0.0", "plugin_api_version": 1, "entry": "provider:Provider", "datasets": ["realtime"]}),
        "provider.py": "raise RuntimeError('must not execute')",
    })
    assert inspect(client, "backup.zip", source).json()["kind"] == "source"
    extension = archive({
        "extension.yaml": yaml.safe_dump({"format": "tick-extension-package", "format_version": 1, "id": "demo.extension", "version": "1.0.0", "extension_api_version": 1, "backend": "extension"}),
        "extension.py": "raise RuntimeError('must not execute')",
    })
    assert inspect(client, "whatever.zip", extension).json()["kind"] == "extension"
    assert not (tmp_path / "extensions").exists()
    assert not (tmp_path / "plugins").exists()


def test_resource_collection_and_page_previews(client):
    factor = json.dumps({"id": "uf_import_center", "label": "测试因子", "kind": "custom", "formula": "close", "lang": "dsl"})
    result = inspect(client, "factor.json", factor)
    assert result.status_code == 200, result.text
    assert result.json()["kind"] == "resources"
    assert result.json()["preview"]["roots"] == ["factor:uf_import_center"]
    result = inspect(client, "research-page.mjs", b"throw new Error('not executed')")
    assert result.json()["kind"] == "page"
    assert result.json()["preview"]["suggested_id"] == "research_page"
    assert len(result.json()["preview"]["digest"]) == 64


def test_bad_archives_ambiguous_formats_and_mixed_installations_fail_closed(client):
    assert inspect(client, "x.zip", b"not zip").status_code == 400
    assert inspect(client, "x.exe", b"binary").status_code == 400
    assert inspect(client, "x.zip", archive({"extension.yaml": "{}", "plugin.yaml": "{}"})).status_code == 400
    result = client.post("/api/import-center/inspect", files=[
        ("files", ("page.mjs", b"export default {}")),
        ("files", ("resource.json", b"{}")),
    ])
    assert result.status_code == 400 and "逐包" in result.text
    assert inspect(client, "empty.mjs", b"").status_code == 400


def test_page_apply_requires_same_bytes_trust_and_explicit_overwrite(client, tmp_path):
    data = b"export default {}"
    fields = {"id": "test_page", "trusted": "true", "digest": hashlib.sha256(data).hexdigest()}
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", data)}, data={**fields, "trusted": "false"}).status_code == 403
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", b"changed")}, data=fields).status_code == 409
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", data)}, data=fields).status_code == 200
    target = tmp_path / "ext_pages" / "test_page" / "bundle.js"
    assert target.read_bytes() == data
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", data)}, data=fields).status_code == 409
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", data)}, data={**fields, "overwrite": "true"}).status_code == 200
    assert client.post("/api/import-center/page", files={"file": ("page.mjs", data)}, data={**fields, "id": "../escape"}).status_code == 400
