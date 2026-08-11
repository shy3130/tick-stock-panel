"""Deterministic universe selection and auditable manifests."""
from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def stable_symbol_sample(symbols: Iterable[Any], size: int, seed: int) -> list[str]:
    """Sample from a canonical symbol order, independent of query/process order."""
    candidates = sorted({str(symbol) for symbol in symbols if symbol is not None})
    if size < 0:
        raise ValueError("universe size must be non-negative")
    return random.Random(int(seed)).sample(candidates, min(int(size), len(candidates)))


def symbols_sha256(symbols: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(str(symbol) for symbol in symbols).encode("utf-8")).hexdigest()


def universe_manifest(
    symbols: Iterable[str],
    *,
    seed: int,
    requested_size: int,
    start: Any,
    end: Any,
    source: str = "data/kline_daily_enriched/**/*.parquet",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = [str(symbol) for symbol in symbols]
    manifest: dict[str, Any] = {
        "selection": "deduplicate + lexicographic symbol sort + random.Random(seed).sample",
        "seed": int(seed),
        "requested_size": int(requested_size),
        "actual_size": len(selected),
        "start": str(start),
        "end": str(end),
        "source": source,
        "symbols_sha256": symbols_sha256(selected),
        "symbols": selected,
    }
    if extra:
        manifest["extra"] = dict(extra)
    return manifest


def write_universe_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
