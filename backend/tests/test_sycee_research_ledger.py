from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.services import user_context
from app.services.user_context import UserIdentity
from app.sycee.research_ledger import router


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def bind_test_user(request: Request, call_next):
        user_id = request.headers.get("x-test-user", "admin")
        token = user_context.set_current_user(UserIdentity(id=user_id, username=user_id))
        try:
            return await call_next(request)
        finally:
            user_context.reset(token)

    app.include_router(router)
    return TestClient(app)


def _entry_payload(title: str = "贵州茅台渠道验证") -> dict:
    return {
        "title": title,
        "subject_type": "stock",
        "subject": "600519.SH",
        "thesis": "渠道库存改善可能带来估值修复。",
        "evidence": ["批价企稳", "现金流质量改善"],
        "counter_evidence": ["消费复苏仍可能低于预期"],
        "invalidation": "核心产品批价连续四周回落。",
        "plan": "进入跟踪池,等待下一期经营数据。",
        "status": "tracking",
        "tags": ["白酒", "基本面"],
    }


def test_research_ledger_crud_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    created_response = client.post("/api/sycee/research", json=_entry_payload())
    assert created_response.status_code == 201
    created = created_response.json()["entry"]
    assert created["id"].startswith("research_")
    assert created["status"] == "tracking"
    assert created["created_at"] == created["updated_at"]

    listed = client.get("/api/sycee/research").json()
    assert listed["total"] == 1
    assert listed["entries"][0]["id"] == created["id"]

    updated_response = client.patch(
        f"/api/sycee/research/{created['id']}",
        json={"status": "validated", "plan": "结论已记录,转入季度复核。"},
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()["entry"]
    assert updated["status"] == "validated"
    assert updated["plan"] == "结论已记录,转入季度复核。"
    assert updated["created_at"] == created["created_at"]

    deleted = client.delete(f"/api/sycee/research/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert client.get("/api/sycee/research").json() == {"entries": [], "total": 0}


def test_research_ledger_is_isolated_per_user(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    admin_headers = {"x-test-user": "admin"}
    alice_headers = {"x-test-user": "alice"}
    admin_entry = client.post(
        "/api/sycee/research",
        headers=admin_headers,
        json=_entry_payload("管理员研究"),
    ).json()["entry"]
    alice_entry = client.post(
        "/api/sycee/research",
        headers=alice_headers,
        json=_entry_payload("Alice 研究"),
    ).json()["entry"]

    admin_rows = client.get("/api/sycee/research", headers=admin_headers).json()["entries"]
    alice_rows = client.get("/api/sycee/research", headers=alice_headers).json()["entries"]

    assert [row["id"] for row in admin_rows] == [admin_entry["id"]]
    assert [row["id"] for row in alice_rows] == [alice_entry["id"]]
    assert (tmp_path / "users" / "admin" / "sycee" / "research_ledger.json").exists()
    assert (tmp_path / "users" / "alice" / "sycee" / "research_ledger.json").exists()


def test_research_ledger_rejects_invalid_or_empty_content(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    missing_title = client.post("/api/sycee/research", json={"subject_type": "stock"})
    assert missing_title.status_code == 422

    invalid_status = client.post(
        "/api/sycee/research",
        json={**_entry_payload(), "status": "unknown"},
    )
    assert invalid_status.status_code == 422

    invalid_id = client.patch("/api/sycee/research/not-valid", json={"status": "archived"})
    assert invalid_id.status_code == 400


def _capture_payload(source_key: str = "watchlist:600519.SH") -> dict:
    return {
        "symbol": "600519.sh",
        "name": "贵州茅台",
        "source": "watchlist",
        "source_label": "自选",
        "source_key": source_key,
        "summary": "从自选页加入研究",
        "snapshot": {"path": "/watchlist", "price": 1550.0},
    }


def test_capture_creates_deduplicates_appends_and_undoes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()

    first = client.post("/api/sycee/research/capture", json=_capture_payload())
    assert first.status_code == 200
    first_result = first.json()
    entry_id = first_result["entry"]["id"]
    first_capture_id = first_result["capture_id"]
    assert first_result["action"] == "created"
    assert first_result["entry"]["origin"] == "capture"
    assert first_result["entry"]["subject"] == "600519.SH"
    assert len(first_result["entry"]["captures"]) == 1

    duplicate = client.post("/api/sycee/research/capture", json=_capture_payload()).json()
    assert duplicate["action"] == "duplicate"
    assert duplicate["capture_id"] == first_capture_id
    assert len(duplicate["entry"]["captures"]) == 1

    appended = client.post(
        "/api/sycee/research/capture",
        json=_capture_payload("monitor:1721532600000"),
    ).json()
    assert appended["action"] == "appended"
    assert len(appended["entry"]["captures"]) == 2

    undo_append = client.delete(
        f"/api/sycee/research/{entry_id}/captures/{appended['capture_id']}"
    ).json()
    assert undo_append["entry_deleted"] is False
    assert len(undo_append["entry"]["captures"]) == 1

    undo_create = client.delete(
        f"/api/sycee/research/{entry_id}/captures/{first_capture_id}"
    ).json()
    assert undo_create == {"ok": True, "entry_deleted": True, "entry": None}
    assert client.get("/api/sycee/research").json() == {"entries": [], "total": 0}


def test_capture_appends_to_manual_active_research_without_deleting_it_on_undo(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = _client()
    manual = client.post("/api/sycee/research", json=_entry_payload()).json()["entry"]

    captured = client.post("/api/sycee/research/capture", json=_capture_payload()).json()
    assert captured["action"] == "appended"
    assert captured["entry"]["id"] == manual["id"]
    assert captured["entry"]["origin"] == "manual"

    undone = client.delete(
        f"/api/sycee/research/{manual['id']}/captures/{captured['capture_id']}"
    ).json()
    assert undone["entry_deleted"] is False
    assert undone["entry"]["id"] == manual["id"]
    assert undone["entry"]["captures"] == []
