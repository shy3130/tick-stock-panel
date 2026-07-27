from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.research_ledger import router as research_router
from app.sycee.research_sharing import public_router
from app.sycee.research_sharing import router as sharing_router


def _client() -> TestClient:
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
    return TestClient(app)


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
