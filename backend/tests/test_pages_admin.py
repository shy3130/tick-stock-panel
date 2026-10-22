"""页面管理源码扩展删除: 校验拒绝路径、先备份后删除、备份下载穿越防御。"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import pages_admin


def _make_client(tmp_path: Path, monkeypatch, custom_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(pages_admin.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))
    monkeypatch.setattr(pages_admin, "_custom_root", lambda: custom_root)
    return TestClient(app)


def _seed_custom(root: Path, ns: str = "zz_test_delete") -> Path:
    ns_dir = root / ns
    ns_dir.mkdir(parents=True)
    (ns_dir / "extension.tsx").write_text("export default { id: 'zz_test_delete' }", encoding="utf-8")
    (root / "_template").mkdir(exist_ok=True)
    return ns_dir


def test_delete_creates_backup_then_removes_dir(tmp_path, monkeypatch):
    root = tmp_path / "frontend" / "src" / "custom"
    _seed_custom(root)
    client = _make_client(tmp_path, monkeypatch, root)

    r = client.delete("/api/pages-admin/source-extensions/zz_test_delete")

    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == "zz_test_delete"
    assert body["backup"].startswith("zz_test_delete-") and body["backup"].endswith(".zip")
    # 目录已删, 备份存在且包含带命名空间前缀的源文件
    assert not (root / "zz_test_delete").exists()
    backup = tmp_path / "data" / "backups" / "pages" / body["backup"]
    assert backup.is_file()
    with zipfile.ZipFile(backup) as zf:
        assert "zz_test_delete/extension.tsx" in zf.namelist()


def test_rejects_template_illegal_and_missing(tmp_path, monkeypatch):
    root = tmp_path / "frontend" / "src" / "custom"
    _seed_custom(root)
    client = _make_client(tmp_path, monkeypatch, root)

    assert client.delete("/api/pages-admin/source-extensions/_template").status_code == 400
    # 编码斜杠被路由层判 404、正则不匹配判 400 — 两种都是拒绝, 关键是不触发删除
    assert client.delete("/api/pages-admin/source-extensions/..%2Fetc").status_code in (400, 404)
    assert client.delete("/api/pages-admin/source-extensions/Has-Caps").status_code == 400
    assert client.delete("/api/pages-admin/source-extensions/not_exist").status_code == 404
    # 模板目录必须原样保留
    assert (root / "_template").is_dir()


def test_rejects_when_no_frontend_source_tree(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, tmp_path / "no-such-root")
    r = client.delete("/api/pages-admin/source-extensions/zz_test_delete")
    assert r.status_code == 409


def test_backup_download_serves_only_own_archives(tmp_path, monkeypatch):
    root = tmp_path / "frontend" / "src" / "custom"
    _seed_custom(root)
    client = _make_client(tmp_path, monkeypatch, root)
    backup_name = client.delete("/api/pages-admin/source-extensions/zz_test_delete").json()["backup"]

    ok = client.get(f"/api/pages-admin/backups/{backup_name}")
    assert ok.status_code == 200
    assert ok.headers["content-disposition"].startswith("attachment")
    assert zipfile.ZipFile(io.BytesIO(ok.content)).namelist()

    # 穿越与非本模块命名的文件一律 404
    assert client.get("/api/pages-admin/backups/evil.zip").status_code == 404
    assert client.get("/api/pages-admin/backups/..%2Fsecrets.zip").status_code == 404
    assert client.get("/api/pages-admin/backups/zz-20990101-000000.zip").status_code == 404
