from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from app.services import agent_sessions, agent_tools
from app.services.agent_bus import AgentBus
from app.services.agent_runtime import run_agent_stream
from app.services.ai_structured.models import CancellationToken


async def run_agent_turn(
    *,
    data_dir: Path,
    session_id: str,
    attempt_id: str,
    messages: list[dict],
    app_state: Any,
    profile_id: str | None,
    bus: AgentBus,
    token: CancellationToken | None = None,
) -> None:
    token = token or CancellationToken()
    assistant_chunks: list[str] = []
    tool_traces: list[dict[str, Any]] = []
    status = "done"
    started_at = perf_counter()
    elapsed_ms: float | None = None
    received_terminal = False
    bus.publish(
        session_id,
        {"type": "attempt_start", "attempt_id": attempt_id, "session_id": session_id},
    )
    bus.publish(
        session_id,
        {"type": "attempt_started", "attempt_id": attempt_id, "session_id": session_id},
    )
    try:
        token.raise_if_cancelled()
        async for line in run_agent_stream(messages, app_state, profile_id):
            token.raise_if_cancelled()
            try:
                event = json.loads(line)
            except Exception:
                event = {}
            event_type = event.get("type")
            if event_type == "tool_call" and isinstance(event.get("name"), str):
                tool_traces.append({"name": event["name"], "args": event.get("args", {})})
            elif event_type == "tool_result" and isinstance(event.get("name"), str):
                trace = next(
                    (
                        item
                        for item in reversed(tool_traces)
                        if item["name"] == event["name"] and "result" not in item
                    ),
                    None,
                )
                if trace is None:
                    trace = {"name": event["name"]}
                    tool_traces.append(trace)
                trace["result"] = event.get("result")
                if isinstance(event.get("elapsed_ms"), (int, float)):
                    trace["elapsed_ms"] = event["elapsed_ms"]
            if event_type in {"done", "error"}:
                received_terminal = True
                if isinstance(event.get("elapsed_ms"), (int, float)):
                    elapsed_ms = event["elapsed_ms"]
            if event_type == "delta" and isinstance(event.get("content"), str):
                assistant_chunks.append(event["content"])
            elif event_type == "error" and isinstance(event.get("message"), str):
                assistant_chunks.append(f"[错误] {event['message']}")
                status = "error"
            bus.publish(session_id, event)
        if not received_terminal:
            status = "error"
            message = "Agent 流式响应在完成前中断，请重试"  # noqa: RUF001
            assistant_chunks.append(
                f"\n[错误] {message}" if assistant_chunks else f"[错误] {message}"
            )
            bus.publish(
                session_id,
                {"type": "error", "code": "ai_provider_error", "message": message},
            )
        terminal_type = "attempt_failed" if status == "error" else "attempt_completed"
        bus.publish(session_id, {"type": terminal_type, "attempt_id": attempt_id})
    except asyncio.CancelledError:
        status = "cancelled"
        assistant_chunks.append("\n[已停止]" if assistant_chunks else "[已停止]")
        bus.publish(session_id, {"type": "cancelled", "attempt_id": attempt_id})
        bus.publish(session_id, {"type": "attempt_cancelled", "attempt_id": attempt_id})
        raise
    except Exception as exc:
        status = "error"
        message = agent_tools.sanitize_tool_error(exc)
        assistant_chunks.append(f"\n[错误] {message}")
        bus.publish(session_id, {"type": "error", "message": message})
        bus.publish(
            session_id,
            {"type": "attempt_failed", "attempt_id": attempt_id, "message": message},
        )
        raise
    finally:
        if assistant_chunks:
            agent_sessions.append_message(
                data_dir,
                session_id,
                "assistant",
                "".join(assistant_chunks),
                tool_traces=tool_traces or None,
                elapsed_ms=(
                    elapsed_ms
                    if elapsed_ms is not None
                    else round((perf_counter() - started_at) * 1000, 1)
                ),
            )
        agent_sessions.set_attempt_status(data_dir, session_id, status)
        bus.close(session_id)
