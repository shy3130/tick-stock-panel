"""Trade Journal ledger persistence."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_JOURNAL_LOCK = threading.RLock()


def ledger_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "ledger.json"


def source_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "source.json"


def journal_state_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "state.json"


def feedback_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "feedback.jsonl"


def read_ledger(data_dir: Path) -> dict[str, Any] | None:
    state = _read_journal_state(data_dir)
    if state is not None:
        return state["ledger"]
    return _read_json(ledger_path(data_dir))


def read_source(data_dir: Path) -> dict[str, Any] | None:
    state = _read_journal_state(data_dir)
    if state is not None:
        return state["source"]
    return _read_json(source_path(data_dir))


def write_ledger(data_dir: Path, payload: dict[str, Any]) -> None:
    state = _read_journal_state(data_dir)
    if state is None:
        _atomic_write_text(ledger_path(data_dir), _json_text(payload))
        return
    _atomic_write_text(
        journal_state_path(data_dir),
        _json_text({"version": 1, "source": state["source"], "ledger": payload}),
    )


def write_source(data_dir: Path, payload: dict[str, Any]) -> None:
    state = _read_journal_state(data_dir)
    if state is None:
        _atomic_write_text(source_path(data_dir), _json_text(payload))
        return
    _atomic_write_text(
        journal_state_path(data_dir),
        _json_text({"version": 1, "source": payload, "ledger": state["ledger"]}),
    )


def write_journal(data_dir: Path, source: dict[str, Any], ledger: dict[str, Any]) -> None:
    """Atomically commit the canonical source and its derived ledger as one state document."""
    _atomic_write_text(
        journal_state_path(data_dir),
        _json_text({"version": 1, "source": source, "ledger": ledger}),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else None


def _read_journal_state(data_dir: Path) -> dict[str, Any] | None:
    state = _read_json(journal_state_path(data_dir))
    if state is None:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("source"), dict):
        raise ValueError("invalid trade journal state")
    if not isinstance(state.get("ledger"), dict):
        raise ValueError("invalid trade journal state")
    return state


@contextmanager
def journal_write_lock(data_dir: Path) -> Iterator[None]:
    """Serialize journal source/ledger mutations across threads and workers."""
    path = data_dir / "user_data" / "trade_journal" / ".write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_LOCK, path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default, allow_nan=False)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def append_feedback(data_dir: Path, entry: dict[str, Any]) -> None:
    path = feedback_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")


def read_feedback(data_dir: Path) -> list[dict[str, Any]]:
    path = feedback_path(data_dir)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def delete_ledger(data_dir: Path) -> bool:
    with journal_write_lock(data_dir):
        paths = (journal_state_path(data_dir), ledger_path(data_dir), source_path(data_dir))
        deleted = False
        for path in paths:
            if path.exists():
                path.unlink()
                deleted = True
        return deleted


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
