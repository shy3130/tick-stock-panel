"""Shared v1 integrity and generation formats. Reading never repairs the source."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

_MAX_RANGE_GENERATIONS = 128
ASSET_LABELS = {"stock": "股票", "etf": "ETF", "index": "指数"}


class EnrichedGenerationUnavailableError(RuntimeError):
    """The enriched dataset has no stable generation available for readers."""


@dataclass(frozen=True)
class EnrichedGenerationSnapshot:
    generation: str
    base_generation: str
    ranges: tuple[tuple[date, str], ...] = ()

    def through(self, end: date) -> str:
        return next(
            (token for day, token in reversed(self.ranges) if day <= end), self.base_generation
        )

    def payload(self) -> dict[str, Any]:
        return {
            **_ready_payload(self.generation),
            "base_generation": self.base_generation,
            "range_generations": [[day.isoformat(), token] for day, token in self.ranges],
        }

    def changed(self, generation: str, start: date | None) -> EnrichedGenerationSnapshot:
        if start is None:
            return EnrichedGenerationSnapshot(generation, generation)
        ranges = tuple((day, token) for day, token in self.ranges if day < start)
        ranges += ((start, generation),)
        base = self.base_generation
        if len(ranges) > _MAX_RANGE_GENERATIONS:
            base = ranges[-_MAX_RANGE_GENERATIONS - 1][1]
            ranges = ranges[-_MAX_RANGE_GENERATIONS:]
        return EnrichedGenerationSnapshot(generation, base, ranges)


def _snapshot(payload: dict[str, Any]) -> EnrichedGenerationSnapshot:
    generation = payload["generation"]
    # Older writers omit range metadata: conservatively invalidate every range.
    if "range_generations" not in payload:
        return EnrichedGenerationSnapshot(generation, generation)
    try:
        base = payload["base_generation"]
        ranges = tuple(
            (date.fromisoformat(day), token) for day, token in payload["range_generations"]
        )
        if (
            not isinstance(base, str)
            or not base
            or len(ranges) > _MAX_RANGE_GENERATIONS
            or any(not isinstance(token, str) or not token for _, token in ranges)
            or any(a[0] >= b[0] for a, b in pairwise(ranges))
            or (ranges[-1][1] if ranges else base) != generation
        ):
            raise ValueError("invalid range generations")
    except (KeyError, TypeError, ValueError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched range generation marker is invalid"
        ) from exc
    return EnrichedGenerationSnapshot(generation, base, ranges)


def _marker_path(data_dir: Path, asset_type: str) -> Path:
    return Path(data_dir) / f".matrix_generation_{asset_type}.json"


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrichedGenerationUnavailableError("enriched data generation marker is invalid")
    return payload


def _ready_payload(generation: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "generation": generation,
        "updated_at_ns": time.time_ns(),
    }


def _is_ready_payload(payload: dict[str, Any]) -> bool:
    generation = payload.get("generation")
    return (
        payload.get("state", "ready") == "ready"
        and isinstance(generation, str)
        and bool(generation)
    )


class DailyIntegrityError(EnrichedGenerationUnavailableError):
    """已知原始日K异常或其依赖重算未完成, 与暂时正在发布区分。"""


def _path(root: Path) -> Path:
    return Path(root) / "user_data" / "daily_integrity_state.json"


@lru_cache(maxsize=32)
def _read(path: str, mtime: int, size: int, identity: tuple[int, int]) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value["version"] != 1 or not isinstance(value["entries"], list):
            raise ValueError("unsupported integrity state")
        for entry in value["entries"]:
            if entry["asset_type"] not in ASSET_LABELS:
                raise ValueError("invalid asset")
            date.fromisoformat(entry["day"])
            if entry["symbol"] is not None and not isinstance(entry["symbol"], str):
                raise ValueError("invalid symbol")
            if (
                not isinstance(entry["needs_raw"], bool)
                or not isinstance(entry.get("needs_enriched", True), bool)
                or entry["kind"] not in {"missing", "snapshot", "corrupt", "invalid"}
                or not isinstance(entry["reason"], str)
            ):
                raise ValueError("invalid repair state")
        revisions = value.get("revisions", {})
        if not isinstance(revisions, dict) or any(
            not isinstance(v, str) for v in revisions.values()
        ):
            raise ValueError("invalid revision")
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DailyIntegrityError("日K完整性状态无法读取, 请检查数据目录") from exc


def snapshot(root: Path) -> dict:
    path = _path(root)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"version": 1, "entries": [], "revisions": {}}
    except OSError as exc:
        raise DailyIntegrityError("日K完整性状态无法读取, 请检查数据目录权限") from exc
    return _read(str(path), info.st_mtime_ns, info.st_size, (info.st_ino, info.st_ctime_ns))


def assert_usable(
    root: Path,
    asset_type: str = "stock",
    *,
    symbols: list[str] | None = None,
    end: date | None = None,
    enriched_only: bool = False,
) -> None:
    end = end or date.max
    for entry in snapshot(root)["entries"]:
        if enriched_only and not entry["needs_raw"] and not entry.get("needs_enriched", True):
            continue
        dependency = entry["asset_type"] == "index" and asset_type in {"stock", "etf"}
        if (
            (entry["asset_type"] == asset_type or dependency)
            and entry["day"] <= end.isoformat()
            and (
                dependency
                or symbols is None
                or entry["symbol"] is None
                or entry["symbol"] in symbols
            )
        ):
            raise DailyIntegrityError(
                f"{ASSET_LABELS[entry['asset_type']]}日K在 {entry['day']} 有未完成修补或重算, "
                "请到 数据→自检/修补 完成后再计算"
            )


def published_generation(root: Path) -> str | None:
    """Older repositories may lack markers. An in-flight marker never permits import."""
    payload = _read_marker(_marker_path(root, "stock"))
    if payload is None:
        return None
    if not _is_ready_payload(payload):
        raise EnrichedGenerationUnavailableError("Enriched data is being published")
    return _snapshot(payload).generation
