"""声效语音包 API: 上传/列表/删除/取文件 (data/sound_packs)。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(settings_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _mp3(name: str = "我的提示音.mp3", size: int = 128) -> tuple[str, bytes]:
    return name, b"ID3" + b"\x00" * (size - 3)


def test_upload_list_serve_roundtrip(client: TestClient, tmp_path: Path):
    name, data = _mp3()
    r = client.post(
        "/api/settings/sound-packs",
        files={"file": (name, data, "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pack"]["name"] == name
    assert (tmp_path / "sound_packs" / name).read_bytes() == data

    listed = client.get("/api/settings/sound-packs").json()["packs"]
    assert [p["name"] for p in listed] == [name]
    assert listed[0]["ext"] == "mp3" and listed[0]["size"] == len(data)

    served = client.get(f"/api/settings/sound-packs/{name}/file")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("audio/")
    assert served.content == data


def test_upload_rejects_bad_extension_and_empty(client: TestClient):
    r = client.post(
        "/api/settings/sound-packs",
        files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "不支持的音频格式" in r.json()["detail"]

    r = client.post(
        "/api/settings/sound-packs",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400


def test_upload_sanitizes_unsafe_stem(client: TestClient, tmp_path: Path):
    r = client.post(
        "/api/settings/sound-packs",
        files={"file": ("../ ../*?.mp3", b"ID3xx", "audio/mpeg")},
    )
    assert r.status_code == 200
    saved = r.json()["pack"]["name"]
    assert saved.endswith(".mp3")
    assert "/" not in saved and ".." not in saved and "*" not in saved
    assert (tmp_path / "sound_packs" / saved).is_file()


def test_upload_size_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_api, "_SOUND_PACK_MAX_BYTES", 8)
    r = client.post(
        "/api/settings/sound-packs",
        files={"file": ("big.wav", b"\x00" * 64, "audio/wav")},
    )
    assert r.status_code == 400
    assert "超过" in r.json()["detail"]


def test_same_name_reupload_replaces(client: TestClient):
    client.post("/api/settings/sound-packs", files={"file": ("a.wav", b"111", "audio/wav")})
    r = client.post("/api/settings/sound-packs", files={"file": ("a.wav", b"22222", "audio/wav")})
    assert r.status_code == 200
    served = client.get("/api/settings/sound-packs/a.wav/file")
    assert served.content == b"22222"


def test_delete_and_path_traversal_guard(client: TestClient, tmp_path: Path):
    client.post("/api/settings/sound-packs", files={"file": ("a.wav", b"111", "audio/wav")})
    assert client.delete("/api/settings/sound-packs/a.wav").status_code == 200
    assert client.delete("/api/settings/sound-packs/a.wav").status_code == 404

    # 穿越尝试: httpx 会规范化 ../ (改写为其他路径 → 路由 404), 编码变体解码后
    # 含 / 同样不匹配路由或被 _safe_pack_path 拒 (400)。两种都安全; 关键断言是
    # 语音包目录之外的任何文件都不可触达。
    for bad in ("../secrets_store.py", "..%2F..%2Fetc%2Fpasswd", "a/../b.wav"):
        assert client.delete(f"/api/settings/sound-packs/{bad}").status_code in (400, 404)
        assert client.get(f"/api/settings/sound-packs/{bad}/file").status_code in (400, 404)
    assert (tmp_path / "secrets_store.py").exists() is False

    # 守卫函数直测: 路径分隔符/点点/空白名一律 400
    from fastapi import HTTPException
    pack_dir = tmp_path / "sound_packs"
    for crafted in ("../x.wav", "a/b.wav", "..", " x.wav ", ""):
        with pytest.raises(HTTPException) as exc:
            settings_api._safe_pack_path(pack_dir, crafted)
        assert exc.value.status_code == 400
