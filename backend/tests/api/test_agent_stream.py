import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agent as agent_api
from app.services import agent_sessions
from app.services.agent_bus import AgentBus


def _client(monkeypatch, tmp_path, fake_stream=None):
    if fake_stream is None:
        async def fake_stream(messages, app_state, profile_id=None, **kw):
            yield json.dumps({"type": "tool_call", "name": "list_strategies", "args": {}})
            yield json.dumps({"type": "tool_result", "name": "list_strategies", "result": {"strategies": []}})
            yield json.dumps({"type": "delta", "content": "答案"})
            yield json.dumps({"type": "done"})

    import app.services.agent_runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_agent_stream", fake_stream)
    bus = AgentBus()
    monkeypatch.setattr(agent_api, "get_bus", lambda: bus)
    monkeypatch.setattr(agent_api, "_TASKS", {})
    app = FastAPI()
    app.include_router(agent_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _send(client, sid, content="hi", **extra):
    body = {"messages": [{"role": "user", "content": content}], **extra}
    return client.post(f"/api/agent/sessions/{sid}/messages", json=body)


def _wait_for_assistant(tmp_path, sid):
    import time

    rows = []
    for _ in range(50):
        rows = agent_sessions.read_messages(tmp_path, sid)
        if any(r["role"] == "assistant" for r in rows):
            return rows
        time.sleep(0.05)
    return rows


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


def test_send_returns_attempt_and_watch_streams_events(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    sent = _send(client, sid).json()
    assert sent["session_id"] == sid
    assert sent["attempt_id"].startswith("agent_attempt_")

    with client.stream("GET", f"/api/agent/sessions/{sid}/stream") as resp:
        assert resp.status_code == 200
        assert "x-ndjson" in resp.headers["content-type"]
        events = [json.loads(line) for line in resp.iter_lines() if line.strip()]

    types = [e["type"] for e in events]
    assert types[0] == "attempt_start"
    assert "delta" in types and types[-1] == "done"


def test_send_persists_user_and_assistant_messages(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    _send(client, sid)
    with client.stream("GET", f"/api/agent/sessions/{sid}/stream") as resp:
        list(resp.iter_lines())

    rows = client.get(f"/api/agent/sessions/{sid}/messages").json()["messages"]
    assert [(r["role"], r["content"]) for r in rows] == [("user", "hi"), ("assistant", "答案")]
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "done"


def test_send_persists_display_content_not_attachment_context(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"messages": [{
            "role": "user",
            "content": "请总结\n\n## 用户附件（只读上下文）\nsecret attachment text",
            "display_content": "请总结",
        }]},
    )
    with client.stream("GET", f"/api/agent/sessions/{sid}/stream") as resp:
        list(resp.iter_lines())

    rows = client.get(f"/api/agent/sessions/{sid}/messages").json()["messages"]
    assert rows[0]["content"] == "请总结"


def test_send_starts_attempt_without_watcher(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]
    _send(client, sid)

    rows = _wait_for_assistant(tmp_path, sid)
    assert [(r["role"], r["content"]) for r in rows] == [("user", "hi"), ("assistant", "答案")]


def test_cancel_missing_attempt_returns_false(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/agent/attempts/agent_attempt_missing/cancel").json() == {"cancelled": False}


def test_send_rejects_non_user_last_message(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    resp = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"messages": [{"role": "assistant", "content": "not a user message"}]},
    )

    assert resp.status_code == 400
    assert agent_sessions.read_messages(tmp_path, sid) == []


def test_send_rejects_concurrent_attempt_for_same_session(monkeypatch, tmp_path):
    class LiveTask:
        def done(self):
            return False

    client = _client(monkeypatch, tmp_path)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]
    agent_sessions.set_attempt(tmp_path, sid, "agent_attempt_busy", "running")
    agent_api._TASKS["agent_attempt_busy"] = LiveTask()

    second = _send(client, sid, content="second message while first still running")
    assert second.status_code == 409

    assert agent_sessions.read_messages(tmp_path, sid) == []
