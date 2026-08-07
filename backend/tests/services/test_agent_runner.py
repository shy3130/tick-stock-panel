import asyncio
import json

import pytest

from app.services import agent_runner
from app.services import agent_sessions
from app.services.agent_bus import AgentBus


def _make_session(tmp_path) -> str:
    sid = agent_sessions.create_session(tmp_path, "t")["session_id"]
    agent_sessions.set_attempt(tmp_path, sid, "agent_attempt_x", "running")
    return sid


async def test_turn_persists_assistant_and_marks_done(tmp_path, monkeypatch):
    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "答"})
        yield json.dumps({"type": "delta", "content": "案"})
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    await agent_runner.run_agent_turn(
        data_dir=tmp_path,
        session_id=sid,
        attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}],
        app_state=object(),
        profile_id=None,
        bus=bus,
    )

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "答案")]
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "done"


async def test_turn_persists_even_with_no_subscriber(tmp_path, monkeypatch):
    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "独立"})
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    await asyncio.create_task(agent_runner.run_agent_turn(
        data_dir=tmp_path,
        session_id=sid,
        attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}],
        app_state=object(),
        profile_id=None,
        bus=bus,
    ))

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "独立")]


async def test_turn_cancelled_midstream_still_persists_partial(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "部分"})
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    task = asyncio.create_task(agent_runner.run_agent_turn(
        data_dir=tmp_path,
        session_id=sid,
        attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}],
        app_state=object(),
        profile_id=None,
        bus=bus,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "部分\n[已停止]"
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "cancelled"


async def test_turn_records_error_status(tmp_path, monkeypatch):
    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "error", "message": "boom"})

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    await agent_runner.run_agent_turn(
        data_dir=tmp_path,
        session_id=sid,
        attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}],
        app_state=object(),
        profile_id=None,
        bus=bus,
    )

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert rows[0]["content"] == "[错误] boom"
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "error"
