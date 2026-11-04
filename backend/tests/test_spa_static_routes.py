"""前端构建产物的静态路由 — 根级文件不能被 SPA 兜底成 HTML。

Docker/桌面安装包由 FastAPI 托管 frontend/dist。favicon.svg 等根级资源若被
SPA 兜底返回 index.html, 浏览器会因 MIME 不符丢弃图标, 页签显示默认占位图。
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import __version__
from app.main import _register_static_routes

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"></svg>'
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
INDEX_HTML = "<!doctype html><div id=root></div>"
NO_STORE = "no-store, must-revalidate"


def _client(static_dir: Path) -> TestClient:
    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "index-abc123.js").write_text("console.log(1)")
    (static_dir / "favicon.svg").write_text(SVG)
    (static_dir / "donate-wechat.jpg").write_bytes(JPEG)
    (static_dir / "index.html").write_text(INDEX_HTML)

    app = FastAPI()
    _register_static_routes(app, static_dir)
    return TestClient(app)


def test_favicon_is_served_as_svg_not_spa_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text == SVG


def test_root_image_is_served_instead_of_spa_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/donate-wechat.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG


def test_unknown_route_still_falls_back_to_index_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/screener")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == NO_STORE
    assert response.text == INDEX_HTML


def test_index_html_direct_request_stays_uncached(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/index.html")

    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE
    assert response.text == INDEX_HTML


@pytest.mark.parametrize("prefix", ["", "/assets"])
@pytest.mark.parametrize(("suffix", "media_type"), [
    (".js", "text/javascript"), (".mjs", "text/javascript"),
    (".css", "text/css"), (".wasm", "application/wasm"),
    (".json", "application/json"), (".svg", "image/svg+xml"),
    (".woff", "font/woff"), (".woff2", "font/woff2"),
])
def test_frontend_mime_is_independent_of_system_mapping(
    tmp_path: Path, monkeypatch, prefix: str, suffix: str, media_type: str,
) -> None:
    client = _client(tmp_path)
    # 注册表/其他库污染全局表后，静态响应仍须使用前端资源的标准类型。
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, suffix, "text/plain")
    asset = tmp_path / prefix.lstrip("/") / f"entry{suffix}"
    asset.write_bytes(b"frontend asset")

    response = client.get(f"{prefix}/entry{suffix}")

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == media_type
    assert response.content == b"frontend asset"


def test_index_html_is_html_even_when_system_mapping_is_wrong(tmp_path, monkeypatch):
    client = _client(tmp_path)
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".html", "text/plain")
    assert client.get("/").headers["content-type"].startswith("text/html")


def test_entry_urls_bypass_legacy_wrong_mime_cache(tmp_path, monkeypatch):
    client = _client(tmp_path)
    (tmp_path / "assets" / "index.css").write_text("body { color: red }")
    (tmp_path / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/assets/index.css">'
        "<script type='module' src='/assets/index-abc123.js'></script>"
    )
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    # 旧版本的 URL/响应可能已被客户端缓存，新 HTML 必须使用不同资源 URL。
    legacy = FastAPI()
    legacy.mount("/assets", StaticFiles(directory=tmp_path / "assets"))
    old = TestClient(legacy).get("/assets/index-abc123.js")
    assert old.headers["content-type"].startswith("text/plain")

    html = client.get("/").text
    urls = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html)
    assert len(urls) == 2
    assert all(not url.startswith("/assets/") for url in urls)
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        expected = "text/javascript" if url.endswith(".js") else "text/css"
        assert response.headers["content-type"].startswith(expected)


def test_frontend_static_preserves_conditional_head_and_range_requests(tmp_path):
    client = _client(tmp_path)
    response = client.get("/assets/index-abc123.js")
    assert client.get(
        "/assets/index-abc123.js", headers={"If-None-Match": response.headers["etag"]},
    ).status_code == 304
    head = client.head("/assets/index-abc123.js")
    assert head.status_code == 200
    assert head.content == b""
    partial = client.get("/assets/index-abc123.js", headers={"Range": "bytes=0-6"})
    assert partial.status_code == 206
    assert partial.content == b"console"


@pytest.mark.parametrize("path", ["missing.js", "%2e%2e/favicon.svg"])
def test_versioned_assets_do_not_fall_back_or_escape_asset_directory(tmp_path, path):
    response = _client(tmp_path).get(f"/_static/{__version__}/assets/{path}")
    assert response.status_code == 404
