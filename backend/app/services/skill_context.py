from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "docs" / "skills"


def load_skill_context(scenario: str, max_chars: int = 12_000) -> str:
    entries = _load_index()
    chunks = []
    total = 0
    for item in entries:
        if scenario not in item.get("scenarios", []):
            continue
        path = _safe_path(str(item.get("path", "")))
        text = path.read_text(encoding="utf-8")
        per_doc = min(int(item.get("max_chars") or max_chars), max_chars - total)
        if per_doc <= 0:
            break
        chunks.append(text[:per_doc])
        total += len(chunks[-1])
        if total >= max_chars:
            break
    if not chunks:
        return ""
    return "以下为本地方法论，不是实时数据：\n\n" + "\n\n---\n\n".join(chunks)


def _load_index() -> list[dict]:
    path = ROOT / "index.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_path(rel: str) -> Path:
    path = (ROOT / rel).resolve()
    if ROOT.resolve() not in path.parents or path.suffix.lower() != ".md":
        raise ValueError("invalid skill path")
    return path
