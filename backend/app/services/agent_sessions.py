from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _root(data_dir: Path) -> Path:
    path = data_dir / "user_data" / "agent_sessions"
    path.mkdir(parents=True, exist_ok=True)
    (path / "messages").mkdir(exist_ok=True)
    return path


def _index_path(data_dir: Path) -> Path:
    return _root(data_dir) / "sessions.json"


def _messages_path(data_dir: Path, session_id: str) -> Path:
    return _root(data_dir) / "messages" / f"{session_id}.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_sessions(data_dir: Path) -> list[dict[str, Any]]:
    sessions = _read_json(_index_path(data_dir), [])
    if not isinstance(sessions, list):
        return []
    return sorted(sessions, key=lambda s: str(s.get("updated_at") or ""), reverse=True)


def create_session(data_dir: Path, title: str | None = None) -> dict[str, Any]:
    ts = _now()
    item = {
        "session_id": f"agent_{uuid.uuid4().hex[:12]}",
        "title": (title or "新对话").strip()[:80] or "新对话",
        "created_at": ts,
        "updated_at": ts,
        "message_count": 0,
        "last_attempt_id": None,
        "last_attempt_status": None,
    }
    sessions = list_sessions(data_dir)
    sessions.insert(0, item)
    _write_json(_index_path(data_dir), sessions)
    _write_json(_messages_path(data_dir, item["session_id"]), [])
    return item


def get_session(data_dir: Path, session_id: str) -> dict[str, Any] | None:
    return next((s for s in list_sessions(data_dir) if s.get("session_id") == session_id), None)


def rename_session(data_dir: Path, session_id: str, title: str) -> dict[str, Any] | None:
    sessions = list_sessions(data_dir)
    updated: dict[str, Any] | None = None
    for item in sessions:
        if item.get("session_id") == session_id:
            item["title"] = title.strip()[:80] or item.get("title") or "新对话"
            item["updated_at"] = _now()
            updated = item
            break
    if updated is not None:
        _write_json(_index_path(data_dir), sessions)
    return updated


def delete_session(data_dir: Path, session_id: str) -> bool:
    sessions = list_sessions(data_dir)
    kept = [s for s in sessions if s.get("session_id") != session_id]
    if len(kept) == len(sessions):
        return False
    _write_json(_index_path(data_dir), kept)
    try:
        _messages_path(data_dir, session_id).unlink()
    except FileNotFoundError:
        pass
    return True


def read_messages(data_dir: Path, session_id: str) -> list[dict[str, Any]]:
    rows = _read_json(_messages_path(data_dir, session_id), [])
    return rows if isinstance(rows, list) else []


def append_message(
    data_dir: Path,
    session_id: str,
    role: str,
    content: str,
    *,
    tool_traces: list[dict[str, Any]] | None = None,
    elapsed_ms: float | None = None,
) -> dict[str, Any] | None:
    if get_session(data_dir, session_id) is None:
        return None
    row = {
        "message_id": f"msg_{uuid.uuid4().hex[:12]}",
        "role": role,
        "content": content,
        "created_at": _now(),
    }
    if tool_traces:
        row["tool_traces"] = tool_traces
        if elapsed_ms is not None:
            row["elapsed_ms"] = elapsed_ms
    rows = read_messages(data_dir, session_id)
    rows.append(row)
    _write_json(_messages_path(data_dir, session_id), rows)
    sessions = list_sessions(data_dir)
    for item in sessions:
        if item.get("session_id") == session_id:
            item["updated_at"] = row["created_at"]
            item["message_count"] = len(rows)
            if role == "user" and (not item.get("title") or item.get("title") == "新对话"):
                item["title"] = content.strip()[:50] or "新对话"
            break
    _write_json(_index_path(data_dir), sessions)
    return row


def set_attempt(data_dir: Path, session_id: str, attempt_id: str, status: str) -> None:
    sessions = list_sessions(data_dir)
    for item in sessions:
        if item.get("session_id") == session_id:
            item["last_attempt_id"] = attempt_id
            item["last_attempt_status"] = status
            item["updated_at"] = _now()
            _write_json(_index_path(data_dir), sessions)
            return


def set_attempt_status(data_dir: Path, session_id: str, status: str) -> None:
    sessions = list_sessions(data_dir)
    for item in sessions:
        if item.get("session_id") == session_id:
            item["last_attempt_status"] = status
            item["updated_at"] = _now()
            _write_json(_index_path(data_dir), sessions)
            return
