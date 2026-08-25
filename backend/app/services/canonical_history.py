"""Build and publish full A-share canonical enriched history generations.

The backfill reads only immutable, published provider snapshots. Output lives in a
separate generation root outside the user's ``data/`` directory. A generation is
made visible only by the final atomic ``current.json`` replacement.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import uuid
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from app.data_providers.fquant.generation import current_path
from app.data_providers.registry import get_active_provider_name, get_provider
from app.indicators.pipeline import ENRICHED_STORAGE_COLS, _select_storage_cols, compute_enriched

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("/Volumes/WD1/duckdb/snapshots/tickflow-canonical-history")
SAFE_EARLIEST = date(1990, 1, 1)
_REQUIRED_SNAPSHOT_LOGICALS = ("tdx", "fstore", "markets", "klines")
_GENERATION_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")


def history_root() -> Path:
    """Return the dedicated external history root, honoring its env override."""
    configured = os.environ.get("TICKFLOW_CANONICAL_HISTORY_ROOT", str(DEFAULT_ROOT))
    return Path(configured).expanduser()


def _ensure_root_outside_user_data(root: Path) -> None:
    """Refuse configurations that would place generated history in DATA_DIR."""
    from app.config import settings

    resolved_root = root.expanduser().resolve()
    resolved_data = settings.data_dir.expanduser().resolve()
    if resolved_root == resolved_data or resolved_data in resolved_root.parents:
        raise RuntimeError("TICKFLOW_CANONICAL_HISTORY_ROOT must be outside DATA_DIR")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def _resolve_published_history(
    root: Path,
) -> tuple[tuple[dict[str, Any], Path] | None, str | None]:
    """Validate ``current.json`` and its immutable generation directory."""
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        return None, "not_published"

    pointer = _read_json(pointer_path)
    if pointer is None:
        return None, "invalid_current_pointer"

    generation = pointer.get("generation")
    relative_path = pointer.get("path")
    if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
        return None, "invalid_generation"
    if relative_path != f"generations/{generation}":
        return None, "invalid_generation_path"

    try:
        resolved_root = root.resolve()
        generation_dir = (root / relative_path).resolve()
    except OSError:
        return None, "invalid_generation_path"
    if resolved_root not in generation_dir.parents or not generation_dir.is_dir():
        return None, "invalid_generation_path"

    manifest = _read_json(generation_dir / "manifest.json")
    if manifest is None:
        return None, "invalid_generation_manifest"
    if manifest.get("generation") != generation or manifest.get("path") != relative_path:
        return None, "generation_manifest_mismatch"

    return (manifest, generation_dir), None


def resolve_published_history(root: Path | None = None) -> tuple[dict[str, Any], Path] | None:
    """Return the currently published, validated history generation if available."""
    published, _ = _resolve_published_history(Path(root or history_root()))
    return published


def _snapshot_paths() -> dict[str, str]:
    paths: dict[str, str] = {}
    missing: list[str] = []
    for logical in _REQUIRED_SNAPSHOT_LOGICALS:
        path = current_path(logical)
        if path is None:
            missing.append(logical)
        else:
            paths[logical] = path
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"required published provider snapshots unavailable: {joined}")
    return paths


def _ensure_snapshot_paths_unchanged(expected: dict[str, str]) -> None:
    changed = [logical for logical, path in expected.items() if current_path(logical) != path]
    if changed:
        joined = ", ".join(changed)
        raise RuntimeError(f"provider snapshot changed during backfill: {joined}; retry the job")


def _sql_string(value: Path) -> str:
    return value.as_posix().replace("'", "''")


class CanonicalHistoryManager:
    """Single-flight background builder for external canonical history."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or history_root())
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] | None = None

    @property
    def _state_path(self) -> Path:
        return self.root / "status.json"

    def _read_state(self) -> dict[str, Any] | None:
        with self._lock:
            if self._state is None:
                self._state = _read_json(self._state_path)
            return dict(self._state) if self._state is not None else None

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            _atomic_write_json(self._state_path, state)
            self._state = dict(state)

    def status(self) -> dict[str, Any]:
        state = self._read_state() or {}
        if state.get("status") in {"pending", "running"}:
            with self._lock:
                live = self._thread is not None and self._thread.is_alive()
            if not live:
                state.update(
                    {
                        "status": "failed",
                        "error": "canonical history backfill was interrupted",
                        "finished_at": datetime.now(UTC).isoformat(),
                    }
                )
                self._save_state(state)
        published, publish_reason = _resolve_published_history(self.root)
        state.setdefault("status", "idle")
        state["available"] = published is not None
        state["reason"] = None if published is not None else publish_reason
        state["root"] = str(self.root)
        if published is not None:
            state["generation"] = published[0]["generation"]
            state["manifest"] = published[0]
        else:
            state.pop("generation", None)
            state.pop("manifest", None)
        return state

    def start(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        batch_size: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        start = start_date or SAFE_EARLIEST
        end = end_date or date.today()
        if start > end:
            raise ValueError("start_date must not be after end_date")
        _ensure_root_outside_user_data(self.root)

        provider_name = get_active_provider_name("daily")
        snapshot_paths = _snapshot_paths() if provider_name.startswith("fquant") else {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"canonical history root is not writable: {exc}") from exc

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()

            published, _ = _resolve_published_history(self.root)
            state: dict[str, Any] = {
                "status": "running",
                "job_id": uuid.uuid4().hex,
                "provider": provider_name,
                "snapshot_paths": snapshot_paths,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "batch_size": batch_size,
                "progress": 0.0,
                "symbols_total": 0,
                "symbols_done": 0,
                "rows": 0,
                "error": None,
                "available": published is not None,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
            }
            self._save_state(state)
            thread = threading.Thread(
                target=self._run,
                args=(dict(state),),
                name=f"canonical-history-{state['job_id'][:8]}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return dict(state)

    def _run(self, initial: dict[str, Any]) -> None:
        state = dict(initial)
        job_id = str(initial["job_id"])
        generation = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{job_id[:8]}"
        staging = self.root / ".staging" / job_id
        provider: Any = None
        connection: duckdb.DuckDBPyConnection | None = None

        try:
            staging.mkdir(parents=True, exist_ok=False)
            provider = get_provider(str(initial["provider"]))
            instruments = provider.get_instruments("stock")
            if instruments is None or instruments.is_empty() or "symbol" not in instruments.columns:
                raise RuntimeError("active provider returned no A-share instruments")
            symbol_values = (
                instruments.get_column("symbol").drop_nulls().cast(pl.String)
            )
            symbols = (
                symbol_values
                .filter(symbol_values.str.contains(r"^\d{6}\.(SH|SZ|BJ)$"))
                .unique()
                .sort()
                .to_list()
            )
            if not symbols:
                raise RuntimeError("active provider returned no canonical A-share symbols")

            state["symbols_total"] = len(symbols)
            self._save_state(state)
            connection = duckdb.connect(str(staging / "staging.duckdb"))
            table_created = False
            total_rows = 0
            start = date.fromisoformat(str(initial["start_date"]))
            end = date.fromisoformat(str(initial["end_date"]))
            start_dt = datetime.combine(start, datetime.min.time())
            end_dt = datetime.combine(end, datetime.max.time())

            for offset in range(0, len(symbols), int(initial["batch_size"])):
                _ensure_snapshot_paths_unchanged(dict(initial.get("snapshot_paths") or {}))
                batch = symbols[offset : offset + int(initial["batch_size"])]
                raw = provider.get_daily(batch, start_dt, end_dt, "stock")
                if raw is not None and not raw.is_empty():
                    factors = provider.get_adj_factors(batch, start_dt, end_dt, "stock")
                    enriched = _select_storage_cols(
                        compute_enriched(raw, factors=factors, instruments=instruments)
                    )
                    missing = [column for column in ENRICHED_STORAGE_COLS if column not in enriched.columns]
                    if missing:
                        raise RuntimeError(
                            "canonical enriched schema is incomplete: " + ", ".join(missing)
                        )
                    if not enriched.is_empty():
                        connection.register("batch_df", enriched)
                        try:
                            if not table_created:
                                connection.execute(
                                    "CREATE TABLE canonical_rows AS SELECT * FROM batch_df"
                                )
                                table_created = True
                            else:
                                connection.execute(
                                    "INSERT INTO canonical_rows SELECT * FROM batch_df"
                                )
                        finally:
                            connection.unregister("batch_df")
                        total_rows += enriched.height

                done = min(offset + len(batch), len(symbols))
                state.update(
                    {
                        "symbols_done": done,
                        "rows": total_rows,
                        "progress": done / len(symbols),
                    }
                )
                self._save_state(state)

            _ensure_snapshot_paths_unchanged(dict(initial.get("snapshot_paths") or {}))
            if not table_created or total_rows == 0:
                raise RuntimeError("provider returned no canonical history rows")

            stats = connection.execute(
                """
                SELECT min(date), max(date), count(DISTINCT date),
                       count(DISTINCT symbol), count(*)
                FROM canonical_rows
                """
            ).fetchone()
            if stats is None or stats[0] is None or stats[1] is None:
                raise RuntimeError("canonical history coverage could not be determined")

            output = staging / "published"
            output.mkdir()
            connection.execute(
                f"COPY canonical_rows TO '{_sql_string(output)}' "
                "(FORMAT PARQUET, PARTITION_BY (date), COMPRESSION ZSTD)"
            )
            connection.close()
            connection = None

            manifest: dict[str, Any] = {
                "schema_version": 1,
                "kind": "tickflow_canonical_enriched_history",
                "generation": generation,
                "path": f"generations/{generation}",
                "start_date": str(stats[0]),
                "end_date": str(stats[1]),
                "rows": int(stats[4]),
                "symbols": int(stats[3]),
                "trading_days": int(stats[2]),
                "source": str(initial["provider"]),
                "source_generations": {
                    logical: Path(path).parent.name
                    for logical, path in dict(initial.get("snapshot_paths") or {}).items()
                },
                "columns": list(ENRICHED_STORAGE_COLS),
                "published_at": datetime.now(UTC).isoformat(),
            }
            generation_dir = self.root / "generations" / generation
            generation_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(output), str(generation_dir))
            _atomic_write_json(generation_dir / "manifest.json", manifest)
            _atomic_write_json(self.root / "current.json", manifest)

            state.update(
                {
                    "status": "succeeded",
                    "available": True,
                    "generation": generation,
                    "manifest": manifest,
                    "rows": int(stats[4]),
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            self._save_state(state)
        except Exception as exc:
            logger.exception("canonical history backfill failed")
            published, _ = _resolve_published_history(self.root)
            state.update(
                {
                    "status": "failed",
                    "available": published is not None,
                    "error": str(exc),
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            try:
                self._save_state(state)
            except OSError:
                logger.exception("failed to persist canonical history failure state")
        finally:
            if connection is not None:
                connection.close()
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("failed to close canonical history provider", exc_info=True)
            shutil.rmtree(staging, ignore_errors=True)


_manager_lock = threading.Lock()
_manager = CanonicalHistoryManager()


def canonical_history_manager(root: Path | None = None) -> CanonicalHistoryManager:
    """Return the process manager, replacing it only for an explicit root change."""
    global _manager
    target = Path(root) if root is not None else history_root()
    with _manager_lock:
        if target != _manager.root:
            _manager = CanonicalHistoryManager(target)
        return _manager
