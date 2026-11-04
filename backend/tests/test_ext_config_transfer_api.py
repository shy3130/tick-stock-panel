from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data as api
from app.config import settings
from app.services.api_gateway import required_scope
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "")
    refreshed = []
    monkeypatch.setattr(api, "_refresh_views", lambda _request: refreshed.append(tmp_path))
    monkeypatch.setattr(api.pull_scheduler, "refresh", lambda _path: pytest.fail("导入禁用配置不应启动调度"))
    ExtConfigStore(tmp_path).upsert(ExtConfig(
        id="auction", label="集合竞价", mode="timeseries", fields=[ExtField("price", "float")],
        pull=PullConfig(url="https://example.test", auth={"type": "header", "header": "X-API-Key"}),
    ))
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(api.router)
    with TestClient(app) as http:
        yield http, refreshed


def test_export_preview_and_import_keep_contract_and_refresh_consumers(client):
    http, refreshed = client
    result = http.get("/api/ext-data/auction/config/export")
    assert result.status_code == 200 and result.headers["content-type"] == "application/zip"
    assert "tsp-ext-data-auction.zip" in result.headers["content-disposition"]
    files = {"file": ("auction.zip", result.content)}
    preview = http.post("/api/ext-data/config/inspect", files=files)
    assert preview.status_code == 200 and preview.json()["requires_key"]
    assert preview.json()["conflict"]
    conflict = http.post("/api/ext-data/config/import", files=files)
    assert conflict.status_code == 409
    imported = http.post("/api/ext-data/config/import", files=files, data={"config_id": "auction_shared"})
    assert imported.status_code == 200 and imported.json()["id"] == "auction_shared"
    assert imported.json()["pull"]["enabled"] is False
    assert len(refreshed) == 1


def test_unknown_config_and_bad_uploads_are_clear_errors(client):
    http, _ = client
    assert http.get("/api/ext-data/missing/config/export").status_code == 404
    for payload in (b"", b"not a zip", b"x" * (4 * 1024 * 1024 + 1)):
        result = http.post("/api/ext-data/config/inspect", files={"file": ("x.zip", payload)})
        assert result.status_code == 400


def test_transfer_remains_management_only_for_api_tokens():
    assert required_scope("GET", "/api/ext-data/auction/config/export") is None
    assert required_scope("POST", "/api/ext-data/config/import") is None
    assert required_scope("POST", "/api/ext-data/config/inspect") is None
