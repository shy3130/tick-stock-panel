"""AlphaGPT Research v1.0 发布清单的通用校验工具。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path, *, role: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required release artifact is missing: {path.name}")
    return {
        "file": path.name,
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def verify_artifact_records(
    records: Iterable[dict[str, Any]],
    *,
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        filename = str(record["file"])
        if filename in seen:
            raise ValueError(f"duplicate release artifact record: {filename}")
        seen.add(filename)
        path = artifact_dir / filename
        exists = path.is_file()
        actual_sha = sha256_file(path) if exists else None
        expected_sha = str(record["sha256"])
        results.append(
            {
                "file": filename,
                "exists": exists,
                "expected_sha256": expected_sha,
                "actual_sha256": actual_sha,
                "matches": exists and actual_sha == expected_sha,
            }
        )
    return results


def require_checks(checks: Iterable[dict[str, Any]]) -> None:
    failed = [check for check in checks if not bool(check.get("passed"))]
    if failed:
        names = ", ".join(str(check.get("name")) for check in failed)
        raise ValueError(f"release validation failed: {names}")
