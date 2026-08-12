from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.freeze_mvp import freeze_release, verify_release


def _write_current(current: Path) -> None:
    current.mkdir(parents=True)
    payload = {
        "schema": "tickflow.mvp_backtest.v1",
        "protocol_hash": "protocol",
        "evidence_status": "historical_backtest_only_not_live_validated",
        "strategy": {"id": "trend_breakout", "name": "趋势突破"},
        "seed": 7,
        "data_status": {"valid": True, "min_date": "2024-01-01", "max_date": "2024-02-01"},
        "universe": {"symbols_sha256": "universe"},
        "result": {"status": "completed", "metrics": {"total_return": -0.02}},
    }
    (current / "mvp_backtest.json").write_text(json.dumps(payload), encoding="utf-8")
    (current / "mvp_backtest.html").write_text("<html>report</html>", encoding="utf-8")


def test_freeze_is_idempotent_and_verifiable(tmp_path: Path) -> None:
    current = tmp_path / "current"
    archive = tmp_path / "archive"
    project = tmp_path / "project"
    source = project / "entry.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('mvp')\n", encoding="utf-8")
    _write_current(current)

    first = freeze_release(
        current_dir=current,
        archive_dir=archive,
        project_root=project,
        source_files=("entry.py",),
    )
    second = freeze_release(
        current_dir=current,
        archive_dir=archive,
        project_root=project,
        source_files=("entry.py",),
    )

    assert first == second
    assert verify_release(archive)["release"]["version"] == "0.1.0"


def test_freeze_refuses_to_overwrite_changed_snapshot(tmp_path: Path) -> None:
    current = tmp_path / "current"
    archive = tmp_path / "archive"
    project = tmp_path / "project"
    source = project / "entry.py"
    source.parent.mkdir(parents=True)
    source.write_text("first\n", encoding="utf-8")
    _write_current(current)
    freeze_release(
        current_dir=current,
        archive_dir=archive,
        project_root=project,
        source_files=("entry.py",),
    )

    source.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        freeze_release(
            current_dir=current,
            archive_dir=archive,
            project_root=project,
            source_files=("entry.py",),
        )


def test_freeze_rejects_failed_backtest(tmp_path: Path) -> None:
    current = tmp_path / "current"
    archive = tmp_path / "archive"
    project = tmp_path / "project"
    source = project / "entry.py"
    source.parent.mkdir(parents=True)
    source.write_text("first\n", encoding="utf-8")
    _write_current(current)
    payload = json.loads((current / "mvp_backtest.json").read_text(encoding="utf-8"))
    payload["result"]["status"] = "failed"
    (current / "mvp_backtest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="成功完成"):
        freeze_release(
            current_dir=current,
            archive_dir=archive,
            project_root=project,
            source_files=("entry.py",),
        )
