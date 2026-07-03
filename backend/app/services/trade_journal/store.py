"""Trade Journal ledger persistence."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def ledger_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "ledger.json"


def source_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "source.json"


def feedback_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "feedback.jsonl"


def read_ledger(data_dir: Path) -> dict[str, Any] | None:
    path = ledger_path(data_dir)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else None


def read_source(data_dir: Path) -> dict[str, Any] | None:
    path = source_path(data_dir)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else None


def write_ledger(data_dir: Path, payload: dict[str, Any]) -> None:
    path = ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")


def write_source(data_dir: Path, payload: dict[str, Any]) -> None:
    path = source_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")


def append_feedback(data_dir: Path, entry: dict[str, Any]) -> None:
    path = feedback_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")


def read_feedback(data_dir: Path) -> list[dict[str, Any]]:
    path = feedback_path(data_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def delete_ledger(data_dir: Path) -> bool:
    path = ledger_path(data_dir)
    source = source_path(data_dir)
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    if source.exists():
        source.unlink()
        deleted = True
    return deleted


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
