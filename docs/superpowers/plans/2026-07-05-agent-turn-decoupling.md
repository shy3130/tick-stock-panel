# Agent Turn Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an agent turn run as an independent backend task so a client disconnect (browser refresh/close) can no longer kill the in-flight LLM call or silently discard the assistant's reply, and let a reloaded page reconnect to a still-running turn.

**Architecture:** The turn stops being an async generator wired 1:1 into a `StreamingResponse`. Instead, `POST /api/agent/sessions/{id}/messages` persists the user message and launches the turn as a fire-and-forget `asyncio.create_task`; that task publishes ndjson events to an in-process, per-session pub/sub (`AgentBus`) and — critically — persists the final assistant message in a `finally` that is always reached. Clients *watch* the turn through a separate, read-only `GET /api/agent/sessions/{id}/stream` subscription whose cancellation-on-disconnect only tears down the subscription, never the turn. Attempt status (`running`/`done`/`cancelled`/`error`) is persisted in the existing session index so a reloaded page can detect an in-flight turn and replay it.

**Tech Stack:** Python 3.13, FastAPI 0.136.1 / Starlette 1.0.1 / uvicorn 0.47.0, `asyncio`, pytest + pytest-asyncio (auto mode); React 18 + TypeScript (Vite), native `fetch` ndjson reader (no `EventSource`).

## Global Constraints

- **Runtime:** Python 3.13; backend deps are pinned — Starlette 1.0.1, uvicorn 0.47.0, FastAPI 0.136.1. Do not add new backend dependencies.
- **Root-cause fact (do not "fix" by upgrading):** uvicorn advertises ASGI `spec_version` `"2.3"`. Starlette 1.0.1 `StreamingResponse.__call__` (`.venv/.../starlette/responses.py:44-60`) races `stream_response` against `listen_for_disconnect` in an `anyio.create_task_group()` when `spec_version < (2, 4)`, cancelling the generator on client disconnect. The fix is architectural (execution lives in a separate task), NOT a version bump.
- **Persistence:** file-based JSON only, via `backend/app/services/agent_sessions.py` (atomic temp-file-then-`replace`). Do NOT introduce a database.
- **Streaming format:** ndjson, `media_type="application/x-ndjson"`, one JSON object per line. Keep it. Do NOT switch to `text/event-stream`/`EventSource`.
- **Test discipline (TDD):** every backend code step is preceded by a failing test. Backend tests build their own `FastAPI()` app + `TestClient` inline (there is no `conftest.py`); follow the style in `backend/tests/api/test_agent_stream.py` and `backend/tests/services/test_agent_loop.py`. pytest-asyncio runs in `asyncio_mode = "auto"` (from `backend/pyproject.toml:81`) — `async def test_*` needs no decorator.
- **Frontend has no test runner** (no vitest/jsdom in `frontend/package.json`; the only `*.test.ts` belongs to the off-limits three-locks work). Frontend tasks are gated by `pnpm --dir frontend build` (runs `tsc -b`) plus a scripted manual verification, NOT by unit tests.
- **Commits:** one deliverable per task, commit at the end of each task. Do not bundle tasks.
- **Do NOT touch** the three-locks work (`docs/superpowers/plans/2026-07-05-three-locks-indicator.md`, `backend/app/indicators/pipeline.py`, `backend/app/api/kline.py`, `frontend/src/components/EChartsCandlestick.tsx`, `frontend/src/lib/threeLocks*.ts`) or the uncommitted data-quality changes already in the working tree. None of this plan's files overlap them.
- **Read before edit:** re-read any file before modifying it; this plan quotes current line numbers but they may drift.
- **Single-process deployment only:** `AgentBus` and `_TASKS` (Task 1, Task 4) are in-process Python state — a plain `dict` and `asyncio.Task`s tied to one event loop. This design assumes tickflow runs as a single `uvicorn` process (no `--workers N > 1`, no multi-process load balancing) for the agent API. If `send`/`watch`/`cancel` for the same session ever land on different worker processes, reconnect/cancel silently degrade (watch sees an "unknown session" and closes immediately, per the Out-of-scope note below — indistinguishable from a stale/crashed attempt). This plan does not add cross-process coordination (Redis pub/sub, etc.) — that is out of scope; if multi-worker deployment is ever needed, it requires a follow-up design.
- **Breaking change, intentional:** `POST /api/agent/stream` is deleted outright in Task 4, not deprecated or kept as a compatibility shim. The only caller is `frontend/src/lib/api.ts`'s `agentStream`, which Task 5 replaces in the same plan — there is no external/other consumer of this endpoint to preserve compatibility for. If any ad-hoc script or manual `curl` workflow depends on the old endpoint, it will start receiving 404s after Task 4 lands.

---

## File Structure

**New backend files:**
- `backend/app/services/agent_bus.py` — in-process per-session pub/sub with a bounded replay buffer. Zero domain knowledge; pure `asyncio`.
- `backend/app/services/agent_runner.py` — the background turn coroutine `run_agent_turn(...)`: drives `run_agent_stream`, publishes to the bus, and unconditionally persists the assistant message + terminal status.
- `backend/tests/services/test_agent_bus.py`, `backend/tests/services/test_agent_runner.py`, `backend/tests/services/test_agent_sessions.py` — new unit tests.

**Modified backend files:**
- `backend/app/services/agent_sessions.py` — add `last_attempt_id`/`last_attempt_status` to the session index and two setters.
- `backend/app/api/agent.py` — replace `POST /stream` (send) with `POST /sessions/{id}/messages` + `GET /sessions/{id}/stream` (watch); rewire cancel to `asyncio.Task.cancel()`.
- `backend/tests/api/test_agent_stream.py` — rewrite the streaming tests for the new endpoints.

**Modified frontend files:**
- `frontend/src/lib/api.ts` — replace `agentStream` with `agentSend` + `agentWatch`; extend `AgentSession` type with attempt fields.
- `frontend/src/pages/Agent.tsx` — split send from watch; add reconnect-on-mount for a running attempt; extract a shared event reducer.

**Out of scope (future work, do not implement):**
- Tool-call traces (`tool_call`/`tool_result`) are still only in frontend React state — never persisted server-side, so tool visualizations are lost on reload even after a successful turn. Not addressed here.
- Intermediate streaming deltas live only in `AgentBus`'s in-memory ring buffer, so they are not durable across a full backend process restart (only the final assistant message is persisted to disk). This matches the reference project's accepted caveat; do NOT try to make deltas restart-durable. A process crash can also leave `last_attempt_status == "running"` on disk with no live task — the watch endpoint handles this gracefully (subscribing to a session the bus has never seen yields nothing and closes immediately), so the reloaded page simply shows the persisted messages.

---

### Task 1: AgentBus — in-process pub/sub with replay

**Files:**
- Create: `backend/app/services/agent_bus.py`
- Test: `backend/tests/services/test_agent_bus.py`

**Interfaces:**
- Consumes: nothing (stdlib `asyncio` only).
- Produces:
  - `class AgentBus` with methods:
    - `begin(session_id: str) -> None` — start a fresh attempt: reset the session's buffer and release any prior subscribers.
    - `publish(session_id: str, event: dict) -> None` — append `event` to the buffer (bounded to 500) and fan out to live subscribers.
    - `close(session_id: str) -> None` — mark the attempt finished and release all subscribers.
    - `async subscribe(session_id: str) -> AsyncIterator[dict]` — replay the current attempt's buffered events, then yield live events until `close()`; returns immediately (no hang) if the session is unknown or already closed.
  - `get_bus() -> AgentBus` — module-level singleton accessor.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_agent_bus.py`:

```python
import asyncio

from app.services.agent_bus import AgentBus, get_bus


async def _drain(bus: AgentBus, session_id: str) -> list[dict]:
    return [event async for event in bus.subscribe(session_id)]


async def test_late_subscriber_replays_buffer_then_ends_after_close():
    bus = AgentBus()
    bus.begin("s1")
    bus.publish("s1", {"type": "delta", "content": "a"})
    bus.publish("s1", {"type": "done"})
    bus.close("s1")

    events = await _drain(bus, "s1")

    assert events == [{"type": "delta", "content": "a"}, {"type": "done"}]


async def test_live_subscriber_receives_events_until_close():
    bus = AgentBus()
    bus.begin("s1")
    collected: list[dict] = []

    async def watch() -> None:
        async for event in bus.subscribe("s1"):
            collected.append(event)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)  # let the subscriber register
    bus.publish("s1", {"type": "delta", "content": "x"})
    bus.publish("s1", {"type": "done"})
    bus.close("s1")
    await task

    assert collected == [{"type": "delta", "content": "x"}, {"type": "done"}]


async def test_subscriber_that_joins_mid_run_gets_replay_plus_live():
    bus = AgentBus()
    bus.begin("s1")
    bus.publish("s1", {"type": "delta", "content": "1"})  # buffered before subscribe
    collected: list[dict] = []

    async def watch() -> None:
        async for event in bus.subscribe("s1"):
            collected.append(event)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    bus.publish("s1", {"type": "delta", "content": "2"})  # live, must not duplicate "1"
    bus.close("s1")
    await task

    assert collected == [{"type": "delta", "content": "1"}, {"type": "delta", "content": "2"}]


async def test_subscribe_unknown_session_returns_immediately():
    bus = AgentBus()
    assert await _drain(bus, "missing") == []


async def test_get_bus_is_singleton():
    assert get_bus() is get_bus()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.agent_bus'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/agent_bus.py`:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

_MAX_BUFFER = 500


class _SessionChannel:
    def __init__(self) -> None:
        self.buffer: list[tuple[int, dict[str, Any]]] = []
        self.subscribers: set[asyncio.Queue[tuple[int, dict[str, Any]] | None]] = set()
        self.closed: bool = False
        self.seq: int = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class AgentBus:
    """In-process, per-session event fan-out with a bounded replay buffer.

    One "attempt" per session at a time: begin() resets the channel, publish()
    appends + fans out, close() ends the attempt and releases subscribers.
    Each event carries a monotonic seq so a subscriber can replay the buffer and
    then de-dup live events against a watermark (no missed or duplicated events).
    """

    def __init__(self) -> None:
        self._channels: dict[str, _SessionChannel] = {}

    def begin(self, session_id: str) -> None:
        prior = self._channels.get(session_id)
        if prior is not None:
            for queue in list(prior.subscribers):
                queue.put_nowait(None)
            prior.subscribers.clear()
        self._channels[session_id] = _SessionChannel()

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        channel = self._channels.get(session_id)
        if channel is None:
            channel = _SessionChannel()
            self._channels[session_id] = channel
        seq = channel.next_seq()
        channel.buffer.append((seq, event))
        if len(channel.buffer) > _MAX_BUFFER:
            channel.buffer = channel.buffer[-_MAX_BUFFER:]
        for queue in list(channel.subscribers):
            queue.put_nowait((seq, event))

    def close(self, session_id: str) -> None:
        channel = self._channels.get(session_id)
        if channel is None:
            return
        channel.closed = True
        for queue in list(channel.subscribers):
            queue.put_nowait(None)
        channel.subscribers.clear()

    async def subscribe(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        channel = self._channels.get(session_id)
        if channel is None:
            return
        queue: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        channel.subscribers.add(queue)
        # If the attempt already finished, no further close() will fire — enqueue
        # our own sentinel so the loop below terminates after replay.
        if channel.closed:
            queue.put_nowait(None)
        try:
            replay = list(channel.buffer)
            watermark = replay[-1][0] if replay else 0
            for _seq, event in replay:
                yield event
            while True:
                item = await queue.get()
                if item is None:
                    return
                seq, event = item
                if seq > watermark:
                    yield event
        finally:
            channel.subscribers.discard(queue)


_BUS = AgentBus()


def get_bus() -> AgentBus:
    return _BUS
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_bus.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_bus.py backend/tests/services/test_agent_bus.py
git commit -m "feat(agent): add in-process AgentBus pub/sub with replay buffer"
```

---

### Task 2: Persist attempt status in the session index

**Files:**
- Modify: `backend/app/services/agent_sessions.py` (add fields in `create_session` ~lines 52-65; add two functions after `append_message` ~line 126)
- Test: `backend/tests/services/test_agent_sessions.py`

**Interfaces:**
- Consumes: existing `create_session`, `list_sessions`, `get_session`, `_write_json`, `_index_path`, `_now` from this module.
- Produces:
  - `create_session` result now includes `"last_attempt_id": None` and `"last_attempt_status": None`.
  - `set_attempt(data_dir: Path, session_id: str, attempt_id: str, status: str) -> None` — set both attempt fields + `updated_at`.
  - `set_attempt_status(data_dir: Path, session_id: str, status: str) -> None` — set `last_attempt_status` + `updated_at`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_agent_sessions.py`:

```python
from app.services import agent_sessions


def test_create_session_has_null_attempt_fields(tmp_path):
    item = agent_sessions.create_session(tmp_path, "t")
    assert item["last_attempt_id"] is None
    assert item["last_attempt_status"] is None


def test_set_attempt_then_status(tmp_path):
    sid = agent_sessions.create_session(tmp_path, "t")["session_id"]

    agent_sessions.set_attempt(tmp_path, sid, "agent_attempt_abc", "running")
    running = agent_sessions.get_session(tmp_path, sid)
    assert running["last_attempt_id"] == "agent_attempt_abc"
    assert running["last_attempt_status"] == "running"

    agent_sessions.set_attempt_status(tmp_path, sid, "done")
    done = agent_sessions.get_session(tmp_path, sid)
    assert done["last_attempt_id"] == "agent_attempt_abc"
    assert done["last_attempt_status"] == "done"


def test_set_attempt_status_missing_session_is_noop(tmp_path):
    agent_sessions.set_attempt_status(tmp_path, "nope", "done")  # must not raise
    assert agent_sessions.get_session(tmp_path, "nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_sessions.py -v`
Expected: FAIL — `test_create_session_has_null_attempt_fields` KeyError `'last_attempt_id'`; `test_set_attempt_then_status` AttributeError `module 'app.services.agent_sessions' has no attribute 'set_attempt'`.

- [ ] **Step 3: Add the fields to `create_session`**

In `backend/app/services/agent_sessions.py`, edit the `item` dict inside `create_session` (currently lines 54-60) to add the two new keys:

```python
    item = {
        "session_id": f"agent_{uuid.uuid4().hex[:12]}",
        "title": (title or "新对话").strip()[:80] or "新对话",
        "created_at": ts,
        "updated_at": ts,
        "message_count": 0,
        "last_attempt_id": None,
        "last_attempt_status": None,
    }
```

- [ ] **Step 4: Add the two setters**

Append to `backend/app/services/agent_sessions.py` (after `append_message`, at end of file):

```python
def set_attempt(data_dir: Path, session_id: str, attempt_id: str, status: str) -> None:
    """Record the session's current attempt id and status."""
    sessions = list_sessions(data_dir)
    changed = False
    for item in sessions:
        if item.get("session_id") == session_id:
            item["last_attempt_id"] = attempt_id
            item["last_attempt_status"] = status
            item["updated_at"] = _now()
            changed = True
            break
    if changed:
        _write_json(_index_path(data_dir), sessions)


def set_attempt_status(data_dir: Path, session_id: str, status: str) -> None:
    """Update only the current attempt's status (attempt id untouched)."""
    sessions = list_sessions(data_dir)
    changed = False
    for item in sessions:
        if item.get("session_id") == session_id:
            item["last_attempt_status"] = status
            item["updated_at"] = _now()
            changed = True
            break
    if changed:
        _write_json(_index_path(data_dir), sessions)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_sessions.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_sessions.py backend/tests/services/test_agent_sessions.py
git commit -m "feat(agent): persist last_attempt_id/status in session index"
```

---

### Task 3: `run_agent_turn` — the independent background coroutine (data-loss fix)

**Files:**
- Create: `backend/app/services/agent_runner.py`
- Test: `backend/tests/services/test_agent_runner.py`

**Interfaces:**
- Consumes: `AgentBus` (Task 1); `agent_sessions.append_message`, `agent_sessions.set_attempt_status` (Task 2); `agent_loop.run_agent_stream` (existing async generator yielding ndjson strings).
- Produces:
  - `async run_agent_turn(*, data_dir: Path, session_id: str, attempt_id: str, messages: list[dict], app_state: Any, profile_id: str | None, bus: AgentBus) -> None` — publishes an `attempt_start` event, relays every `run_agent_stream` event to the bus while accumulating assistant text, and in `finally` persists the assistant message and terminal status (`done`/`cancelled`/`error`) then calls `bus.close(session_id)`. Reached unconditionally, including on `asyncio.CancelledError`.

**Key design note:** `asyncio.CancelledError` subclasses `BaseException` (not `Exception`) on Python 3.13, so `run_agent_stream`'s internal `except Exception` does NOT swallow a task cancellation — it propagates out of the `async for` into this coroutine's `except asyncio.CancelledError`, where cleanup runs. This is what guarantees the assistant reply is saved even when the browser is gone.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_agent_runner.py`:

```python
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
        data_dir=tmp_path, session_id=sid, attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}], app_state=object(),
        profile_id=None, bus=bus,
    )

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "答案")]
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "done"


async def test_turn_persists_even_with_no_subscriber(tmp_path, monkeypatch):
    # No one ever calls bus.subscribe(): execution and persistence must be
    # completely independent of any watcher. This is the disconnect fix.
    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "独立"})
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    task = asyncio.create_task(agent_runner.run_agent_turn(
        data_dir=tmp_path, session_id=sid, attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}], app_state=object(),
        profile_id=None, bus=bus,
    ))
    await task

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "独立")]


async def test_turn_cancelled_midstream_still_persists_partial(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def fake_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "部分"})
        started.set()
        await asyncio.Event().wait()  # block until the task is cancelled

    monkeypatch.setattr(agent_runner, "run_agent_stream", fake_stream)
    bus = AgentBus()
    bus.begin("s")
    sid = _make_session(tmp_path)

    task = asyncio.create_task(agent_runner.run_agent_turn(
        data_dir=tmp_path, session_id=sid, attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}], app_state=object(),
        profile_id=None, bus=bus,
    ))
    await started.wait()
    task.cancel()
    await task  # run_agent_turn swallows CancelledError after cleanup

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
        data_dir=tmp_path, session_id=sid, attempt_id="agent_attempt_x",
        messages=[{"role": "user", "content": "hi"}], app_state=object(),
        profile_id=None, bus=bus,
    )

    rows = agent_sessions.read_messages(tmp_path, sid)
    assert rows[0]["content"] == "[错误] boom"
    assert agent_sessions.get_session(tmp_path, sid)["last_attempt_status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.agent_runner'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/agent_runner.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.services import agent_sessions
from app.services.agent_bus import AgentBus
from app.services.agent_loop import run_agent_stream


async def run_agent_turn(
    *,
    data_dir: Path,
    session_id: str,
    attempt_id: str,
    messages: list[dict],
    app_state: Any,
    profile_id: str | None,
    bus: AgentBus,
) -> None:
    """Run one agent turn to completion, independent of any HTTP request.

    Relays every event from run_agent_stream to `bus` while accumulating the
    assistant's text, and ALWAYS persists that text plus a terminal attempt
    status in the finally block — this is the fix for the data-loss bug where a
    client disconnect used to discard the already-generated reply.
    """
    assistant_chunks: list[str] = []
    status = "done"
    bus.publish(
        session_id,
        {"type": "attempt_start", "attempt_id": attempt_id, "session_id": session_id},
    )
    try:
        async for line in run_agent_stream(messages, app_state, profile_id):
            try:
                event = json.loads(line)
            except Exception:
                event = {}
            if event.get("type") == "delta" and isinstance(event.get("content"), str):
                assistant_chunks.append(event["content"])
            elif event.get("type") == "error" and isinstance(event.get("message"), str):
                assistant_chunks.append(f"[错误] {event['message']}")
                status = "error"
            bus.publish(session_id, event)
    except asyncio.CancelledError:
        status = "cancelled"
        assistant_chunks.append("\n[已停止]" if assistant_chunks else "[已停止]")
        bus.publish(session_id, {"type": "cancelled", "attempt_id": attempt_id})
    except Exception as exc:  # defensive: a bg task must never die silently
        status = "error"
        assistant_chunks.append(f"\n[错误] {exc}")
        bus.publish(session_id, {"type": "error", "message": str(exc)})
    finally:
        if assistant_chunks:
            agent_sessions.append_message(
                data_dir, session_id, "assistant", "".join(assistant_chunks)
            )
        agent_sessions.set_attempt_status(data_dir, session_id, status)
        bus.close(session_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/services/test_agent_runner.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_runner.py backend/tests/services/test_agent_runner.py
git commit -m "feat(agent): add run_agent_turn background coroutine that always persists reply"
```

---

### Task 4: New send/watch endpoints + task-based cancel in the API

**Files:**
- Modify: `backend/app/api/agent.py` (imports lines 1-18; replace `POST /stream` lines 128-181; rewrite cancel lines 85-90; remove `AgentStreamIn` lines 26-29)
- Test: `backend/tests/api/test_agent_stream.py` (rewrite)

**Interfaces:**
- Consumes: `agent_sessions` (Task 2 setters), `get_bus` (Task 1), `run_agent_turn` (Task 3).
- Produces:
  - `POST /api/agent/sessions/{session_id}/messages` body `{messages: list[dict], profile_id?: str}` → `{"attempt_id": str, "session_id": str}`. Persists the last user message, marks the attempt `running`, launches `run_agent_turn` via `asyncio.create_task`, tracks the task in `_TASKS`.
  - `GET /api/agent/sessions/{session_id}/stream` → ndjson `StreamingResponse` subscribing to the bus for that session (replay + live). Disconnecting only tears down the subscription.
  - `POST /api/agent/attempts/{attempt_id}/cancel` → `{"cancelled": bool}`; now calls `asyncio.Task.cancel()` on the tracked task.
  - Module global `_TASKS: dict[str, asyncio.Task]` (replaces `_ACTIVE_ATTEMPTS`/`_CANCELLED_ATTEMPTS`).

- [ ] **Step 1: Rewrite the failing test file**

Replace the entire contents of `backend/tests/api/test_agent_stream.py`:

```python
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

    # run_agent_turn imports run_agent_stream into its own module namespace.
    import app.services.agent_runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_agent_stream", fake_stream)
    # Fresh bus per client so buffers/subscribers never leak across tests
    # (module-level singleton would be quietly shared across the whole file).
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


def test_watch_disconnect_does_not_stop_persistence(monkeypatch, tmp_path):
    # A watcher that never reads to the end (simulating browser refresh) must not
    # prevent the assistant reply from being persisted.
    #
    # `TestClient` runs the ASGI app's event loop on a background thread while this
    # test function executes synchronously on the main thread. asyncio.Event is not
    # thread-safe to .set() from a foreign thread (its waiters are plain futures
    # bound to the loop that created them) — use threading.Event and have the fake
    # stream await it via asyncio.to_thread so the wakeup crosses threads safely.
    import threading

    gate = threading.Event()

    async def slow_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "答案"})
        await asyncio.to_thread(gate.wait)  # hold the turn open past the watcher's early exit
        yield json.dumps({"type": "done"})

    client = _client(monkeypatch, tmp_path, fake_stream=slow_stream)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]
    _send(client, sid)

    # Open the watch stream, read only the first event, then abandon it.
    with client.stream("GET", f"/api/agent/sessions/{sid}/stream") as resp:
        next(resp.iter_lines())

    gate.set()  # let the (still-running) background turn finish
    # Poll persisted messages until the finally block writes the assistant reply.
    for _ in range(50):
        rows = client.get(f"/api/agent/sessions/{sid}/messages").json()["messages"]
        if any(r["role"] == "assistant" for r in rows):
            break
        import time
        time.sleep(0.05)
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
    import threading

    gate = threading.Event()

    async def slow_stream(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "delta", "content": "进行中"})
        await asyncio.to_thread(gate.wait)
        yield json.dumps({"type": "done"})

    client = _client(monkeypatch, tmp_path, fake_stream=slow_stream)
    sid = client.post("/api/agent/sessions", json={"title": ""}).json()["session_id"]

    first = _send(client, sid)
    assert first.status_code == 200

    second = _send(client, sid, content="second message while first still running")
    assert second.status_code == 409

    gate.set()
    # Drain the first attempt so the background task finishes before the test ends.
    with client.stream("GET", f"/api/agent/sessions/{sid}/stream") as resp:
        list(resp.iter_lines())

    rows = agent_sessions.read_messages(tmp_path, sid)
    # Only the first attempt's user message was persisted — the rejected second
    # send must not have appended anything.
    assert [r["content"] for r in rows if r["role"] == "user"] == ["hi"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_agent_stream.py -v`
Expected: FAIL — the send route `POST /sessions/{id}/messages` does not exist yet (404s), and `import ... agent_runner` / `get_bus` attribute wiring is not in place.

- [ ] **Step 3: Update imports and module globals**

In `backend/app/api/agent.py`, replace the import block (lines 1-18) and remove the now-unused `AgentStreamIn` model. New top of file:

```python
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import agent_tools
from app.services import agent_sessions
from app.services.agent_bus import get_bus
from app.services.agent_runner import run_agent_turn
from app.services.ai_provider import generate_ai_text

router = APIRouter(prefix="/api/agent", tags=["agent"])
_TASKS: dict[str, asyncio.Task] = {}
```

Then update the request model block: delete the `AgentStreamIn` class (old lines 26-29) and add an `AgentSendIn` model next to the others:

```python
class AgentSendIn(BaseModel):
    messages: list[dict]
    profile_id: str | None = None
```

- [ ] **Step 4: Rewrite the cancel endpoint**

Replace the existing `cancel_attempt` (old lines 85-90) with:

```python
@router.post("/attempts/{attempt_id}/cancel")
async def cancel_attempt(attempt_id: str) -> dict:
    task = _TASKS.get(attempt_id)
    if task is None or task.done():
        return {"cancelled": False}
    task.cancel()
    return {"cancelled": True}
```

**⚠️ Must be `async def`, not `def`.** FastAPI runs a plain `def` endpoint in a worker thread pool (`run_in_threadpool`), so `task.cancel()` would be called from a *different thread* than the one running the event loop that owns `task` — `asyncio.Task.cancel()` is not documented as thread-safe and must be called from the same thread as the loop (the safe cross-thread alternative is `loop.call_soon_threadsafe(task.cancel)`). Making the endpoint `async def` runs it directly on the event loop, so `task.cancel()` executes on the same thread that owns the task — no cross-thread call needed.

- [ ] **Step 5: Replace `POST /stream` with send + watch endpoints**

Delete the entire `chat_stream` function (old lines 128-181) and put in its place:

```python
@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: AgentSendIn, request: Request) -> dict:
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages empty")
    last = req.messages[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        raise HTTPException(status_code=400, detail="last message must be a user message with string content")

    data_dir = _data_dir(request)
    session = agent_sessions.get_session(data_dir, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    running_attempt_id = session.get("last_attempt_id")
    if session.get("last_attempt_status") == "running" and running_attempt_id:
        running_task = _TASKS.get(running_attempt_id)
        if running_task is not None and not running_task.done():
            raise HTTPException(status_code=409, detail="an attempt is already running for this session")

    stored = last.get("display_content")
    agent_sessions.append_message(
        data_dir,
        session_id,
        "user",
        stored if isinstance(stored, str) else last["content"],
    )

    attempt_id = f"agent_attempt_{uuid.uuid4().hex[:12]}"
    bus = get_bus()
    bus.begin(session_id)
    agent_sessions.set_attempt(data_dir, session_id, attempt_id, "running")

    task = asyncio.create_task(
        run_agent_turn(
            data_dir=data_dir,
            session_id=session_id,
            attempt_id=attempt_id,
            messages=req.messages,
            app_state=request.app.state,
            profile_id=req.profile_id,
            bus=bus,
        )
    )
    _TASKS[attempt_id] = task
    task.add_done_callback(lambda _t, aid=attempt_id: _TASKS.pop(aid, None))
    return {"attempt_id": attempt_id, "session_id": session_id}


@router.get("/sessions/{session_id}/stream")
async def watch_stream(session_id: str, request: Request):
    if agent_sessions.get_session(_data_dir(request), session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    bus = get_bus()

    async def gen():
        # Subscribing (and its cancellation on client disconnect) only affects
        # THIS read-only subscription — never the background run_agent_turn task.
        async for event in bus.subscribe(session_id):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_agent_stream.py tests/services/test_agent_loop.py -v`
Expected: PASS (all). `test_agent_loop.py` still passes because `run_agent_stream` itself is unchanged.

- [ ] **Step 7: Run the broader agent suite for regressions**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_agent.py tests/services/test_agent_bus.py tests/services/test_agent_runner.py tests/services/test_agent_sessions.py -v`
Expected: PASS. (`test_agent.py` exercises the untouched `POST /api/agent/chat`.)

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/agent.py backend/tests/api/test_agent_stream.py
git commit -m "feat(agent): decouple send from watch via background task + AgentBus endpoints"
```

---

### Task 5: Frontend API client — `agentSend` + `agentWatch`

**Files:**
- Modify: `frontend/src/lib/api.ts` (`AgentSession` interface lines 68-74; `agentStream` method lines 989-1032; the `cancelAgentAttempt`/session methods stay)

**Interfaces:**
- Consumes: existing `request`, `toast`, `AgentMsg`, `AgentEvent` types.
- Produces:
  - `AgentSession` gains `last_attempt_id?: string | null` and `last_attempt_status?: 'running' | 'done' | 'cancelled' | 'error' | null`.
  - `api.agentSend(sessionId: string, messages: AgentMsg[], profileId?: string): Promise<{ attempt_id: string; session_id: string }>`.
  - `api.agentWatch(sessionId: string, signal?: AbortSignal): AsyncGenerator<AgentEvent>` — GET ndjson reader.
  - `api.agentStream` is removed (all callers move to send + watch in Task 6).

- [ ] **Step 1: Extend the `AgentSession` type**

In `frontend/src/lib/api.ts`, edit the `AgentSession` interface (lines 68-74) to add the two fields:

```typescript
export interface AgentSession {
  session_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  last_attempt_id?: string | null
  last_attempt_status?: 'running' | 'done' | 'cancelled' | 'error' | null
}
```

- [ ] **Step 2: Replace `agentStream` with `agentSend` + `agentWatch`**

In `frontend/src/lib/api.ts`, replace the whole `async *agentStream(...) { ... }` method (lines 989-1032) with:

```typescript
  agentSend: (sessionId: string, messages: AgentMsg[], profileId?: string) =>
    request<{ attempt_id: string; session_id: string }>(
      `/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify({ messages, ...(profileId ? { profile_id: profileId } : {}) }),
      },
    ),

  async *agentWatch(sessionId: string, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}/stream`, {
      signal,
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) as AgentEvent } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) as AgentEvent } catch { /* ignore */ }
    }
  },
```

- [ ] **Step 3: Typecheck the client**

Run: `cd frontend && pnpm exec tsc -b`
Expected: FAIL — `src/pages/Agent.tsx` still references the removed `api.agentStream`. This is expected; Task 6 fixes the caller. Confirm the ONLY errors are in `Agent.tsx` (the api.ts file itself must compile clean).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(agent): replace agentStream with agentSend + agentWatch client methods"
```

---

### Task 6: Frontend Agent page — send/watch split + reconnect-on-mount

**Files:**
- Modify: `frontend/src/pages/Agent.tsx` (`sendPrompt` lines 254-335; initial-load `useEffect` lines 206-219; second URL-sync `useEffect` lines 221-226; mobile `<select>` `onChange` line 420; sidebar session button `onClick` line 480; add a shared reducer + `reconnect`/`openSession` helpers)

**Interfaces:**
- Consumes: `api.agentSend`, `api.agentWatch`, `api.cancelAgentAttempt`, `api.agentSessionMessages`, `api.agentSessions` (Task 5); `AgentSession.last_attempt_status`.
- Produces: no new exports — internal refactor. After this task `api.agentStream` has zero references, and every place that can navigate to an existing session (initial URL load, subsequent URL changes, the mobile session `<select>`, and the sidebar session list) goes through `openSession`, so all of them reconnect to a running attempt instead of only the very first page load.

**⚠️ Scope correction (panel 2 review, High-3, verified true):** the original draft of this task only patched the initial-mount `useEffect` (lines 206-219). The current `Agent.tsx` has *three more* places that call `loadSession` directly and were left untouched: the second `useEffect` that reacts to later `urlSessionId` changes without a full remount (lines 221-226), the mobile `<select>`'s `onChange` (line 420), and the sidebar's per-session button `onClick` (line 480). Switching to a session with a running attempt through any of those three would silently show stale/incomplete messages with no live update and no reconnect — the exact bug this plan exists to fix, just reachable through a different UI path. All four call sites must be unified through one `openSession` helper (Step 3 below).

- [ ] **Step 1: Extract the event reducer into a module-level helper**

In `frontend/src/pages/Agent.tsx`, add this pure helper just above `export function Agent()` (after the `MessageBubble`/`WelcomeScreen` components, ~line 180). It folds one `AgentEvent` into the last assistant message; it is reused by both `sendPrompt` and `reconnect`:

```typescript
function applyAgentEvent(prev: ChatMsg[], evt: AgentEvent, attemptIdRef: { current: string | null }): ChatMsg[] {
  const lastIdx = prev.length - 1
  const last = prev[lastIdx]
  if (last?.role !== 'assistant') return prev

  const nextLast: ChatMsg = { ...last, tools: last.tools ? [...last.tools] : [] }
  if (evt.type === 'attempt_start') {
    attemptIdRef.current = evt.attempt_id
  } else if (evt.type === 'delta') {
    nextLast.content += evt.content
  } else if (evt.type === 'tool_call') {
    nextLast.tools = [...(nextLast.tools ?? []), { name: evt.name, args: evt.args }]
  } else if (evt.type === 'tool_result') {
    const tools = [...(nextLast.tools ?? [])]
    let idx = -1
    for (let k = tools.length - 1; k >= 0; k--) {
      if (tools[k].name === evt.name && tools[k].result === undefined) { idx = k; break }
    }
    if (idx >= 0) tools[idx] = { ...tools[idx], result: evt.result }
    nextLast.tools = tools
  } else if (evt.type === 'error') {
    nextLast.content += `\n[错误] ${evt.message}`
  } else if (evt.type === 'cancelled') {
    nextLast.content += nextLast.content ? '\n[已停止]' : '[已停止]'
  }
  const next = [...prev]
  next[lastIdx] = nextLast
  return next
}
```

- [ ] **Step 2: Rewrite `sendPrompt` to send-then-watch**

Replace the body of `sendPrompt` from the `setStreaming(true)` line through the end of the `try` block (currently lines 277-313) so it calls `agentSend` then iterates `agentWatch`. The full replacement for `sendPrompt` (keep the function signature and the pre-send session/history setup at lines 254-276 unchanged, replace from `setStreaming(true)` onward):

```typescript
    setStreaming(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const { attempt_id } = await api.agentSend(activeSessionId, history, profileId)
      attemptIdRef.current = attempt_id
      for await (const evt of api.agentWatch(activeSessionId, ctrl.signal)) {
        setMsgs(prev => applyAgentEvent(prev, evt, attemptIdRef))
      }
      void refreshSessions(activeSessionId)
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') last.content += last.content ? '\n[已停止]' : '[已停止]'
          return next
        })
        return
      }
      setMsgs(prev => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.role === 'assistant') last.content += `\n[请求失败] ${(e as Error).message}`
        return next
      })
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null
      attemptIdRef.current = null
      setStreaming(false)
    }
```

- [ ] **Step 3: Add a `reconnect` helper**

In `frontend/src/pages/Agent.tsx`, add this function next to `loadSession` (after `loadSession`, ~line 246). It loads persisted messages and watches (replaying) an in-flight attempt.

**⚠️ Fix (panel 2 review, High-4, verified true):** the original draft unconditionally appended an empty assistant bubble before watching. But `AgentBus.subscribe` (Task 1) returns immediately with zero events when the session is unknown to the bus — which happens whenever `last_attempt_status` is stale-`"running"` on disk with no live task behind it (e.g. the backend process restarted since the attempt was launched; see the Out-of-scope note). In that case the old draft left a permanent empty `(无回复)` bubble at the end of the conversation that nothing ever cleaned up, directly contradicting this plan's own claim (Out-of-scope note) that a stale attempt just "shows the persisted messages." The fix: only insert the assistant bubble lazily, on the *first* event actually received. If zero events arrive, `msgs` is left exactly as loaded from disk — no phantom bubble.

```typescript
  async function reconnect(id: string) {
    const { messages } = await api.agentSessionMessages(id)
    setSessionId(id)
    setSessionInUrl(id, true)
    setMsgs(messages.map(m => ({ role: m.role, content: m.content })))
    setStreaming(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let bubbleAdded = false
    try {
      for await (const evt of api.agentWatch(id, ctrl.signal)) {
        if (!bubbleAdded) {
          bubbleAdded = true
          setMsgs(prev => [...prev, { role: 'assistant', content: '', tools: [] }])
        }
        setMsgs(prev => applyAgentEvent(prev, evt, attemptIdRef))
      }
      // Whether or not the bus had anything to replay (stale attempt vs. a real
      // in-flight one), refresh the session list so a stale "running" status
      // that this reconnect found empty doesn't keep looking live in the sidebar.
      void refreshSessions(id)
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      if (bubbleAdded) {
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') last.content += `\n[请求失败] ${(e as Error).message}`
          return next
        })
      }
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null
      attemptIdRef.current = null
      setStreaming(false)
    }
  }
```

**Known residual limitation (accepted, matches this plan's Out-of-scope stance — not fixed here):** the backend has no startup reconciliation for a crashed process's dangling `last_attempt_status == "running"` (see Out-of-scope note). This fix stops it from producing a visible dangling bubble, but every subsequent page load for that session will still call `reconnect` and get an empty replay (a harmless no-op extra request) until the user sends a new message, which starts a fresh attempt and overwrites the stale status. Do not attempt backend reconciliation in this task — out of scope.

- [ ] **Step 4: Add a shared `openSession` dispatcher and use it at every session-switch call site**

**⚠️ Scope addition (panel 2 review, High-3, verified true):** `reconnect` must be reachable from every place the user can land on an existing session, not just the very first page load. Add this helper next to `reconnect` (it reads the already-loaded `sessions` state, so it must NOT be used inside the initial-load effect itself — that effect gets a fresher `rows` array directly from the `api.agentSessions()` call and must check that instead, see Step 5):

```typescript
  function openSession(id: string, replaceUrl = false) {
    const match = sessions.find(s => s.session_id === id)
    if (match?.last_attempt_status === 'running') {
      void reconnect(id)
    } else {
      void loadSession(id, replaceUrl)
    }
  }
```

Then replace every other direct `loadSession` call with `openSession`:

1. The second `useEffect` that reacts to later `urlSessionId` changes (currently lines 221-226) — replace its body:
   ```typescript
   useEffect(() => {
     if (!urlSessionId || urlSessionId === sessionId) return
     if (!sessions.some(s => s.session_id === urlSessionId)) return
     openSession(urlSessionId, true)
     // eslint-disable-next-line react-hooks/exhaustive-deps
   }, [urlSessionId, sessions, sessionId])
   ```
2. The mobile `<select>`'s `onChange` (currently line 420, `if (e.target.value) void loadSession(e.target.value)`) — replace with:
   ```typescript
   onChange={e => {
     if (e.target.value) openSession(e.target.value)
     else clear()
   }}
   ```
3. The sidebar's per-session button `onClick` (currently line 480, `onClick={() => void loadSession(s.session_id)}`) — replace with:
   ```typescript
   onClick={() => openSession(s.session_id)}
   ```

- [ ] **Step 5: Trigger reconnect from the initial-load effect**

In `frontend/src/pages/Agent.tsx`, replace the initial-load `useEffect` (lines 206-219) so that when the URL session's `last_attempt_status` is `'running'` it reconnects instead of just loading:

```typescript
  useEffect(() => {
    api.agentSessions()
      .then(({ sessions: rows }) => {
        setSessions(rows)
        if (!urlSessionId) return
        const match = rows.find(s => s.session_id === urlSessionId)
        if (!match) {
          setSearchParams({}, { replace: true })
        } else if (match.last_attempt_status === 'running') {
          void reconnect(urlSessionId)
        } else {
          void loadSession(urlSessionId, true)
        }
      })
      .catch(() => setSessions([]))
    // 初次进入时恢复 URL session（含在跑 attempt 的重连）；之后由显式切换动作维护。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
```

- [ ] **Step 6: Typecheck and lint**

Run: `cd frontend && pnpm exec tsc -b && pnpm exec eslint src/pages/Agent.tsx src/lib/api.ts`
Expected: PASS — no type errors, no `agentStream` references remain. If eslint flags the `reconnect`/`openSession` reference inside an effect as a missing dep, keep the existing `eslint-disable-next-line react-hooks/exhaustive-deps` already present on that effect.

- [ ] **Step 7: Confirm no stale references**

Run: `cd frontend && grep -rn "agentStream" src/`
Expected: no output (empty). If anything prints, update that caller to `agentSend`/`agentWatch`.

- [ ] **Step 8: Manual verification (data-loss + reconnect, all 4 entry points)**

Start backend + frontend, then:

```bash
# Terminal A
cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000
# Terminal B
cd frontend && pnpm dev
```

Verify each scenario:
1. **Happy path:** open `/agent`, send a message, watch the streamed reply, confirm it renders and the session appears in the sidebar with the reply after a reload.
2. **Data-loss fix, reload:** send a message, and mid-reply refresh the browser tab (the URL still carries `?session=...`). Reopen the same session — the assistant's reply for that turn is present (previously it vanished). If the turn is still running when you reload, the page reconnects and continues streaming with no dangling empty bubble.
3. **Reconnect via the other 3 entry points (panel 2 High-3 fix):** start a message in session A, then — without waiting for it to finish — switch to session B and back to session A using (a) the desktop sidebar button, (b) the mobile `<select>` dropdown (narrow viewport), and (c) editing the `?session=` URL param directly while the page is already mounted. All three must show the live/partial reply catching up (via `reconnect`), not a stale/empty view.
4. **Concurrent send rejected (panel 2 High-2 fix):** send a message, then — while it's still streaming — try to send a second message in the same session (e.g. by typing and pressing Enter again quickly, or via two browser tabs on the same session). The second send must fail (409) rather than starting a second overlapping turn; confirm only one assistant reply is appended.
5. **Cancel:** send a message, click 停止. Confirm the reply ends with `[已停止]` and the persisted message reflects the partial text.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/pages/Agent.tsx
git commit -m "feat(agent): send-then-watch flow with reconnect for in-flight turns"
```

---

## Self-Review

**1. Spec coverage:**
- *Decouple send from execution* → Task 4 `send_message` persists user msg + `asyncio.create_task(run_agent_turn)` and returns `attempt_id` without awaiting the turn. ✅
- *Turn runs to completion + persists in unconditional finally (the data-loss fix)* → Task 3 `run_agent_turn` finally block + tests `test_turn_persists_even_with_no_subscriber` and `test_turn_cancelled_midstream_still_persists_partial`. ✅
- *In-memory pub/sub with ring buffer, fan-out to N subscribers* → Task 1 `AgentBus` (bounded 500, multi-subscriber). ✅
- *Separate watch endpoint; disconnect must not cancel the turn* → Task 4 `GET /sessions/{id}/stream` (subscription-only); proven by `test_watch_disconnect_does_not_stop_persistence` and the unit-level `test_turn_persists_even_with_no_subscriber`. Root-cause (Starlette spec_version race) is confined to the watch generator, never the turn task. ✅
- *Reconnect/replay after reload* → Task 2 persists `last_attempt_status`; Task 1 `subscribe` replays the buffer; Task 6 `reconnect` triggers on `last_attempt_status === 'running'`. ✅
- *Cancel now cancels the real task* → Task 4 `cancel_attempt` calls `task.cancel()`; `run_agent_turn` catches `CancelledError`. ✅
- *Keep ndjson, keep `/api/agent/sessions/*` REST surface* → watch is ndjson; session CRUD routes unchanged (`test_agent_sessions_crud` retained). ✅
- *Keep file-based JSON persistence, no DB* → only `agent_sessions.py` JSON used. ✅
- *TDD matching existing conventions* → every backend code step has a preceding failing-test step using inline `FastAPI()`+`TestClient`/`async def` (auto mode). Frontend gated by `tsc`+lint+manual (no test runner exists). ✅
- *Out-of-scope items noted* → tool-call trace persistence and restart-durable deltas explicitly deferred. ✅

**2. Placeholder scan:** No `TBD`/`TODO`/"add error handling"/"similar to Task N". Every code step contains complete code. ✅

**3. Type consistency:** `run_agent_turn(*, data_dir, session_id, attempt_id, messages, app_state, profile_id, bus)` keyword signature is identical in Task 3 (def), Task 3 tests, and Task 4 (call site). `AgentBus.begin/publish/close/subscribe` names match across Tasks 1, 3, 4. `set_attempt(...4 args)` vs `set_attempt_status(...3 args)` used consistently in Tasks 2/3/4. `_TASKS: dict[str, asyncio.Task]` and `attempt_id` naming consistent. Frontend `agentSend`/`agentWatch`/`applyAgentEvent`/`reconnect` names match across Tasks 5/6. `last_attempt_status` literal set `'running'|'done'|'cancelled'|'error'` matches the backend `status` values written by `run_agent_turn`. ✅

**Design decisions resolved without asking:**
- **Hybrid vs pure-immediate-return, chose explicit send→watch split** (send returns `{attempt_id}`, watch is a separate GET). Reasoning: matches the reference project's shape exactly, is the only design that also delivers reconnect (needed regardless), and confines the Starlette disconnect-cancellation race to a read-only subscription. Cost is one extra round trip per send, which is negligible.
- **Send endpoint = `POST /sessions/{id}/messages` (not a modified `POST /stream`).** Reasoning: RESTful (mirrors the existing `GET .../messages`), keeps the `/api/agent/sessions/*` surface coherent, and the old racy `/stream` is deleted rather than left as a foot-gun. The frontend already always creates a session before sending, so requiring a session id breaks nothing.
- **Always replay the current attempt's buffer on subscribe** (rather than porting Vibe-Trading's `Last-Event-ID`/`replay_all` distinction). Reasoning: eliminates the send→watch race window with zero client bookkeeping; a turn is short and its buffer is small, so full replay is cheap. Seq-based watermark de-dups live events.
- **`task.cancel()` as the cancel mechanism** (dropping the old flag-polling `_CANCELLED_ATTEMPTS`). Reasoning: it actually interrupts the in-flight LLM `await`, and `run_agent_turn`'s `except CancelledError` still persists the partial reply — cooperative flags could not interrupt a blocked network read.
- **Frontend verified via `tsc`/eslint/manual, not unit tests.** Reasoning: `frontend/package.json` has no test runner and adding vitest is out of scope and risks colliding with the off-limits three-locks `*.test.ts`.

**4. Panel 2's review (2026-07-05, verdict "Request Changes"): 4 High + 3 Medium + 2 Low, every one verified true against the actual plan text — no false positives — all fixed:**
- High-1: `cancel_attempt` was a plain `def`, so FastAPI runs it in a thread-pool thread; calling `task.cancel()` cross-thread onto the event-loop-owning thread is not a documented-safe `asyncio` operation → changed to `async def` so it executes on the same thread/loop as the task.
- High-2: `send_message` launched a new background turn unconditionally, with no check for an already-running attempt on the same session — two rapid sends could run two turns concurrently, both writing to the same bus channel and the same session's message/status fields → added a busy-check (`last_attempt_status == "running"` and `_TASKS` still has a live task for that attempt) returning 409, plus a new test.
- High-3: the reconnect fix only patched the initial-mount effect; three more `loadSession` call sites (the later-`urlSessionId` effect, the mobile `<select>`, and the sidebar button) were untouched and would silently show stale/incomplete state for a running attempt reached through them → added a shared `openSession` dispatcher and rewired all three, plus a new manual-verification scenario.
- High-4: `reconnect` unconditionally appended an empty assistant bubble before knowing whether the watch stream had anything to replay; for a stale `last_attempt_status == "running"` with no live bus channel (e.g. after a backend restart), this left a permanent empty `(无回复)` bubble, contradicting this plan's own Out-of-scope claim that a stale attempt "just shows the persisted messages" → the bubble is now only inserted lazily on the first event actually received.
- Medium-1: no explicit single-process assumption was stated even though `_TASKS`/`AgentBus` are in-process state → added to Global Constraints.
- Medium-2: `test_watch_disconnect_does_not_stop_persistence` used `asyncio.Event()` set from the test's own thread while the fake stream awaited it inside the ASGI app's separate event-loop thread — not a thread-safe wakeup → switched to `threading.Event()` + `await asyncio.to_thread(gate.wait)`.
- Medium-3: the "breaking change" reasoning for deleting `POST /stream` existed only inside the buried "Design decisions resolved without asking" section → promoted to an explicit line in Global Constraints.
- Low-1: the test file's comment claimed "each test gets an isolated bus" but `_BUS = AgentBus()` was a module-level singleton shared by every test → moved inside `_client()`, and also reset `agent_api._TASKS` per client so the new busy-check (High-2) can't leak state across tests either.
- Low-2: `send_message` silently accepted a non-`"user"`-role last message, launching a turn without persisting any user message → added an explicit 400 check plus a test.
