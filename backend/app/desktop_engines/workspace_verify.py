"""Check installed executables and the real indexes belonging to a workspace copy."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from tivelo_contracts.signatures import MAX_ENVELOPE

from .bundle import digest, read_limited
from .store import verify_release


def _run(executable, root, *arguments, timeout=300, draft_ticket=None):
    with tempfile.TemporaryDirectory(prefix="tivelo-workspace-report-") as temp:
        report = Path(temp) / "report.json"
        env = {**os.environ, "DATA_DIR": str(root), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        env.pop("PYTHONPATH", None)
        if draft_ticket is not None:
            env["TSP_WORKSPACE_VERIFY_TICKET"] = draft_ticket
        else:
            env.pop("TSP_WORKSPACE_VERIFY_TICKET", None)
        process = subprocess.run(
            [str(executable), "--engine", "--report", str(report), *arguments],
            env=env,
            cwd=temp,
            timeout=timeout,
            check=False,
        )
        if not report.is_file():
            raise ValueError("配套桌面程序没有返回工作区核验结果")
        result = json.loads(report.read_text(encoding="utf-8"))
        if process.returncode:
            raise ValueError(result.get("message", "配套桌面程序未通过核验"))
        return result


def certify(executable, source, db, *, expected):
    """Run the selected original pair on synthetic facts using a separate core store."""
    executable = executable.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="tivelo-host-proof-") as tmp:
        root = Path(tmp)
        status = _run(executable, root, "status")
        baseline, pair = status["baseline_id"], status["bundled_combination"]
        if expected is not None and baseline != expected:
            raise ValueError("所选可执行文件与工作区宿主基线不匹配")
        selected = None
        if db is not None:
            table = (
                "workspace_retired_active"
                if db.execute("PRAGMA user_version").fetchone() == (2,)
                else "active"
            )
            selected = db.execute(
                f"SELECT id,revision FROM {table} WHERE baseline=?", (baseline,)
            ).fetchone()
        if selected and selected[0] != pair:
            pair, revision = selected
            original, copy = source / "tivelo-desktop", root / "tivelo-desktop"
            if (
                not isinstance(pair, str)
                or len(pair) != 64
                or any(c not in "0123456789abcdef" for c in pair)
            ):
                raise ValueError("工作区核心组合编号无效")
            name = pair + ".json"
            certificate = read_limited(original / "certificates" / name, MAX_ENVELOPE)
            release = verify_release(certificate)
            if release.id != pair or release.baseline_id != baseline:
                raise ValueError("工作区核心组合签名与预计宿主不匹配")
            (copy / "certificates" / name).write_bytes(certificate)
            bundled = {w["sha256"] for w in status["engines"]}
            for wheel in release.wheels:
                if wheel.sha256 not in bundled:
                    filename = wheel.sha256 + ".whl"
                    payload = read_limited(original / "artifacts" / filename)
                    if digest(payload) != wheel.sha256:
                        raise ValueError("已保留的引擎 wheel 损坏")
                    (copy / "artifacts" / filename).write_bytes(payload)
            with closing(sqlite3.connect(copy / "combinations.sqlite3")) as scratch, scratch:
                scratch.execute(
                    "INSERT OR REPLACE INTO active VALUES(?,?,?)", (baseline, pair, revision)
                )
                scratch.execute("INSERT OR IGNORE INTO history VALUES(?,?)", (baseline, pair))
        proof = _run(executable, root, "check")
        if proof["combination"] != pair or proof["workbench"]["final_equity"] != "5237.58":
            raise ValueError("配套宿主未使用原组合完成研究进程核验")
        return {
            "baseline": baseline,
            "combination": pair,
            "image": {
                k: proof["runtime"][k] for k in ("python_sha256", "stdlib_sha256", "python_abi")
            },
        }


def check_clone(draft, installation, ticket):
    # Native self-tests can spawn children. They use this private draft, independent
    # of the exclusive coordinator transaction held by the parent process.
    return _run(
        sys.executable,
        draft,
        "_workspace-check",
        str(installation),
        timeout=3600,
        draft_ticket=ticket,
    )


def verify_contents(root, installation):
    from tivelo_data.storage import SnapshotStore, file_digest
    from tivelo_extensions.installation import EngineInstallation, bind_desktop_installation
    from tivelo_extensions.store import EnginePackageStore

    from app.services.engine_jobs.artifacts import verify_completed
    from app.services.engine_jobs.schedules import ScheduleStore
    from app.services.engine_jobs.storage import JobStore

    bind_desktop_installation(root, installation)
    engine = root / "research/engine"
    result = {
        "jobs": 0,
        "job_artifacts": 0,
        "schedules": 0,
        "versions": 0,
        "snapshots": 0,
        "fact_files": 0,
    }
    if (engine / "jobs.sqlite3").exists() or (engine / "schedules.sqlite3").exists():
        jobs = JobStore(root)
        with jobs.connection() as db:
            for (key,) in db.execute("SELECT id FROM job_catalog"):
                plan = jobs.plan(key)
                for node_id, node in jobs.get(key)["nodes"].items():
                    if node["status"] == "succeeded":
                        verify_completed(jobs, plan, node_id)
                        result["job_artifacts"] += 1
                result["jobs"] += 1
        if (engine / "schedules.sqlite3").exists():
            schedules = ScheduleStore(jobs)
            with schedules.connection() as db:
                for (key,) in db.execute("SELECT id FROM all_schedules"):
                    schedules._definition(key)
                    result["schedules"] += 1
    if (engine / "data/index.sqlite3").exists():
        lake = SnapshotStore(engine / "data")
        content = {}
        with lake.connection() as db:
            for (key,) in db.execute("SELECT version_id FROM versions"):
                version = lake.dataset_version(key)
                for artifact in version.contents.artifacts:
                    file = lake.path(artifact.path)
                    if file not in content:
                        content[file] = (file.stat().st_size, file_digest(file))
                    if content[file] != (artifact.size_bytes, artifact.sha256):
                        raise ValueError("行情事实文件与已发布的版本摘要不一致")
                result["versions"] += 1
            for (key,) in db.execute("SELECT snapshot_id FROM snapshots"):
                lake.snapshot(key)
                result["snapshots"] += 1
        result["fact_files"] = len(content)
    result["extensions"] = EnginePackageStore(root).check_workspace_versions()
    identity = EngineInstallation(root).status()
    result["installation"] = {
        "device": (identity["identity"] or {}).get("sha256"),
        "leases": len(identity["leases"]),
    }
    return result


def record_deliveries(coordinator, source):
    coordinator.execute(
        "CREATE TABLE IF NOT EXISTS workspace_deliveries(schedule_id TEXT, id TEXT, PRIMARY KEY(schedule_id,id))"
    )
    old = source / "research/engine/schedules.sqlite3"
    if old.exists():
        with closing(sqlite3.connect(old.as_uri() + "?mode=ro", uri=True)) as departed:
            submitted = departed.execute(
                "SELECT schedule_id,id FROM deliveries WHERE json_extract(state,'$.status')='submitted'"
            )
            coordinator.executemany(
                "INSERT OR IGNORE INTO workspace_deliveries VALUES(?,?)", submitted
            )


def suspend_rewound_schedules(coordinator, destination):
    """An enabled schedule must not replay an occurrence submitted by any retained generation."""
    target = destination / "research/engine/schedules.sqlite3"
    if (
        not target.exists()
        or not coordinator.execute("SELECT 1 FROM workspace_deliveries LIMIT 1").fetchone()
    ):
        return []
    # The schedule writer normally uses WAL. Rollback journaling on both writable
    # databases is required for SQLite's atomic multi-database commit.
    with closing(sqlite3.connect(target, isolation_level=None, timeout=0)) as retained:
        if retained.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError("计划索引仍被使用，请退出工作台后重试")
    coordinator.execute("ATTACH DATABASE ? AS restore_schedules", (str(target),))
    tables = {
        r[0]
        for r in coordinator.execute(
            "SELECT name FROM restore_schedules.sqlite_schema WHERE type='table'"
        )
    }
    paused = []
    for table in ("schedules", "trading_day_schedules", "extended_schedules"):
        if table in tables:
            changes = coordinator.execute(
                f"UPDATE restore_schedules.{table} AS schedule SET state=json_set(state,'$.enabled',json('false')) "
                "WHERE json_extract(state,'$.enabled')=1 AND EXISTS(SELECT 1 FROM workspace_deliveries AS known "
                "WHERE known.schedule_id=schedule.id AND NOT EXISTS(SELECT 1 FROM restore_schedules.deliveries AS local "
                "WHERE local.id=known.id AND json_extract(local.state,'$.status')='submitted')) RETURNING id"
            )
            paused.extend(key for (key,) in changes)
    return sorted(paused)
