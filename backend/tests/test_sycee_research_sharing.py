from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee import research_ledger, research_sharing
from app.sycee.research_ledger import router as research_router
from app.sycee.research_sharing import public_router
from app.sycee.research_sharing import router as sharing_router


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def bind_test_user(request: Request, call_next):
        if request.url.path.startswith("/api/public/"):
            assert user_context.get_current_user() is None
            return await call_next(request)
        user_id = request.headers.get("x-test-user", "admin")
        token = user_context.set_current_user(UserIdentity(id=user_id, username=user_id))
        try:
            return await call_next(request)
        finally:
            user_context.reset(token)

    app.include_router(research_router)
    app.include_router(sharing_router)
    app.include_router(public_router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _fail_first_share_index_write(monkeypatch) -> None:
    real_write_index = research_sharing._write_index_unlocked
    writes = 0

    def fail_first_index_write(shares):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("simulated share index failure")
        return real_write_index(shares)

    monkeypatch.setattr(research_sharing, "_write_index_unlocked", fail_first_index_write)


def _fail_share_index_replace(monkeypatch) -> None:
    real_replace = research_sharing.os.replace

    def fail_index_replace(source, target):
        if Path(target).name == "research_shares.json":
            raise OSError("simulated share index replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(research_sharing.os, "replace", fail_index_replace)


def _fail_first_research_write(monkeypatch) -> None:
    real_write = research_ledger._write_unlocked
    writes = 0

    def fail_first_write(entries):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("simulated research ledger failure")
        return real_write(entries)

    monkeypatch.setattr(research_ledger, "_write_unlocked", fail_first_write)


def _entry_payload(title: str = "贵州茅台渠道验证") -> dict:
    return {
        "title": title,
        "subject_type": "stock",
        "subject": "600519.SH",
        "thesis": "渠道库存改善可能带来估值修复。",
        "evidence": ["批价企稳"],
        "counter_evidence": ["消费复苏仍可能低于预期"],
        "invalidation": "批价连续四周回落。",
        "plan": "等待下一期经营数据。",
        "status": "tracking",
        "tags": ["白酒"],
    }


def _capture_payload() -> dict:
    return {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "source": "watchlist",
        "source_label": "自选",
        "source_key": "watchlist:/private/path",
        "summary": "从自选页加入研究",
        "snapshot": {"path": "/private/path", "price": 1550.0},
    }


def test_share_snapshot_lifecycle_and_public_redaction(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    client.post("/api/sycee/research/capture", json=_capture_payload())

    created = client.post(f"/api/sycee/research/{entry['id']}/share")
    assert created.status_code == 201
    share = created.json()["share"]
    assert len(share["token"]) >= 40

    repeated = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert repeated["token"] == share["token"]

    public = client.get(f"/api/public/sycee/research/{share['token']}")
    assert public.status_code == 200
    assert public.headers["cache-control"] == "no-store"
    assert public.headers["x-robots-tag"] == "noindex, nofollow"
    document = public.json()
    assert "owner" not in document
    assert "id" not in document["entry"]
    assert document["entry"]["thesis"] == _entry_payload()["thesis"]
    capture = document["entry"]["captures"][0]
    assert capture == {
        "captured_at": capture["captured_at"],
        "source_label": "自选",
        "summary": "从自选页加入研究",
    }
    assert "source_key" not in capture
    assert "snapshot" not in capture

    client.patch(
        f"/api/sycee/research/{entry['id']}",
        json={"thesis": "更新后的判断。"},
    )
    unchanged = client.get(f"/api/public/sycee/research/{share['token']}").json()
    assert unchanged["entry"]["thesis"] == _entry_payload()["thesis"]

    refreshed = client.put(f"/api/sycee/research/{entry['id']}/share")
    assert refreshed.status_code == 200
    assert refreshed.json()["share"]["token"] == share["token"]
    updated = client.get(f"/api/public/sycee/research/{share['token']}").json()
    assert updated["entry"]["thesis"] == "更新后的判断。"

    revoked = client.delete(f"/api/sycee/research/{entry['id']}/share")
    assert revoked.status_code == 200
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 404


def test_failed_share_creation_cleans_public_and_temp_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    _fail_share_index_replace(monkeypatch)

    response = client.post(f"/api/sycee/research/{entry['id']}/share")

    assert response.status_code == 500
    assert client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"] is None
    public_dir = tmp_path / "sycee_public" / "research_shares"
    assert list(public_dir.glob("*.json")) == []
    assert list(tmp_path.rglob("*.tmp")) == []


def test_failed_share_refresh_keeps_previous_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    previous = client.get(f"/api/public/sycee/research/{share['token']}").json()
    client.patch(
        f"/api/sycee/research/{entry['id']}",
        json={"thesis": "索引失败时不应公开的新判断。"},
    )
    _fail_share_index_replace(monkeypatch)

    response = client.put(f"/api/sycee/research/{entry['id']}/share")

    assert response.status_code == 500
    public = client.get(f"/api/public/sycee/research/{share['token']}").json()
    assert public == previous
    current = client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert current == share
    assert list(tmp_path.rglob("*.tmp")) == []


def test_share_management_is_user_isolated_but_public_read_is_anonymous(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]

    alice = {"x-test-user": "alice"}
    assert client.get(f"/api/sycee/research/{entry['id']}/share", headers=alice).status_code == 404
    assert client.delete(f"/api/sycee/research/{entry['id']}/share", headers=alice).status_code == 404
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 200


def test_deleting_research_revokes_its_share(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]

    assert client.delete(f"/api/sycee/research/{entry['id']}").status_code == 200
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 404
    assert client.get("/api/public/sycee/research/not-a-token").status_code == 404


def test_failed_share_revocation_keeps_research_entry_and_link(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    _fail_first_share_index_write(monkeypatch)

    response = client.delete(f"/api/sycee/research/{entry['id']}")

    assert response.status_code == 500
    assert client.get("/api/sycee/research").json()["total"] == 1
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 200
    current = client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert current["token"] == share["token"]


def test_failed_research_delete_restores_revoked_share(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    entry = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    _fail_first_research_write(monkeypatch)

    response = client.delete(f"/api/sycee/research/{entry['id']}")

    assert response.status_code == 500
    assert client.get("/api/sycee/research").json()["total"] == 1
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 200
    current = client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert current["token"] == share["token"]


def test_undoing_an_auto_created_research_revokes_its_share(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    captured = client.post("/api/sycee/research/capture", json=_capture_payload()).json()
    entry = captured["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]

    undone = client.delete(
        f"/api/sycee/research/{entry['id']}/captures/{captured['capture_id']}"
    )
    assert undone.status_code == 200
    assert undone.json()["entry_deleted"] is True
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 404


def test_failed_share_revocation_keeps_auto_created_research(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    captured = client.post("/api/sycee/research/capture", json=_capture_payload()).json()
    entry = captured["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    _fail_first_share_index_write(monkeypatch)

    response = client.delete(
        f"/api/sycee/research/{entry['id']}/captures/{captured['capture_id']}"
    )

    assert response.status_code == 500
    ledger = client.get("/api/sycee/research").json()
    assert ledger["total"] == 1
    assert ledger["entries"][0]["captures"][0]["id"] == captured["capture_id"]
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 200
    current = client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert current["token"] == share["token"]


def test_failed_auto_research_delete_restores_revoked_share(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client(raise_server_exceptions=False)
    captured = client.post("/api/sycee/research/capture", json=_capture_payload()).json()
    entry = captured["entry"]
    share = client.post(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    _fail_first_research_write(monkeypatch)

    response = client.delete(
        f"/api/sycee/research/{entry['id']}/captures/{captured['capture_id']}"
    )

    assert response.status_code == 500
    ledger = client.get("/api/sycee/research").json()
    assert ledger["total"] == 1
    assert ledger["entries"][0]["captures"][0]["id"] == captured["capture_id"]
    assert client.get(f"/api/public/sycee/research/{share['token']}").status_code == 200
    current = client.get(f"/api/sycee/research/{entry['id']}/share").json()["share"]
    assert current["token"] == share["token"]
