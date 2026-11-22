"""The normal import center and research worker use the same signed source installation."""

import json
import os
import runpy
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_engine_jobs import drain
from test_import_center import client, inspect  # noqa: F401 - management fixture
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery

from app.api.engine_jobs import router
from app.services.engine_jobs.manager import EngineJobManager


def test_source_update_between_job_submission_and_spawn(client, tmp_path, monkeypatch):  # noqa: F811
    import app.services.heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, "_memory_admission_ok", lambda: True)
    example = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "packages/tivelo-data/examples/native-provider/build.py"
        )
    )
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "release-trust.json"
    trust.write_text(json.dumps(example["trust_document"](key)))
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(trust))
    app = client.app
    app.include_router(router)
    manager = EngineJobManager(tmp_path, autostart=False)
    app.state.engine_job_manager = manager
    root = "/api/engine-jobs"

    def install(version, generation, price):
        build = tmp_path / version
        build.mkdir()
        native = example["compile_module"](build, version)
        payload = example["bundle"](native, key, version, price)
        info = inspect(client, "source.zip", payload).json()
        assert info["kind"] == "engine"
        preview = info["preview"]
        assert not preview["errors"]
        url = "/api/engine-extension-packages"
        response = client.post(
            url + "/stage",
            files={"file": ("source.zip", payload)},
            data={k: preview[k] for k in ("revision", "digest")},
        )
        assert response.status_code == 200, response.text
        digest = preview["digest"]
        response = client.post(
            f"{url}/{digest}/switch", json={"operation": "activate", "generation": generation}
        )
        assert response.status_code == 200, response.text
        return digest

    capture = dict(
        kind="engine_data_provider",
        provider_id=example["CAPABILITY"],
        request=example["REQUEST"],
    )

    def submit(key, digest):
        request = dict(
            idempotency_key=key, nodes=[dict(node_id="market", capture=dict(datasets=[capture]))]
        )
        response = client.post(root + "/preview", json=request)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["trading_day_capture_nodes"] == ["market"]
        assert plan["plan"]["plan_version"] == "1.3"
        locked = plan["plan"]["inputs"][0]["preparation"]
        assert locked["sources"][0]["plan"]["extension"]["archive_sha256"] == digest
        assert "tivelo-extensions" in {p["name"] for p in locked["packages"]}
        result = client.post(root, params={"expected_plan_id": plan["plan_id"]}, json=request)
        assert result.status_code == 200, result.text
        return result.json()["job_id"]

    def schedule(key, digest):
        response = client.post(
            root + "/schedules",
            json={
                "schedule_version": "1.1",
                "schedule_key": key,
                "trigger": {
                    "kind": "trading_day",
                    "calendar_version": "2026.1.0",
                    "start_date": "2025-01-03",
                    "end_date": "2025-01-03",
                    "anchor": "close",
                    "offset_minutes": 30,
                    "daily_capture_nodes": ["market"],
                },
                "template": {"nodes": [{"node_id": "market", "capture": {"datasets": [capture]}}]},
            },
        )
        assert response.status_code == 200, response.text
        identity = response.json()["schedule_id"]
        assert client.post(
            f"{root}/schedules/{identity}/enabled", json={"enabled": True}
        ).is_success
        assert manager.dispatch_due("2025-01-03T07:30:00Z") == 1
        (delivery,) = manager.schedules.deliveries(identity)
        assert delivery["status"] == "submitted" and delivery["trading_date"] == "2025-01-03"
        job = delivery["job_id"]
        plan = client.get(f"{root}/{job}/plan").json()
        source = plan["inputs"][0]["preparation"]["sources"][0]["plan"]
        assert source["extension"]["archive_sha256"] == digest
        assert (source["request"]["start"], source["request"]["end"]) == (
            "2025-01-02T16:00:00Z",
            "2025-01-03T16:00:00Z",
        )
        return job

    def read(job, price, dates=("2025-01-02", "2025-01-03")):
        state = drain(manager, job)
        assert state["status"] == "succeeded", state["nodes"]
        assert all(
            node["attempts"][0]["worker"]["pid"] != os.getpid() for node in state["nodes"].values()
        )
        response = client.get(f"{root}/{job}/snapshots/market")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        with PortableSnapshotReader(
            manager.store.path(snapshot["path"]), snapshot_id=snapshot["snapshot_id"]
        ) as reader:
            reader.verify()
            req = example["REQUEST"]
            query = BarQuery(
                instrument_ids=req["instrument_ids"],
                timeframe="1d",
                start=req["start"],
                end=req["end"],
                as_of=datetime(2025, 1, 7, tzinfo=UTC),
            )
            assert [(r.trading_date.isoformat(), r.close) for r in reader.bars(query)] == [
                (day, price) for day in dates
            ]
        return snapshot["snapshot_id"]

    try:
        first = install("1.0.0", 0, "10")
        old = submit("before-upgrade", first)
        scheduled_old = schedule("schedule-before-upgrade", first)
        second = install("2.0.0", 1, "12")
        new = submit("after-upgrade", second)
        scheduled_new = schedule("schedule-after-upgrade", second)
        watched = capture["request"]["dataset_id"]
        response = client.post(
            root + "/schedules",
            json={
                "schedule_version": "1.2",
                "schedule_key": "signed-publications",
                "trigger": {"kind": "data_publication", "dataset_id": watched},
                "template": {"nodes": [{"node_id": "market", "capture": {"datasets": [capture]}}]},
            },
        )
        assert response.is_success, response.text
        subscription = response.json()["schedule_id"]
        snapshots = {
            job: read(job, price, dates)
            for job, price, dates in (
                (old, 10, ("2025-01-02", "2025-01-03")),
                (new, 12, ("2025-01-02", "2025-01-03")),
                (scheduled_old, 10, ("2025-01-03",)),
                (scheduled_new, 12, ("2025-01-03",)),
            )
        }
        assert len(set(snapshots.values())) == 4
        expected = {}
        for job, snap in snapshots.items():
            (version,) = client.get(f"{root}/{job}/snapshots/market").json()["datasets"]
            expected[version["version_id"]] = snap
        assert len(expected) == 4
        response = client.post(
            f"/api/engine-extension-packages/{second}/switch",
            json={"operation": "disable", "generation": 2},
        )
        assert response.is_success, response.text
        manager.set_schedule_enabled(subscription, True)
        for _ in range(4):
            assert manager.dispatch_due("2025-01-03T07:30:00Z") == 1
            delivery = manager.schedules.deliveries(subscription, limit=1)[0]
            plan = manager.store.plan(delivery["job_id"])
            assert plan.plan_version == "1.5" and not plan.inputs[0].preparation.sources
            assert drain(manager, plan.plan_id)["status"] == "succeeded"
            snapshot = client.get(f"{root}/{plan.plan_id}/snapshots/market").json()
            assert expected.pop(delivery["publication"]["version_id"]) == snapshot["snapshot_id"]
        assert not expected and manager.dispatch_due("2025-01-03T07:30:00Z") == 0
    finally:
        assert manager.shutdown()


def test_native_worker_budget_counts_and_terminates_descendants():
    import subprocess
    import sys
    from contextlib import suppress

    import psutil

    from app.backtest.worker import _native_tree_rss, _terminate_task

    child_code = (
        "import time; data=bytearray(8*1024*1024); print('ready',flush=True); time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-u','-c',sys.argv[1]],stdout=subprocess.PIPE); "
        "assert child.stdout.readline().strip()==b'ready'; print(child.pid,flush=True); time.sleep(30)"
    )
    with subprocess.Popen(
        [sys.executable, "-u", "-c", parent_code, child_code], stdout=subprocess.PIPE
    ) as parent:
        worker = None
        try:
            worker = psutil.Process(int(parent.stdout.readline()))
            root_rss = psutil.Process(parent.pid).memory_info().rss
            assert _native_tree_rss(parent.pid) >= root_rss + worker.memory_info().rss - 1024 * 1024
            _terminate_task(parent, native_data=True)
            parent.wait(timeout=5)
            worker.wait(timeout=5)
        finally:
            with suppress(ProcessLookupError):
                parent.kill()
            if worker is not None:
                with suppress(psutil.NoSuchProcess):
                    worker.kill()
