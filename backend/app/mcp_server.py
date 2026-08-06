from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from app.data_providers.capability_gate import detect_capabilities
from app.data_providers.registry import close_all_providers
from app.services.agent_tools import TOOLS, call_tool
from app.services.screener import ScreenerService
from app.services.screener import close_screener_sql_connection
from app.storage.repository import DataStore, KlineRepository
from app.strategy.engine import StrategyEngine


logger = logging.getLogger(__name__)


def _run_close_step(name: str, callback) -> None:
    try:
        callback()
    except Exception:  # noqa: BLE001
        logger.exception("MCP close step failed: %s", name)


def build_state():
    store = DataStore()
    try:
        repo = KlineRepository(store)
        repo.refresh_cache()
        screener = ScreenerService(repo)
        strategy_engine = StrategyEngine(
            enriched_loader=screener._load_enriched_for_date,
            enriched_history_loader=screener._load_enriched_history,
            strategy_dirs=[
                Path(__file__).resolve().parent / "strategy" / "builtin",
                store.data_dir / "strategies" / "custom",
                store.data_dir / "strategies" / "ai",
            ],
        )
        return SimpleNamespace(
            repo=repo,
            datastore=store,
            capabilities=detect_capabilities(),
            strategy_engine=strategy_engine,
            quote_service=None,
            depth_service=None,
        )
    except Exception:
        _run_close_step("data providers", close_all_providers)
        _run_close_step("screener SQL connection", close_screener_sql_connection)
        _run_close_step("data store", store.close)
        raise


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
    try:
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
    finally:
        _run_close_step("data providers", close_all_providers)
        _run_close_step("screener SQL connection", close_screener_sql_connection)
        _run_close_step("data store", state.datastore.close)


if __name__ == "__main__":
    raise SystemExit(main())
