"""url 嵌入型扩展页面: 模板绑定校验 (url 必填 http(s) / 数据型模板仍需 data_source)。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analysis


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(analysis.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _url_body(**overrides) -> dict:
    body = {"label": "外部页面", "template": "url", "url": "https://example.com/embed"}
    body.update(overrides)
    return body


def test_url_template_roundtrip(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/analysis-menus/my_embed", json=_url_body())
    assert r.status_code == 200, r.text
    saved = c.get("/api/analysis-menus/my_embed").json()
    assert saved["template"] == "url"
    assert saved["url"] == "https://example.com/embed"
    assert saved["label"] == "外部页面"


def test_url_template_rejects_missing_bad_and_long_urls(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/analysis-menus/a", json=_url_body(url=None)).status_code == 422
    assert c.post("/api/analysis-menus/b", json=_url_body(url="javascript:alert(1)")).status_code == 422
    assert c.post("/api/analysis-menus/d", json=_url_body(url="ftp://x/y")).status_code == 422
    assert c.post("/api/analysis-menus/e", json=_url_body(url="https://x/" + "a" * 600)).status_code == 422


def test_data_templates_still_require_data_source(tmp_path):
    c = _client(tmp_path)
    body = {"label": "排行", "template": "ranking", "data_source": ""}
    assert c.post("/api/analysis-menus/r1", json=body).status_code == 422
    # 正常数据型保存不受影响
    ok = c.post("/api/analysis-menus/r2", json={**body, "data_source": "ext_hy_ths"})
    assert ok.status_code == 200
    assert ok.json()["data_source"] == "ext_hy_ths"


def test_url_template_ignores_data_source(tmp_path):
    """url 模板不绑数据源: 缺省与传入都被接受, 保存值为空串。"""
    c = _client(tmp_path)
    r = c.post("/api/analysis-menus/u1", json=_url_body(data_source=""))
    assert r.status_code == 200
    assert r.json()["data_source"] == ""
