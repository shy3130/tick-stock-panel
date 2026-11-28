"""Bounded metadata discovery in the workbench input root; no executable imports."""

import os
from collections import deque
from itertools import islice

from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.errors import TiveloError

from .artifacts import read_document, read_input_run
from .storage import JobError

MAX_ENTRIES = 1024
MAX_DEPTH = 4
MAX_METADATA_BYTES = 32 * 1024**2


def run_summary(path, archive, lock):
    spec = lock.contents.spec
    return {
        "path": path,
        "archive_id": archive.archive_id,
        "lock_id": lock.lock_id,
        "snapshot_id": spec.snapshot_id,
        "strategy_id": spec.strategy.definition_id,
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        "universe_count": len(spec.universe),
        "price_view": spec.price_view.model_dump(mode="json"),
        "controlled_semantics": lock.contents.scale_proof.numeric_semantics
        if lock.contents.scale_proof
        else None,
        "accounting_model": spec.accounting_model,
        "semantics_version": spec.semantics_version,
        "initial_capital": str(spec.initial_capital),
        "currency": spec.base_currency,
    }


def input_catalog(store):
    runs, snapshots, issues = [], [], []
    pending = deque([("inputs", 0)])
    remaining = MAX_ENTRIES
    metadata_remaining = MAX_METADATA_BYTES
    truncated = False
    while pending and remaining > 0:
        parent, depth = pending.popleft()
        try:
            with os.scandir(store.path(parent)) as iterator:
                entries = list(islice(iterator, remaining + 1))
            if len(entries) > remaining:
                entries = entries[:remaining]
                truncated = True
            remaining -= len(entries)
            for entry in sorted(entries, key=lambda value: value.name):
                if entry.is_symlink():
                    issues.append({"path": f"{parent}/{entry.name}"[7:], "code": "UNSAFE_PATH"})
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                relative = f"{parent}/{entry.name}"
                path = relative[7:]
                try:
                    directory = store.path(relative)
                    run = (directory / "run-lock.json").exists()
                    snapshot = (directory / "bundle.json").exists()
                    names = (
                        ("manifest.json", "run-lock.json")
                        if run
                        else (("bundle.json",) if snapshot else ())
                    )
                    size = sum(store.path(relative + "/" + name).stat().st_size for name in names)
                    if size > metadata_remaining:
                        truncated = True
                        issues.append({"path": path, "code": "RESOURCE_LIMIT"})
                        continue
                    metadata_remaining -= size
                    if run:
                        archive, lock = read_input_run(store, path)
                        runs.append(run_summary(path, archive, lock))
                    elif snapshot:
                        bundle = SnapshotBundle.model_validate_json(
                            read_document(store.path(relative + "/bundle.json"))
                        )
                        snapshots.append(
                            {
                                "path": path,
                                "snapshot_id": bundle.snapshot.snapshot_id,
                                "dataset_count": len(bundle.datasets),
                            }
                        )
                    elif depth + 1 < MAX_DEPTH:
                        pending.append((relative, depth + 1))
                    else:
                        truncated = True
                except (ValueError, OSError, JobError, TiveloError) as exc:
                    issues.append(
                        {
                            "path": path,
                            "code": str(exc.code)
                            if isinstance(exc, (JobError, TiveloError))
                            else "INVALID_INPUT",
                        }
                    )
        except (ValueError, OSError, JobError):
            issues.append({"path": parent[7:] if parent != "inputs" else "", "code": "UNAVAILABLE"})
    return {
        "catalog_version": "1.0",
        "runs": sorted(runs, key=lambda item: item["path"]),
        "snapshots": sorted(snapshots, key=lambda item: item["path"]),
        "issues": issues,
        "truncated": truncated or bool(pending),
    }
