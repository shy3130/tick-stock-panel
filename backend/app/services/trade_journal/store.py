"""Trade Journal ledger persistence."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def ledger_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trade_journal" / "ledger.json"


def read_ledger(data_dir: Path) -> dict[str, Any] | None:
    path = ledger_path(data_dir)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else None


def write_ledger(data_dir: Path, payload: dict[str, Any]) -> None:
    path = ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")


def delete_ledger(data_dir: Path) -> bool:
    path = ledger_path(data_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
