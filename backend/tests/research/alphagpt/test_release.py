from __future__ import annotations

import hashlib

import pytest

from research.alphagpt.release import (
    artifact_record,
    require_checks,
    verify_artifact_records,
)


def test_release_artifact_record_and_verification(tmp_path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b'{"ok": true}\n')
    record = artifact_record(artifact, role="test")
    assert record["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

    results = verify_artifact_records([record], artifact_dir=tmp_path)
    assert results[0]["matches"] is True

    artifact.write_bytes(b"changed")
    changed = verify_artifact_records([record], artifact_dir=tmp_path)
    assert changed[0]["matches"] is False


def test_release_verification_rejects_duplicate_records(tmp_path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    record = artifact_record(artifact, role="test")
    with pytest.raises(ValueError, match="duplicate"):
        verify_artifact_records([record, record], artifact_dir=tmp_path)


def test_release_requires_every_check_to_pass() -> None:
    require_checks([{"name": "ok", "passed": True}])
    with pytest.raises(ValueError, match="failed"):
        require_checks(
            [
                {"name": "ok", "passed": True},
                {"name": "bad", "passed": False},
            ]
        )
