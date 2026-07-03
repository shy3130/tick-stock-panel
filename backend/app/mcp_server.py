from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from app.data_providers.capability_gate import detect_capabilities
from app.services.agent_tools import TOOLS, call_tool
from app.storage.repository import DataStore, KlineRepository


def build_state():
    store = DataStore()
    return SimpleNamespace(repo=KlineRepository(store), capabilities=detect_capabilities(), strategy_engine=None)


def handle_message(msg: dict, state) -> dict:
    mid = msg.get("id")
    method = msg.get("method")
    if method == "tools/list":
        return {"id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name"), state, params.get("arguments") or {})
            return {"id": mid, "result": result}
        except Exception as e:  # noqa: BLE001
            return {"id": mid, "error": {"message": str(e)}}
    return {"id": mid, "error": {"message": f"unknown method: {method}"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    state = build_state()
    if args.self_test:
        listed = handle_message({"id": 1, "method": "tools/list"}, state)
        called = handle_message({"id": 2, "method": "tools/call", "params": {"name": "get_capabilities", "arguments": {}}}, state)
        return 0 if listed.get("result") and called.get("result") is not None else 1
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            out = handle_message(msg, state)
        except Exception as e:  # noqa: BLE001
            out = {"id": None, "error": {"message": str(e)}}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
