"""Build and publish full A-share canonical enriched history generations.

The backfill reads only immutable, published provider snapshots. Output lives in a
separate generation root outside the user's ``data/`` directory. A generation is
made visible only by the final atomic ``current.json`` replacement.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
_REQUIRED_SNAPSHOT_LOGICALS = (
    "tdx",
    "fstore",
    "markets",
    "klines",
    "extended",
)
_CALENDAR_SNAPSHOT_LOGICALS = ("tdx", "markets")
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


def _snapshot_paths(
    logicals: tuple[str, ...] = _REQUIRED_SNAPSHOT_LOGICALS,
) -> dict[str, str]:
    paths: dict[str, str] = {}
    missing: list[str] = []
    for logical in logicals:
        path = current_path(logical)
        if path is None:
            missing.append(logical)
        else:
            paths[logical] = path
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"required published provider snapshots unavailable: {joined}")
    return paths


def snapshot_identity(logical: str, path: str | Path) -> dict[str, str] | None:
    """Verify a published snapshot's sibling manifest and return its identity.

    The manifest next to ``path`` must pin ``generation`` equal to the
    directory name and carry an entry for ``logical`` pointing at the same
    file.  On success the resolved ``manifest.json`` SHA-256 is returned so
    canonical manifests can pin the exact published bytes.
    """
    try:
        db = Path(path)
        if db.is_symlink() or not db.is_file():
            return None
        manifest_path = db.parent / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        manifest = _read_json(manifest_path)
        if manifest is None or manifest.get("generation") != db.parent.name:
            return None
        entries = manifest.get("entries")
        entry = next(
            (e for e in entries or [] if isinstance(e, dict) and e.get("logical") == logical),
            None,
        )
        if not isinstance(entry, dict) or Path(str(entry.get("file", ""))).name != db.name:
            return None
        return {
            "generation": db.parent.name,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
    except OSError:
        return None


def _ensure_snapshot_paths_unchanged(expected: dict[str, str]) -> None:
    changed = [logical for logical, path in expected.items() if current_path(logical) != path]
    if changed:
        joined = ", ".join(changed)
        raise RuntimeError(f"provider snapshot changed during backfill: {joined}; retry the job")


def _sql_string(value: Path) -> str:
    return value.as_posix().replace("'", "''")


def _compute_enriched_batch(
    provider: Any,
    symbols: list[str],
    start: datetime,
    end: datetime,
    instruments: pl.DataFrame,
) -> pl.DataFrame:
    raw = provider.get_daily(symbols, start, end, "stock")
    if raw is None or raw.is_empty():
        return pl.DataFrame()
    factors = provider.get_adj_factors(symbols, start, end, "stock")
    return _select_storage_cols(
        compute_enriched(raw, factors=factors, instruments=instruments)
    )

def _compute_enriched_batch_isolated(
    provider_name: str,
    snapshot_paths: dict[str, str],
    symbols: list[str],
    start: datetime,
    end: datetime,
    instruments: pl.DataFrame,
) -> pl.DataFrame:
    provider = get_provider(provider_name, snapshot_paths=snapshot_paths)
    try:
        return _compute_enriched_batch(provider, symbols, start, end, instruments)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


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
        workers: int = 1,
    ) -> dict[str, Any]:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        if not 1 <= workers <= 8:
            raise ValueError("workers must be between 1 and 8")
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
                "workers": workers,
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
            provider = get_provider(
                str(initial["provider"]),
                snapshot_paths=dict(initial.get("snapshot_paths") or {}),
            )
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

            batches = [
                symbols[offset : offset + int(initial["batch_size"])]
                for offset in range(0, len(symbols), int(initial["batch_size"]))
            ]
            completed_symbols = 0

            def persist_batch(enriched: pl.DataFrame) -> None:
                nonlocal table_created, total_rows
                if enriched.is_empty():
                    return
                missing = [
                    column
                    for column in ENRICHED_STORAGE_COLS
                    if column not in enriched.columns
                ]
                if missing:
                    raise RuntimeError(
                        "canonical enriched schema is incomplete: " + ", ".join(missing)
                    )
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

            def record_progress(batch_count: int) -> None:
                nonlocal completed_symbols
                completed_symbols += batch_count
                state.update(
                    {
                        "symbols_done": completed_symbols,
                        "rows": total_rows,
                        "progress": completed_symbols / len(symbols),
                    }
                )
                self._save_state(state)

            workers = int(initial.get("workers") or 1)
            expected_snapshots = dict(initial.get("snapshot_paths") or {})
            if workers == 1:
                for batch in batches:
                    _ensure_snapshot_paths_unchanged(expected_snapshots)
                    persist_batch(
                        _compute_enriched_batch(
                            provider, batch, start_dt, end_dt, instruments
                        )
                    )
                    record_progress(len(batch))
            else:
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="canonical-batch",
                ) as executor:
                    futures = {}
                    for batch in batches:
                        _ensure_snapshot_paths_unchanged(expected_snapshots)
                        future = executor.submit(
                            _compute_enriched_batch_isolated,
                            str(initial["provider"]),
                            expected_snapshots,
                            batch,
                            start_dt,
                            end_dt,
                            instruments,
                        )
                        futures[future] = len(batch)
                    for future in as_completed(futures):
                        _ensure_snapshot_paths_unchanged(expected_snapshots)
                        persist_batch(future.result())
                        record_progress(futures[future])

            _ensure_snapshot_paths_unchanged(dict(initial.get("snapshot_paths") or {}))
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
                "schema_version": 2,
                "kind": "tickflow_canonical_enriched_history",
                "generation": generation,
                "path": f"generations/{generation}",
                "start_date": str(stats[0]),
                "end_date": str(stats[1]),
                "rows": int(stats[4]),
                "symbols": int(stats[3]),
                "trading_days": int(stats[2]),
                "source": str(initial["provider"]),
                "workers": int(initial.get("workers") or 1),
                "source_generations": {
                    logical: snapshot_identity(logical, path)
                    for logical, path in dict(initial.get("snapshot_paths") or {}).items()
                },
                "columns": list(ENRICHED_STORAGE_COLS),
                "published_at": datetime.now(UTC).isoformat(),
            }
            if any(value is None for value in manifest["source_generations"].values()):
                raise RuntimeError("canonical snapshot identity could not be verified")
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


    def publish_incremental_from_local(
        self,
        repo: Any,
        through_date: date,
    ) -> dict[str, Any]:
        """Publish validated local enriched dates as a new immutable generation.

        Existing generation files are hard-linked when the filesystem permits it;
        new local partitions are copied so later overlay replacement cannot mutate
        the published generation. The current pointer changes only after a complete
        coverage scan and a parent-generation compare.
        """
        _ensure_root_outside_user_data(self.root)
        with self._lock:
            state = self._read_state() or {}
            if state.get("status") == "running":
                return {"status": "skipped", "reason": "backfill_running"}
            published = resolve_published_history(self.root)
            if published is None:
                return {"status": "skipped", "reason": "not_published"}
            parent_manifest, parent_dir = published
            columns = parent_manifest.get("columns")
            if (
                int(parent_manifest.get("schema_version") or 0) < 2
                or not isinstance(columns, list)
                or "raw_open" not in columns
            ):
                return {"status": "skipped", "reason": "schema_upgrade_required"}
            parent_end = date.fromisoformat(str(parent_manifest["end_date"]))
            if through_date <= parent_end:
                return {
                    "status": "up_to_date",
                    "generation": parent_manifest["generation"],
                }

            local_root = Path(repo.store.data_dir) / "kline_daily_enriched"
            partitions: list[tuple[date, Path]] = []
            if local_root.is_dir():
                for entry in local_root.iterdir():
                    if not entry.is_dir() or not entry.name.startswith("date="):
                        continue
                    try:
                        day = date.fromisoformat(entry.name.removeprefix("date="))
                    except ValueError:
                        continue
                    if parent_end < day <= through_date:
                        partitions.append((day, entry))
            partitions.sort()
            calendar_snapshot_paths = _snapshot_paths(_CALENDAR_SNAPSHOT_LOGICALS)
            calendar_provider = get_provider(
                str(parent_manifest.get("source") or "fquant_local"),
                snapshot_paths=calendar_snapshot_paths,
            )
            try:
                calendar_frame = calendar_provider.get_daily(
                    ["000001.INDEX"],
                    datetime.combine(parent_end + date.resolution, datetime.min.time()),
                    datetime.combine(through_date, datetime.max.time()),
                    "index",
                )
            finally:
                close = getattr(calendar_provider, "close", None)
                if callable(close):
                    close()
            if (
                calendar_frame is None
                or calendar_frame.is_empty()
                or "date" not in calendar_frame.columns
            ):
                return {"status": "skipped", "reason": "calendar_unavailable"}
            expected_days = {
                value
                for value in calendar_frame.get_column("date").to_list()
                if parent_end < value <= through_date
            }
            actual_days = {day for day, _partition in partitions}
            missing_days = sorted(expected_days - actual_days)
            unexpected_days = sorted(actual_days - expected_days)
            if missing_days or unexpected_days:
                return {
                    "status": "skipped",
                    "reason": "calendar_partition_mismatch",
                    "missing_dates": [day.isoformat() for day in missing_days],
                    "unexpected_dates": [
                        day.isoformat() for day in unexpected_days
                    ],
                }
            if not partitions:
                return {"status": "skipped", "reason": "no_validated_partitions"}

            partition_meta: dict[str, dict[str, Any]] = {}
            for day, partition in partitions:
                files = sorted(partition.glob("*.parquet"))
                if not files:
                    raise RuntimeError(f"canonical incremental partition empty: {day}")
                frame = pl.read_parquet(files)
                missing = [
                    column
                    for column in ENRICHED_STORAGE_COLS
                    if column not in frame.columns
                ]
                if missing:
                    raise RuntimeError(
                        f"canonical incremental schema incomplete for {day}: "
                        + ", ".join(missing)
                    )
                if frame.is_empty() or frame.get_column("date").unique().to_list() != [day]:
                    raise RuntimeError(
                        f"canonical incremental partition date mismatch: {day}"
                    )
                digest = hashlib.sha256()
                for file in files:
                    digest.update(file.name.encode("utf-8"))
                    digest.update(file.read_bytes())
                partition_meta[day.isoformat()] = {
                    "rows": frame.height,
                    "symbols": frame.get_column("symbol").n_unique(),
                    "sha256": digest.hexdigest(),
                }

            job_id = uuid.uuid4().hex
            generation = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{job_id[:8]}"
            staging = self.root / ".staging" / f"incremental-{job_id}"
            output = staging / "published"

            def link_or_copy(source: str, destination: str) -> str:
                try:
                    os.link(source, destination)
                    return destination
                except OSError:
                    return shutil.copy2(source, destination)

            try:
                staging.mkdir(parents=True, exist_ok=False)
                shutil.copytree(
                    parent_dir,
                    output,
                    copy_function=link_or_copy,
                    ignore=shutil.ignore_patterns("manifest.json"),
                )
                for day, source in partitions:
                    destination = output / f"date={day.isoformat()}"
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(source, destination, copy_function=shutil.copy2)

                connection = duckdb.connect()
                try:
                    stats = connection.execute(
                        f"""
                        SELECT min(date), max(date), count(DISTINCT date),
                               count(DISTINCT symbol), count(*)
                        FROM read_parquet(
                            '{_sql_string(output / "date=*" / "*.parquet")}',
                            hive_partitioning = true,
                            union_by_name = true
                        )
                        """
                    ).fetchone()
                finally:
                    connection.close()
                if stats is None or stats[0] is None or stats[1] is None:
                    raise RuntimeError("canonical incremental coverage unavailable")

                latest = resolve_published_history(self.root)
                if (
                    latest is None
                    or latest[0].get("generation")
                    != parent_manifest.get("generation")
                ):
                    raise RuntimeError(
                        "canonical parent generation changed during incremental publish"
                    )
                source_generations = dict(parent_manifest.get("source_generations") or {})
                calendar_identities: dict[str, dict[str, str]] = {}
                for logical in _CALENDAR_SNAPSHOT_LOGICALS:
                    identity = snapshot_identity(logical, calendar_snapshot_paths.get(logical, ""))
                    if identity is None:
                        raise RuntimeError(f"calendar snapshot identity unavailable: {logical}")
                    calendar_identities[logical] = identity
                    source_generations[logical] = identity
                manifest = {
                    "schema_version": 2,
                    "kind": "tickflow_canonical_enriched_history",
                    "generation": generation,
                    "path": f"generations/{generation}",
                    "parent_generation": parent_manifest["generation"],
                    "update_type": "incremental_local_partitions",
                    "start_date": str(stats[0]),
                    "end_date": str(stats[1]),
                    "rows": int(stats[4]),
                    "symbols": int(stats[3]),
                    "trading_days": int(stats[2]),
                    "source": str(parent_manifest.get("source") or "fquant_local"),
                    "source_generations": source_generations,
                    "calendar_source_generations": calendar_identities,
                    "columns": list(ENRICHED_STORAGE_COLS),
                    "incremental_partitions": partition_meta,
                    "published_at": datetime.now(UTC).isoformat(),
                }
                generation_dir = self.root / "generations" / generation
                generation_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(output), str(generation_dir))
                _atomic_write_json(generation_dir / "manifest.json", manifest)
                _atomic_write_json(self.root / "current.json", manifest)
                return {
                    "status": "succeeded",
                    "generation": generation,
                    "parent_generation": parent_manifest["generation"],
                    "partitions": len(partitions),
                    "rows": int(stats[4]),
                }
            finally:
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
