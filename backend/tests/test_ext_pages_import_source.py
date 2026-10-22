"""源码导入(.tsx → 页面插件): esbuild 编译、契约 shim、端点校验与 409 降级。

CI 无 frontend/node_modules → find_esbuild 为 None → 整组跳过 (源码部署本机全跑)。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_pages
from app.services import ext_source_import

pytestmark = pytest.mark.skipif(
    ext_source_import.find_esbuild() is None,
    reason="当前环境无 esbuild (需要 frontend/node_modules), 源码导入仅源码/桌面部署可测",
)

# 故意经由 @/ 别名引用宿主模块, 验证 alias 注入; 独立组件避免依赖 JSX 运行时差异
_FIXTURE_TSX = """\
import { createElement } from 'react'
import { cn } from '@/lib/cn'

export function Demo() {
  return createElement('div', { className: cn('p-2', 'text-sm') }, 'demo')
}

export default {
  id: 'zz_fixture_demo',
  apiVersion: 1 as const,
  routes: [{ id: 'demo', path: '/zz_fixture_demo', component: Demo }],
  navigation: [{ id: 'demo_nav', routeId: 'demo', label: '演示导入' }],
}
"""


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(ext_pages.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))
    return TestClient(app)


def test_compile_fixture_bundles_host_alias_and_shim(tmp_path):
    src = tmp_path / "fixture.tsx"
    src.write_text(_FIXTURE_TSX, encoding="utf-8")
    data, summary = ext_source_import.compile_source(src, "fixture")
    assert len(data) > 1000
    # shim 的命名空间补点逻辑与源扩展 id 都应在产物里
    assert b"imported." in data
    assert b"zz_fixture_demo" in data
    # @/lib/cn 已被 alias 解析打进包 (而不是残留裸模块说明符导致浏览器无法加载)
    assert b"from '@/lib/cn'" not in data
    assert "fixture.tsx" in summary


def test_upload_endpoint_compiles_and_stores(tmp_path):
    c = _client(tmp_path)
    r = c.post(
        "/api/ext-pages/import-source",
        files={"file": ("demo_page.tsx", _FIXTURE_TSX.encode(), "text/plain")},
        data={"id": "zz_imported_demo"},
    )
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["id"] == "zz_imported_demo"
    assert (tmp_path / "data" / "ext_pages" / "zz_imported_demo" / "bundle.js").is_file()


def test_upload_rejects_non_tsx(tmp_path):
    c = _client(tmp_path)
    r = c.post(
        "/api/ext-pages/import-source",
        files={"file": ("evil.js", b"export default {}", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_without_esbuild_returns_409(tmp_path, monkeypatch):
    monkeypatch.setattr(ext_source_import, "find_esbuild", lambda: None)
    c = _client(tmp_path)
    r = c.post(
        "/api/ext-pages/import-source",
        files={"file": ("demo.tsx", _FIXTURE_TSX.encode(), "text/plain")},
    )
    assert r.status_code == 409
    assert "esbuild" in r.json()["detail"]


def test_from_project_validates_namespace(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/ext-pages/import-source/from-project", json={"namespace": "_template"}).status_code == 400
    assert c.post("/api/ext-pages/import-source/from-project", json={"namespace": "../x"}).status_code == 400
    assert c.post("/api/ext-pages/import-source/from-project", json={"namespace": "not_exist_ns"}).status_code == 400


def test_from_project_compiles_real_namespace(tmp_path):
    """真实编译一个小体量的本项目扩展, 验证完整链路 (真实前端源码树)。"""
    c = _client(tmp_path)
    r = c.post("/api/ext-pages/import-source/from-project", json={"namespace": "concept-analysis"})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["id"] == "concept_analysis"
    bundle = (tmp_path / "data" / "ext_pages" / "concept_analysis" / "bundle.js").read_bytes()
    assert b"concept-analysis" in bundle
    assert len(bundle) < 5 * 1024 * 1024
