"""运行时页面插件 API 测试 — 上传校验/生命周期/bundle 分发。

TestClient 最小 app 风格(同 test_ext_market_level): 不起完整后端,
app.state.repo 只提供 store.data_dir。
"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.api.ext_pages import router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _bundle_js() -> bytes:
    # 最小合法 ES 模块产物: default 导出 FrontendExtension 形状
    return (
        "const extension = { id: 'demo.page', apiVersion: 1, "
        "routes: [{ id: 'demo-page', path: '/demo-page', component: () => null }], "
        "navigation: [{ id: 'demo-page', routeId: 'demo-page', label: '演示页', icon: null }] };\n"
        "export default extension;\n"
    ).encode()


def _upload(client: TestClient, pack_id: str, data: bytes, filename: str = "extension.js"):
    return client.post(
        "/api/ext-pages/upload",
        data={"id": pack_id},
        files={"file": (filename, io.BytesIO(data), "text/javascript")},
    )


def test_upload_and_lifecycle(client: TestClient, tmp_path: Path):
    r = _upload(client, "demo_page", _bundle_js())
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["id"] == "demo_page"
    assert meta["enabled"] is False  # 上传后未验证, 默认停用
    assert meta["size"] == len(_bundle_js()) and len(meta["sha256"]) == 64
    assert (tmp_path / "ext_pages" / "demo_page" / "bundle.js").exists()

    # 清单回填 → 启用
    r = client.put(
        "/api/ext-pages/demo_page/manifest",
        json={"label": "演示页", "path": "/demo-page", "error": ""},
    )
    assert r.status_code == 200 and r.json()["enabled"] is True

    # 列表
    r = client.get("/api/ext-pages")
    assert [m["id"] for m in r.json()] == ["demo_page"]

    # 分发: MIME + no-store + ETag
    r = client.get("/api/ext-pages/demo_page/bundle.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-store"
    assert "etag" in r.headers
    assert b"export default" in r.content

    # 停用 / 删除
    assert client.put("/api/ext-pages/demo_page/enabled", json={"enabled": False}).json()["enabled"] is False
    assert client.delete("/api/ext-pages/demo_page").json() == {"ok": True}
    assert client.get("/api/ext-pages/demo_page/bundle.js").status_code == 404


def test_upload_rejects_bad_suffix_and_id(client: TestClient):
    assert _upload(client, "x", b"a,b", "data.csv").status_code == 400
    # 路径穿越/非法 ID 一律拒绝(空 ID 由 FastAPI 表单校验 422), 不得落盘
    for bad in ("../evil", "a/b", "bad id", "x" * 0, ""):
        assert _upload(client, bad, _bundle_js()).status_code in (400, 422)


def test_upload_size_cap_413(client: TestClient):
    big = b"//" + b"x" * (5 * 1024 * 1024 + 10)
    assert _upload(client, "big", big).status_code == 413


def test_upload_empty_file_400(client: TestClient):
    assert _upload(client, "empty", b"").status_code == 400


def test_upload_overwrites_same_id(client: TestClient):
    _upload(client, "same", _bundle_js())
    v2 = _bundle_js() + b"// v2\n"
    _upload(client, "same", v2)
    r = client.get("/api/ext-pages/same/bundle.js")
    assert r.content == v2  # 覆盖式更新


def test_manifest_error_disables(client: TestClient):
    _upload(client, "bad", _bundle_js())
    r = client.put(
        "/api/ext-pages/bad/manifest",
        json={"label": "", "path": "", "error": "apiVersion 不匹配"},
    )
    assert r.json()["enabled"] is False and r.json()["error"] == "apiVersion 不匹配"


def test_missing_pack_404(client: TestClient):
    assert client.get("/api/ext-pages/none/bundle.js").status_code == 404
    assert client.put("/api/ext-pages/none/enabled", json={"enabled": True}).status_code == 404


def test_write_bundle_capped_stops_reading(tmp_path: Path):
    """越限立即停止读取: 计数流在 413 后不应被继续消费。"""
    import asyncio

    from app.api.ext_pages import _write_bundle_capped

    class CountingStream:
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)
            self.reads = 0

        # UploadFile 经 run_in_threadpool 同步调用 file.read, 这里保持同步签名
        def read(self, n: int = -1) -> bytes:
            self.reads += 1
            return self._buf.read(n)

    stream = CountingStream(b"x" * 200)
    with pytest.raises(Exception) as exc:
        asyncio.run(_write_bundle_capped(UploadFile(stream), tmp_path / "dummy.bin", max_bytes=64))
    assert getattr(exc.value, "status_code", None) == 413
    assert stream.reads == 1  # 超限块读出后不再继续读
