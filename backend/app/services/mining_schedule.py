"""Weekly scheduled mining orchestration and deterministic data claims."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.backtest.factor import FACTOR_COLUMNS, FACTOR_METHODOLOGY_VERSION
from app.backtest.mining import (
    required_outer_folds,
    required_trading_bars,
    validation_config_for_profile,
)
from app.services import preferences
from app.services.mining_preflight import enriched_partition_dates
from app.services.regime_builder import load_regime_history, regime_path

logger = logging.getLogger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
MINING_ALGORITHM_VERSION = "mining-v3"
FINGERPRINT_VERSION = "weekly-mining-data-v2"
_PROFILES = frozenset({"balanced", "strict"})
_CLAIM_LOCK = threading.Lock()


def beijing_now(now: datetime | None = None) -> datetime:
    """Return an aware Beijing datetime without depending on the server timezone."""
    if now is None:
        return datetime.now(BEIJING_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING_TZ)
    return now.astimezone(BEIJING_TZ)


def beijing_date(now: datetime | None = None) -> date:
    return beijing_now(now).date()


def iso_week(value: date) -> tuple[int, int]:
    iso_year, week, _ = value.isocalendar()
    return iso_year, week


def build_default_request(repo: Any, profile: str) -> dict[str, Any]:
    """Build the bounded V1 stock/full-market request used by the scheduler."""
    if profile not in _PROFILES:
        raise ValueError(f"unsupported mining profile: {profile}")
    latest = repo.latest_enriched_date("stock")
    end = latest.isoformat() if latest is not None else None
    return {
        "factor_names": [item["id"] for item in FACTOR_COLUMNS[:48]],
        "strategy_ids": [],
        "symbols": None,
        "asset_type": "stock",
        "start": None,
        "end": end,
        "budget_profile": profile,
        "require_regime": True,
    }


def build_data_fingerprint(
    repo: Any,
    app_state: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Hash one stable managed generation plus source metadata."""
    for _attempt in range(2):
        fingerprint = _build_data_fingerprint_once(repo, app_state, request)
        if repo.get_matrix_data_generation(fingerprint["asset_type"]) == fingerprint["generation"]:
            return fingerprint
    raise ValueError("enriched data changed while building the mining fingerprint")


def _build_data_fingerprint_once(
    repo: Any,
    app_state: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    data_dir = Path(repo.store.data_dir)
    asset_type = str(request.get("asset_type") or "stock")
    enriched_root = (
        data_dir / "kline_daily_enriched"
        if asset_type == "stock"
        else data_dir / f"kline_{asset_type}_enriched"
    )
    module_root = Path(__file__).resolve().parents[1]
    components = {
        "version": FINGERPRINT_VERSION,
        "asset_type": asset_type,
        "generation": repo.get_matrix_data_generation(asset_type),
        "latest_enriched_date": _iso_or_none(repo.latest_enriched_date(asset_type)),
        "enriched": _enriched_metadata(enriched_root),
        "instruments": _instrument_metadata(repo, asset_type),
        "regime": _path_metadata(regime_path(data_dir), root=data_dir),
        "algorithm_version": MINING_ALGORITHM_VERSION,
        "methodology_version": FACTOR_METHODOLOGY_VERSION,
        "implementation": _implementation_metadata(module_root),
        "strategies": _selected_strategy_metadata(
            app_state,
            request.get("strategy_ids") or [],
            data_dir,
        ),
    }
    payload = _canonical_json(components)
    return {
        **components,
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def schedule_claim(day: date) -> str:
    iso_year, week = iso_week(day)
    return f"weekly-{iso_year}-W{week:02d}"


def run_weekly_mining(app_state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Check the weekly gate and enqueue mining; never perform mining synchronously."""
    config = preferences.get_mining_schedule()
    day = beijing_date(now)
    if not config["mining_schedule_enabled"]:
        return {"status": "disabled"}
    weekday = day.weekday()
    if weekday > 4 or weekday < config["mining_schedule_weekday"]:
        return {"status": "weekday_mismatch"}

    manager = getattr(app_state, "mining_manager", None)
    repo = getattr(app_state, "repo", None)
    if manager is None or repo is None:
        raise RuntimeError("scheduled mining dependencies are not initialized")
    store = getattr(manager, "store", None)
    if store is None:
        raise RuntimeError("scheduled mining manager has no run store")

    request = build_default_request(repo, config["mining_budget_profile"])
    fingerprint = build_data_fingerprint(repo, app_state, request)
    claim = schedule_claim(day)
    fingerprint = {**fingerprint, "source": "scheduled", "source_claim": claim}

    with _CLAIM_LOCK:
        existing = store.get(claim)
        if existing is not None:
            return {"status": "already_claimed", "run_id": claim}

        prerequisite_error = _prerequisite_error(repo, request)
        if prerequisite_error is not None:
            _record_skipped_prerequisite(store, claim, request, fingerprint, prerequisite_error)
            return {
                "status": "skipped_prerequisite",
                "run_id": claim,
                "error": prerequisite_error,
            }

        run = manager.start(
            request,
            fingerprint,
            force=False,
            source="scheduled",
            run_id=claim,
        )
    run_id = run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", None)
    return {"status": "enqueued", "run_id": run_id or claim}


def _prerequisite_error(repo: Any, request: dict[str, Any]) -> str | None:
    data_dir = Path(repo.store.data_dir)
    end = request.get("end")
    if end is None:
        return "stock enriched data is unavailable"
    regime = regime_path(data_dir)
    try:
        if not regime.is_file() or regime.stat().st_size <= 0:
            return "regime data is unavailable"
    except OSError:
        return "regime data is unavailable"

    start = request.get("start")
    start_date = date.fromisoformat(start) if start is not None else None
    end_date = date.fromisoformat(end)
    partitions = enriched_partition_dates(
        data_dir,
        "stock",
        start_date,
        end_date,
    )
    covered = [value.isoformat() for value in partitions]
    profile = request["budget_profile"]
    validation = validation_config_for_profile(profile)
    required = required_trading_bars(
        validation,
        required_outer_folds(profile),
    )
    if len(covered) < required:
        return (
            "insufficient enriched trading dates: "
            f"need {required}, got {len(covered)}"
        )

    regime_history = load_regime_history(data_dir)
    if regime_history.is_empty() or "date" not in regime_history.columns:
        return "regime data is unavailable"
    regime_dates = set(
        regime_history.select(
            pl.col("date").cast(pl.Utf8).str.slice(0, 10)
        ).to_series().to_list()
    )
    required_predecessors = set(covered[:-1])
    missing_regime = required_predecessors - regime_dates
    if missing_regime:
        return (
            "regime coverage is incomplete for T-1 alignment: "
            f"missing {len(missing_regime)} trading dates"
        )
    return None


def _record_skipped_prerequisite(
    store: Any,
    claim: str,
    request: dict[str, Any],
    fingerprint: dict[str, Any],
    error: str,
) -> None:
    try:
        store.create(request, fingerprint, run_id=claim)
    except Exception:
        if store.get(claim) is not None:
            return
        raise
    store.append_event(
        claim,
        "skipped_prerequisite",
        {"source": "scheduled", "reason": error},
    )
    store.transition_status(claim, "skipped_prerequisite", error=error)


def _instrument_metadata(repo: Any, asset_type: str) -> dict[str, Any]:
    instruments = repo.get_instruments_asset(asset_type)
    if instruments is None or instruments.is_empty() or "symbol" not in instruments.columns:
        return {"rows": 0, "digest": "no-instruments"}
    columns = [
        name
        for name in (
            "symbol",
            "name",
            "total_shares",
            "float_shares",
            "limit_up",
            "limit_down",
        )
        if name in instruments.columns
    ]
    payload = instruments.select(columns).sort("symbol").to_dicts()
    digest = hashlib.blake2b(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        digest_size=20,
    ).hexdigest()
    return {"rows": instruments.height, "columns": columns, "digest": digest}


def _enriched_metadata(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for partition in sorted(root.glob("date=*"), key=lambda item: item.name):
        try:
            date.fromisoformat(partition.name.removeprefix("date="))
        except ValueError:
            continue
        records.append(
            {
                "partition": partition.name,
                "file": _path_metadata(partition / "part.parquet", root=root),
            }
        )
    return {
        "partition_count": len(records),
        "first_partition": records[0]["partition"] if records else None,
        "last_partition": records[-1]["partition"] if records else None,
        "metadata_digest": hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest(),
    }


def _selected_strategy_metadata(
    app_state: Any,
    strategy_ids: list[str],
    data_dir: Path,
) -> list[dict[str, Any]]:
    if not strategy_ids:
        return []
    engine = getattr(app_state, "strategy_engine", None)
    if engine is None:
        raise RuntimeError("strategy engine is unavailable for scheduled mining fingerprint")
    metadata: list[dict[str, Any]] = []
    for strategy_id in sorted(strategy_ids):
        strategy = engine.get(strategy_id)
        if strategy.execution_backend != "matrix_native":
            raise ValueError(f"scheduled mining strategy is not matrix-native: {strategy_id}")
        source_path = Path(strategy.file_path) if strategy.file_path is not None else None
        override_path = data_dir / "user_data" / "strategy_overrides" / f"{strategy_id}.json"
        metadata.append(
            {
                "strategy_id": strategy_id,
                "source": _content_metadata(
                    source_path, root=source_path.parent if source_path else data_dir
                ),
                "source_tree": (
                    _implementation_metadata(source_path.parent)
                    if source_path is not None
                    else None
                ),
                "override": _content_metadata(override_path, root=data_dir),
            }
        )
    return metadata


def _path_metadata(path: Path | None, *, root: Path) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return {"path": _relative_path(path, root), "exists": False}
    return {
        "path": _relative_path(path, root),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _content_metadata(path: Path | None, *, root: Path) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        content = path.read_bytes()
    except OSError:
        return {"path": _relative_path(path, root), "exists": False}
    return {
        "path": _relative_path(path, root),
        "exists": True,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _implementation_metadata(module_root: Path) -> dict[str, Any]:
    records = []
    for path in sorted(module_root.rglob("*.py"), key=lambda item: item.as_posix()):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        records.append({
            "path": path.relative_to(module_root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return {
        "file_count": len(records),
        "digest": hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest(),
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _iso_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
