from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import tickflow_backup as backup


class FakeLifecycle:
    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.calls: list[str] = []

    def inspect(self) -> backup.ContainerState:
        self.calls.append("inspect")
        return backup.ContainerState(
            exists=True,
            running=self.running,
            healthy=self.running,
            container_id="fake-container",
            image="fake-image:latest",
        )

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def start_and_wait_healthy(self) -> None:
        self.calls.append("start_and_wait_healthy")
        self.running = True


def _project_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    data = root / "data"
    (data / "kline_daily" / "date=2026-08-11").mkdir(parents=True)
    (data / "user_data").mkdir(parents=True)
    (data / "kline_daily" / "date=2026-08-11" / "part.parquet").write_bytes(b"bars")
    (data / "user_data" / "preferences.json").write_text('{"theme":"dark"}', "utf-8")
    (data / "user_data" / "paper_account.json").write_text('{"cash":10000}', "utf-8")
    (data / "user_data" / "auth.json").write_text("credential", "utf-8")
    (data / "user_data" / "auth.json.old").write_text("credential", "utf-8")
    (data / "user_data" / "secrets.json").write_text("secret", "utf-8")
    (data / "user_data" / "paper_account.json.lock").write_text("lock", "utf-8")
    (data / "unfinished.tmp").write_text("partial", "utf-8")
    (data / "api-token.txt").write_text("secret", "utf-8")
    (data / "provider.json").write_text('{"api_key":"must-not-leak"}', "utf-8")
    (root / "docker-compose.yml").write_text("services: {}\n", "utf-8")
    (root / "tiers.yaml").write_text("tiers: {}\n", "utf-8")
    return root


def test_sensitive_and_transient_names_are_excluded() -> None:
    excluded = {
        "user_data/auth.json",
        "user_data/auth.json.reset-backup",
        "user_data/secrets.json",
        "user_data/paper_account.json.lock",
        "unfinished.tmp",
        ".env",
        "keys/private-key.pem",
        "api-token.txt",
    }
    for relative in excluded:
        assert backup.exclusion_reason(Path(relative)) is not None, relative

    for relative in (
        "user_data/preferences.json",
        "user_data/paper_account.json",
        "user_data/watchlist.parquet",
        "kline_daily/date=2026-08-11/part.parquet",
    ):
        assert backup.exclusion_reason(Path(relative)) is None, relative


def test_docker_lifecycle_decodes_cli_output_as_utf8(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    backup.DockerComposeLifecycle(tmp_path).inspect()

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_successful_backup_stops_running_container_and_restarts_it(tmp_path: Path) -> None:
    project = _project_fixture(tmp_path)
    lifecycle = FakeLifecycle(running=True)

    snapshot = backup.create_backup(
        project_root=project,
        backup_root=tmp_path / "backups",
        lifecycle=lifecycle,
        apply_retention=False,
    )

    assert snapshot.name.endswith(".complete")
    assert lifecycle.calls == ["inspect", "stop", "start_and_wait_healthy"]
    report = backup.validate_snapshot(snapshot)
    assert report.file_count == 3
    assert (snapshot / "data/user_data/preferences.json").exists()
    assert not (snapshot / "data/user_data/auth.json").exists()
    assert not (snapshot / "data/user_data/secrets.json").exists()
    assert not (snapshot / "data/api-token.txt").exists()
    assert not (snapshot / "data/provider.json").exists()
    metadata = json.loads((snapshot / "metadata.json").read_text("utf-8"))
    assert "user_data/auth.json" in metadata["excluded_files"]
    assert "provider.json" in metadata["excluded_files"]
    assert metadata["container"]["was_running"] is True


def test_backup_failure_still_restarts_originally_running_container(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project_fixture(tmp_path)
    lifecycle = FakeLifecycle(running=True)

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(backup, "_copy_source_files", fail_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        backup.create_backup(
            project_root=project,
            backup_root=tmp_path / "backups",
            lifecycle=lifecycle,
            apply_retention=False,
        )

    assert lifecycle.calls == ["inspect", "stop", "start_and_wait_healthy"]
    assert not list((tmp_path / "backups").glob("*.complete"))
    assert list((tmp_path / "backups").glob("*.staging"))


def test_backup_does_not_start_container_that_was_already_stopped(tmp_path: Path) -> None:
    project = _project_fixture(tmp_path)
    lifecycle = FakeLifecycle(running=False)

    backup.create_backup(
        project_root=project,
        backup_root=tmp_path / "backups",
        lifecycle=lifecycle,
        apply_retention=False,
    )

    assert lifecycle.calls == ["inspect"]


def test_snapshot_validation_detects_tampering(tmp_path: Path) -> None:
    project = _project_fixture(tmp_path)
    snapshot = backup.create_backup(
        project_root=project,
        backup_root=tmp_path / "backups",
        lifecycle=FakeLifecycle(running=False),
        apply_retention=False,
    )
    (snapshot / "data/user_data/preferences.json").write_text('{"theme":"lite"}', "utf-8")

    with pytest.raises(backup.SnapshotValidationError, match="hash mismatch"):
        backup.validate_snapshot(snapshot)


def test_restore_is_isolated_and_refuses_production_data_path(tmp_path: Path) -> None:
    project = _project_fixture(tmp_path)
    snapshot = backup.create_backup(
        project_root=project,
        backup_root=tmp_path / "backups",
        lifecycle=FakeLifecycle(running=False),
        apply_retention=False,
    )

    with pytest.raises(ValueError, match="production data"):
        backup.restore_snapshot(
            snapshot,
            destination=project / "data",
            production_data_dir=project / "data",
        )

    restored = backup.restore_snapshot(
        snapshot,
        destination=tmp_path / "restore-test",
        production_data_dir=project / "data",
    )
    assert (restored / "user_data/preferences.json").exists()
    assert not (restored / "user_data/auth.json").exists()


def test_retention_only_deletes_verified_complete_snapshots(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    legacy = root / "pre-docker-20260812-103349"
    staging = root / "tickflow-20260101-030000.staging"
    corrupt = root / "tickflow-20260102-030000.complete"
    legacy.mkdir()
    staging.mkdir()
    corrupt.mkdir()

    valid: list[Path] = []
    for day in range(1, 18):
        path = root / f"tickflow-202607{day:02d}-190000.complete"
        path.mkdir()
        (path / "COMPLETE.json").write_text("{}", "utf-8")
        valid.append(path)

    monkeypatch.setattr(
        backup,
        "validate_snapshot",
        lambda path: backup.ValidationReport(path=path, file_count=1, total_bytes=1),
    )
    deleted = backup.apply_retention_policy(root, daily=14, weekly=0, monthly=0)

    assert len(deleted) == 3
    assert legacy.exists()
    assert staging.exists()
    assert corrupt.exists()
    assert all(path.name.endswith(".complete") for path in deleted)
    assert all(not path.exists() for path in deleted)
