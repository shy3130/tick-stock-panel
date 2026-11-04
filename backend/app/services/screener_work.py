"""Input identities for shared screening; no vendor or UI-specific computation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from app.definitions import JsonDefinitionStore
from app.services import daily_health
from app.services.shared_work import SharedWork, WorkBusyError
from app.strategy import config as strategy_config

_DEFINITIONS = (
    JsonDefinitionStore("user_data/custom_signals", label="signal"),
    JsonDefinitionStore("user_data/custom_factors", label="factor"),
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def hard_identity(repo, engine, asset_type: str) -> tuple:
    root = Path(repo.store.data_dir)
    return (
        str(root.resolve()), id(engine), id(getattr(engine, "_strategies", None)),
        getattr(engine, "definition_revision", 0),
        daily_health.revision(root, asset_type),
        tuple(store.fingerprint(root) for store in _DEFINITIONS),
        digest(strategy_config.list_overrides(root)),
    )


def source_version(repo, asset_type: str, timeframe: str, as_of: date) -> object:
    daily_health.assert_usable(Path(repo.store.data_dir), asset_type, end=as_of)
    if timeframe == "1d":
        getter = getattr(repo, "get_enriched_generation_snapshot", None)
        if callable(getter):
            return getter(asset_type).through(as_of)
    folder = {"stock": "kline_minute", "etf": "kline_etf_minute", "index": "kline_index_minute"}[asset_type]
    partition = Path(repo.store.data_dir) / folder / f"date={as_of}"
    return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(partition.glob("*.parquet")))


def assert_current(repo, engine, asset_type: str, expected: tuple) -> None:
    if hard_identity(repo, engine, asset_type) != expected:
        raise WorkBusyError("策略定义、参数或日K修补状态已变化，请重新计算")


def identity_for_inputs(repo, engine, asset_type: str, overrides_map: dict) -> tuple:
    identity = hard_identity(repo, engine, asset_type)
    current = strategy_config.list_overrides(Path(repo.store.data_dir))
    if any((current.get(sid) or {}) != (value or {}) for sid, value in overrides_map.items()):
        raise WorkBusyError("策略参数已变化，请重新计算")
    return identity


single_work = SharedWork("strategy.single", max_entries=32,
                         size_of=lambda value: len(json.dumps(asdict(value), default=str).encode()))
custom_work = SharedWork("screener.custom")
full_batch_work = SharedWork("strategy.batch", max_pending=32, max_entries=8,
                            size_of=lambda values: len(json.dumps({k: asdict(v) for k, v in values.items()}, default=str).encode()))
# Full rows are only kept briefly for automatic overlapping batches; manual runs bypass them.
batch_results = SharedWork("strategy.unit", size_of=lambda value: len(json.dumps(value, default=str).encode()))
