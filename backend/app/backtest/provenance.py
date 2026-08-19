"""回测数据、股票池和指标口径的可复现元数据。"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from app.backtest.metrics import MetricContext

ENGINE_VERSION = "polars-numpy-v1"


def build_data_snapshot(
    repo: Any,
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    adjustment_mode: str = "qfq",
) -> tuple[dict, list[str]]:
    """只读当前已发布 generation，生成可比较的回测数据快照描述。"""
    from app.services.canonical_history import resolve_published_history

    published = resolve_published_history(getattr(repo, "_external_enriched_root", None))
    manifest = published[0] if published is not None else {}
    source_generations = dict(manifest.get("source_generations") or {})

    local_latest_value = None
    local_latest = getattr(repo, "local_enriched_latest_date", None)
    if callable(local_latest):
        local_latest_value = local_latest()

    normalized_symbols = sorted(set(symbols or []))
    if normalized_symbols:
        universe_definition = {
            "type": "explicit_symbols",
            "count": len(normalized_symbols),
            "hash": _stable_hash(normalized_symbols),
        }
        universe_as_of = end.isoformat()
        warnings: list[str] = []
    else:
        universe_definition = {
            "type": "current_provider_universe",
            "asset_type": "stock",
        }
        universe_as_of = None
        warnings = [
            "survivorship_bias: 当前股票池无法证明为 point-in-time 历史股票池",
        ]

    snapshot = {
        "canonical_generation": manifest.get("generation"),
        "canonical_start_date": manifest.get("start_date"),
        "canonical_end_date": manifest.get("end_date"),
        "local_overlay_latest_date": (
            local_latest_value.isoformat()
            if isinstance(local_latest_value, date)
            else None
        ),
        "data_start": start.isoformat(),
        "data_cutoff": end.isoformat(),
        "adjustment_mode": adjustment_mode,
        "adjustment_generation": source_generations.get("tdx"),
        "source_generations": source_generations,
        "universe_definition": universe_definition,
        "universe_as_of": universe_as_of,
    }
    snapshot["snapshot_hash"] = _stable_hash(snapshot)
    return snapshot, warnings


def build_run_provenance(
    repo: Any,
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    metric_context: MetricContext,
    random_seed: int | None = None,
    adjustment_mode: str = "qfq",
) -> dict:
    data_snapshot, warnings = build_data_snapshot(
        repo,
        start=start,
        end=end,
        symbols=symbols,
        adjustment_mode=adjustment_mode,
    )
    return {
        "data_snapshot": data_snapshot,
        "metric_context": metric_context.to_dict(),
        "engine_version": ENGINE_VERSION,
        "random_seed": random_seed,
        "warnings": warnings,
    }


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
