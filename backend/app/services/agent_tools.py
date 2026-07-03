from __future__ import annotations

from typing import Any


TOOLS = [
    {
        "name": "get_capabilities",
        "description": "Return current data/provider capability labels.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_strategies",
        "description": "Return available screener strategy ids and names.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def call_tool(name: str, app_state: Any, args: dict | None = None) -> dict:
    args = args or {}
    if name == "get_capabilities":
        capset = getattr(app_state, "capabilities", None)
        return {"capabilities": sorted(capset.all()) if capset else []}
    if name == "list_strategies":
        engine = getattr(app_state, "strategy_engine", None)
        rows = []
        if engine is not None:
            for item in engine.list_strategies():
                rows.append({
                    "id": item.id,
                    "name": item.name,
                    "source": item.source,
                    "tags": item.tags,
                })
        return {"strategies": rows[:200]}
    raise ValueError(f"unknown agent tool: {name}")
