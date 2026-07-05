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
    except Exception as exc:  # noqa: BLE001
        status = "error"
        assistant_chunks.append(f"\n[错误] {exc}")
        bus.publish(session_id, {"type": "error", "message": str(exc)})
    finally:
        if assistant_chunks:
            agent_sessions.append_message(data_dir, session_id, "assistant", "".join(assistant_chunks))
        agent_sessions.set_attempt_status(data_dir, session_id, status)
        bus.close(session_id)
