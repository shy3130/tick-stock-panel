import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agent as agent_api


def _client(monkeypatch, tmp_path):
    async def fake_run(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "tool_call", "name": "list_strategies", "args": {}})
        yield json.dumps({"type": "tool_result", "name": "list_strategies", "result": {"strategies": []}})
        yield json.dumps({"type": "delta", "content": "答案"})
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(agent_api, "run_agent_stream", fake_run, raising=False)
    app = FastAPI()
    app.include_router(agent_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_agent_stream_returns_ndjson_events(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client.stream(
        "POST",
        "/api/agent/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        assert "x-ndjson" in resp.headers["content-type"]
        lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert [e["type"] for e in lines] == ["tool_call", "tool_result", "delta", "done"]


def test_agent_sessions_crud(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    created = client.post("/api/agent/sessions", json={"title": "测试"}).json()
    sid = created["session_id"]
    assert created["title"] == "测试"
    assert client.get("/api/agent/sessions").json()["sessions"][0]["session_id"] == sid

    renamed = client.patch(f"/api/agent/sessions/{sid}", json={"title": "改名"}).json()
    assert renamed["title"] == "改名"
    assert client.get(f"/api/agent/sessions/{sid}/messages").json() == {"messages": []}

    assert client.delete(f"/api/agent/sessions/{sid}").json() == {"deleted": True}
    assert client.get(f"/api/agent/sessions/{sid}/messages").status_code == 404


def test_agent_stream_persists_session_messages(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    with client.stream(
        "POST",
        "/api/agent/stream",
        json={"session_id": sid, "messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        list(resp.iter_lines())

    rows = client.get(f"/api/agent/sessions/{sid}/messages").json()["messages"]
    assert [(r["role"], r["content"]) for r in rows] == [("user", "hi"), ("assistant", "答案")]


def test_agent_session_stream_emits_attempt_start(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    with client.stream(
        "POST",
        "/api/agent/stream",
        json={"session_id": sid, "messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]

    assert lines[0]["type"] == "attempt_start"
    assert lines[0]["session_id"] == sid
    assert lines[0]["attempt_id"].startswith("agent_attempt_")


def test_agent_cancel_attempt_endpoint(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    attempt_id = "agent_attempt_test"
    assert client.post(f"/api/agent/attempts/{attempt_id}/cancel").json() == {"cancelled": False}

    agent_api._ACTIVE_ATTEMPTS.add(attempt_id)
    try:
        assert client.post(f"/api/agent/attempts/{attempt_id}/cancel").json() == {"cancelled": True}
    finally:
        agent_api._ACTIVE_ATTEMPTS.discard(attempt_id)
        agent_api._CANCELLED_ATTEMPTS.discard(attempt_id)


def test_agent_stream_persists_display_content_not_attachment_context(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    with client.stream(
        "POST",
        "/api/agent/stream",
        json={
            "session_id": sid,
            "messages": [{
                "role": "user",
                "content": "请总结\n\n## 用户附件（只读上下文）\nsecret attachment text",
                "display_content": "请总结",
            }],
        },
    ) as resp:
        assert resp.status_code == 200
        list(resp.iter_lines())

    rows = client.get(f"/api/agent/sessions/{sid}/messages").json()["messages"]
    assert rows[0]["content"] == "请总结"
