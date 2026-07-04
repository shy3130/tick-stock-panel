import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agent as agent_api


def _client(monkeypatch):
    async def fake_run(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "tool_call", "name": "list_strategies", "args": {}})
        yield json.dumps({"type": "tool_result", "name": "list_strategies", "result": {"strategies": []}})
        yield json.dumps({"type": "delta", "content": "答案"})
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(agent_api, "run_agent_stream", fake_run, raising=False)
    app = FastAPI()
    app.include_router(agent_api.router)
    app.state.repo = object()
    return TestClient(app)


def test_agent_stream_returns_ndjson_events(monkeypatch):
    client = _client(monkeypatch)
    with client.stream(
        "POST",
        "/api/agent/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        assert "x-ndjson" in resp.headers["content-type"]
        lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert [e["type"] for e in lines] == ["tool_call", "tool_result", "delta", "done"]
