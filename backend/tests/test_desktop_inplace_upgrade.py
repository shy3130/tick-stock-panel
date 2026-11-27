"""Desktop in-place overwrite upgrade (e.g. v0.4.1 -> v0.4.2) workspace adoption tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tivelo_contracts import API_MAJOR

from app.desktop_engines.bundle import DISTRIBUTIONS, ENGINES, digest, target
from app.desktop_engines.launcher import engine_cli
from app.desktop_engines.store import DesktopStore
from app.desktop_engines.workspaces import current_workspace, route


def _write_bundle(folder: Path, workbench_version: str, *, wheel_tag: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    seed = wheel_tag or workbench_version
    wheels = [
        {
            "name": name,
            "version": "0.1.0a1",
            "filename": f"{name.replace('-', '_')}-0.1.0a1-py3-none-any.whl",
            "sha256": digest(f"{name}:{seed}".encode()),
        }
        for name in DISTRIBUTIONS
    ]
    lock = {
        "format": "tivelo-desktop-combination-v1",
        "workbench": workbench_version,
        "runtime": target(),
        "contracts_api": API_MAJOR,
        "extension_api": 1,
        "research": "tivelo-backtest",
        "extension_kinds": ["price_view", "dataset_provider"],
        "dependency_lock_sha256": digest(f"deps:{seed}".encode()),
        "dependencies": {"polars": "1.44.1"},
        "engine_requirements": {name: ">=0.1.0a1,<0.2" for name in ENGINES},
        "wheels": wheels,
    }
    (folder / "compatibility-lock.json").write_text(
        json.dumps(lock, sort_keys=True),
        encoding="utf-8",
    )
    return folder


def test_inplace_upgrade_adopts_unmigrated_original_workspace(tmp_path: Path) -> None:
    """v0.4.1 -> v0.4.2 -> v0.4.3 overwrite upgrade re-binds baseline without touching user data."""
    data_dir = tmp_path / "data"
    user_pref = data_dir / "user_data" / "preferences.json"
    user_pref.parent.mkdir(parents=True, exist_ok=True)
    user_pref.write_text('{"onboarding_completed": true}', encoding="utf-8")

    b041 = _write_bundle(tmp_path / "b041", "0.4.1")
    b042 = _write_bundle(tmp_path / "b042", "0.4.2")
    b043 = _write_bundle(tmp_path / "b043", "0.4.3")

    # 1. First launch on v0.4.1 initializes combinations.sqlite3 with 0.4.1 baseline_id.
    anchor_41 = DesktopStore(b041, data_dir)
    with route(anchor_41, data_dir) as store:
        assert store.data_dir == data_dir.resolve()

    # 2. Overwrite install upgrades executable to v0.4.2 while data/ stays intact.
    report = tmp_path / "status-042.json"
    assert engine_cli(b042, data_dir, ["--report", str(report), "status"]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    anchor_42 = DesktopStore(b042, data_dir)
    assert payload["baseline_id"] == anchor_42.baseline_id
    assert payload["compatibility_lock"]["workbench"] == "0.4.2"

    with anchor_42.guard(coordinator=True) as db:
        state = current_workspace(anchor_42, db)
        assert state["original"] == anchor_42.baseline_id
        assert state["active"] == anchor_42.baseline_id
        assert state["revision"] == 0
        assert list(state["hosts"]) == [anchor_42.baseline_id]

    # 3. Subsequent upgrade v0.4.2 -> v0.4.3 continues to work seamlessly.
    anchor_43 = DesktopStore(b043, data_dir)
    with route(anchor_43, data_dir) as store:
        assert store.data_dir == data_dir.resolve()
    with anchor_43.guard(coordinator=True) as db:
        state = current_workspace(anchor_43, db)
        assert state["original"] == anchor_43.baseline_id
        assert state["active"] == anchor_43.baseline_id
        assert state["revision"] == 0

    assert user_pref.read_text(encoding="utf-8") == '{"onboarding_completed": true}'


def test_inplace_upgrade_still_fences_migrated_multi_workspace(tmp_path: Path) -> None:
    """A workspace that migrated to a managed copy (user_version=2) must NOT be auto-adopted."""
    data_dir = tmp_path / "data"
    b041 = _write_bundle(tmp_path / "b041", "0.4.1")
    b042 = _write_bundle(tmp_path / "b042", "0.4.2")
    b043 = _write_bundle(tmp_path / "b043", "0.4.3")

    anchor_41 = DesktopStore(b041, data_dir)
    anchor_42 = DesktopStore(b042, data_dir)
    anchor_43 = DesktopStore(b043, data_dir)
    with route(anchor_41, data_dir):
        pass

    slot = "a" * 64
    (anchor_41.root / "workspaces" / slot / "data").mkdir(parents=True)
    with closing(sqlite3.connect(anchor_41.database)) as db, db:
        db.execute(
            "INSERT INTO workspace_hosts VALUES(?,?,?)",
            (anchor_42.baseline_id, slot, json.dumps({"python_abi": "cp312"})),
        )
        db.execute(
            "UPDATE workspace_selection SET active=?, revision=1 WHERE singleton=1",
            (anchor_42.baseline_id,),
        )
        db.execute("ALTER TABLE active RENAME TO workspace_retired_active")
        db.execute("PRAGMA user_version=2")

    with pytest.raises(ValueError, match="当前可执行文件与活动工作区不匹配"), route(anchor_41, data_dir):
        pass

    with pytest.raises(ValueError, match="当前可执行文件与活动工作区不匹配"), route(anchor_43, data_dir):
        pass


def test_inplace_upgrade_still_fences_installed_standalone_engine(tmp_path: Path) -> None:
    """A workspace with a standalone installed core-engine override must NOT be silently adopted."""
    data_dir = tmp_path / "data"
    b041 = _write_bundle(tmp_path / "b041", "0.4.1")
    b042 = _write_bundle(tmp_path / "b042", "0.4.2")

    anchor_41 = DesktopStore(b041, data_dir)
    anchor_42 = DesktopStore(b042, data_dir)
    with route(anchor_41, data_dir):
        pass

    with closing(sqlite3.connect(anchor_41.database)) as db, db:
        db.execute("INSERT INTO active VALUES(?,?,1)", (anchor_41.baseline_id, "b" * 64))
        db.execute("INSERT INTO history VALUES(?,?)", (anchor_41.baseline_id, "b" * 64))

    with pytest.raises(ValueError, match="当前可执行文件与活动工作区不匹配"), route(anchor_42, data_dir):
        pass


def test_inplace_upgrade_still_fences_when_workspaces_dir_exists(tmp_path: Path) -> None:
    """A workspace directory containing managed copies is never silently adopted."""
    data_dir = tmp_path / "data"
    b041 = _write_bundle(tmp_path / "b041", "0.4.1")
    b042 = _write_bundle(tmp_path / "b042", "0.4.2")

    anchor_41 = DesktopStore(b041, data_dir)
    anchor_42 = DesktopStore(b042, data_dir)
    with route(anchor_41, data_dir):
        pass

    (anchor_41.root / "workspaces").mkdir(parents=True)

    with pytest.raises(ValueError, match="当前可执行文件与活动工作区不匹配"), route(anchor_42, data_dir):
        pass


def test_inplace_upgrade_blocks_when_research_job_active(tmp_path: Path) -> None:
    """An active research job in data/research/engine/jobs.sqlite3 blocks silent baseline switch."""
    data_dir = tmp_path / "data"
    b041 = _write_bundle(tmp_path / "b041", "0.4.1")
    b042 = _write_bundle(tmp_path / "b042", "0.4.2")

    anchor_41 = DesktopStore(b041, data_dir)
    anchor_42 = DesktopStore(b042, data_dir)
    with route(anchor_41, data_dir):
        pass

    jobs_db = data_dir / "research" / "engine" / "jobs.sqlite3"
    jobs_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(jobs_db)) as db, db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE job_catalog (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("INSERT INTO job_catalog VALUES('j1', '{\"status\":\"running\"}')")

    with pytest.raises(ValueError, match="仍有排队或执行中的研究任务"), route(anchor_42, data_dir):
        pass

    # Database remained bound to v0.4.1 untouched.
    with anchor_41.guard(coordinator=True) as db:
        assert current_workspace(anchor_41, db)["active"] == anchor_41.baseline_id
