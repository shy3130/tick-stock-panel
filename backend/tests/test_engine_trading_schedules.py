import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_engine_jobs import drain
from test_engine_jobs import make_manager as make_manager
from test_engine_jobs import seed as seed
from test_engine_schedules import START, schedule
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.base import fingerprint
from tivelo_contracts.data import BarQuery

from app.api.engine_jobs import router
from app.backtest.worker import run_worker_task
from app.services.engine_jobs.artifacts import verify_completed
from app.services.engine_jobs.schedule_calendar import freeze_trading_days
from app.services.engine_jobs.schedule_models import ScheduleRequest
from app.services.engine_jobs.storage import JobError


def trading(key="post-close", **changes):
    return schedule(
        key,
        schedule_version="1.1",
        trigger={
            "kind": "trading_day",
            "venue_id": "XSHG",
            "calendar_version": "2026.1.0",
            "start_date": "2026-09-28",
            "end_date": "2026-10-12",
            "anchor": "close",
            "offset_minutes": 30,
            **changes,
        },
    )


def test_market_holiday_catchup_restart_and_original_report(make_manager, monkeypatch):
    manager = make_manager()
    request = trading()
    record = manager.create_schedule(request)
    identity = record["schedule_id"]
    assert not record["enabled"] and not manager.store.list()
    assert record["calendar"]["trading_days"] == 6
    assert record["next_due_at"] == "2026-09-28T07:30:00+00:00"
    manager.set_schedule_enabled(identity, True)
    assert manager.dispatch_due("2026-09-28T07:29:59Z") == 0
    assert manager.dispatch_due("2026-09-30T07:30:00Z") == 1
    (first,) = manager.schedules.deliveries(identity)
    assert (first["trading_date"], first["missed_count"]) == ("2026-09-30", 2)
    assert manager.dispatch_due("2026-10-07T15:59:59Z") == 0
    assert manager.schedules.get(identity)["next_due_at"] == "2026-10-08T07:30:00+00:00"
    assert drain(manager, first["job_id"])["status"] == "succeeded"
    assert manager.shutdown()

    from tivelo_market_cn import presets

    def missing(*args, **kwargs):
        raise AssertionError("persisted schedules must not reload the current market package")

    monkeypatch.setattr(presets, "load_calendar_preset", missing)
    restored = make_manager()
    assert restored.create_schedule(request)["schedule_id"] == identity
    assert restored.dispatch_due("2026-10-20T00:00:00Z") == 1
    final, original = restored.schedules.deliveries(identity)
    assert original == first and final["job_id"] != first["job_id"]
    assert (final["trading_date"], final["missed_count"]) == ("2026-10-12", 2)
    assert restored.dispatch_due("2026-10-20T00:00:00Z") == 0
    assert restored.schedules.get(identity)["next_due_at"] is None
    assert not restored.schedules.get(identity)["enabled"]
    result = drain(restored, final["job_id"])
    report = json.loads(
        restored.store.path(result["nodes"]["first.report"]["artifact"]["path"]).read_text()
    )
    assert report["summary"]["metrics"]["final_equity"] == "5237.58"


def test_trading_schedules_pin_venue_and_resolve_the_open_in_local_time(make_manager):
    manager = make_manager()
    with pytest.raises(JobError) as early:
        freeze_trading_days(trading().trigger, as_of=datetime(2020, 1, 1, tzinfo=UTC))
    assert early.value.code == "FUTURE_INFORMATION"
    saved = manager.create_schedule(
        trading(
            venue_id="XSHE",
            start_date="2026-10-01",
            end_date="2026-10-08",
            anchor="open",
            offset_minutes=-30,
        )
    )
    assert saved["next_due_at"] == "2026-10-08T01:00:00+00:00"
    changed = trading(venue_id="XSHG")
    with pytest.raises(JobError, match="计划键"):
        manager.create_schedule(changed)
    app = FastAPI()
    app.state.engine_job_manager = manager
    app.include_router(router)
    with TestClient(app) as client:
        detail = client.get(f"/api/engine-jobs/schedules/{saved['schedule_id']}/calendar").json()
        assert detail["calendar_id"] == "cn.equity.xshe.regular-auction"
        assert detail["occurrences"] == [
            {"trading_date": "2026-10-08", "due_at": "2026-10-08T01:00:00Z"}
        ]
        assert len(detail["calendar_sha256"]) == len(detail["preset_sha256"]) == 64
        assert (
            client.post(
                "/api/engine-jobs/schedules",
                json=trading("exact", preset_sha256=detail["preset_sha256"]).model_dump(
                    mode="json"
                ),
            ).status_code
            == 200
        )
        for key, changes, code in (
            (
                "closed",
                {"start_date": "2026-10-01", "end_date": "2026-10-07"},
                "DATA_COVERAGE_INSUFFICIENT",
            ),
            ("beyond", {"end_date": "2035-01-01"}, "DATA_COVERAGE_INSUFFICIENT"),
            ("unknown", {"calendar_version": "missing"}, "RULES_UNAVAILABLE"),
            ("digest", {"preset_sha256": "a" * 64}, "ARTIFACT_CORRUPT"),
        ):
            result = client.post(
                "/api/engine-jobs/schedules", json=trading(key, **changes).model_dump(mode="json")
            )
            assert result.status_code == 409 and result.json()["detail"]["code"] == code
        for changes in (
            {"venue_id": "XNYS"},
            {"market_id": "unregistered"},
            {"anchor": "lunch"},
            {"offset_minutes": True},
            {"start_date": "2026-11-30"},
        ):
            body = trading("invalid").model_dump(mode="json")
            body["trigger"].update(changes)
            assert client.post("/api/engine-jobs/schedules", json=body).status_code == 422
        body = trading("legacy").model_dump(mode="json")
        body["schedule_version"] = "1.0"
        assert client.post("/api/engine-jobs/schedules", json=body).status_code == 422
    assert len(manager.schedules.list()) == 2


def test_concurrent_market_claims_and_lost_receipt_are_idempotent(make_manager, monkeypatch):
    manager = make_manager()
    saved = manager.create_schedule(trading())
    identity = saved["schedule_id"]
    manager.set_schedule_enabled(identity, True)
    clock = "2026-10-08T07:30:00Z"
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return manager.schedules.claim_due(clock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [f.result() for f in [pool.submit(claim), pool.submit(claim)]]
    assert len(a) == 1 and a == b and len(manager.schedules.deliveries(identity)) == 1
    monkeypatch.setattr(
        manager.schedules,
        "finish",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("lost receipt")),
    )
    with pytest.raises(sqlite3.OperationalError):
        manager.dispatch_due(clock)
    (job,) = manager.store.list()
    assert manager.shutdown()
    restored = make_manager()
    assert restored.dispatch_due(clock) == 1
    (first,) = restored.schedules.deliveries(identity)
    assert first["job_id"] == job["job_id"] and first["status"] == "submitted"
    assert restored.dispatch_due("2026-10-09T07:30:00Z") == 1
    skipped, first = restored.schedules.deliveries(identity)
    assert skipped["status"] == "skipped_overlap" and skipped["trading_date"] == "2026-10-09"
    assert len(restored.store.list()) == 1


def test_interval_compatibility_and_damaged_calendar_are_isolated(make_manager):
    manager = make_manager()
    interval = schedule("clock")
    original = manager.create_schedule(interval)
    assert original["schedule_id"] == fingerprint(interval)
    cal = manager.create_schedule(trading())
    for entry in (original, cal):
        manager.set_schedule_enabled(entry["schedule_id"], True)
    with manager.schedules.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("SELECT id FROM schedules").fetchall() == [(original["schedule_id"],)]
        raw = json.loads(db.execute("SELECT request FROM trading_day_schedules").fetchone()[0])
        raw["calendar"]["occurrences"][0]["due_at"] = "2026-09-28T07:31:00Z"
        db.execute("UPDATE trading_day_schedules SET request=?", (json.dumps(raw).encode(),))
    assert manager.dispatch_due(START) == 1
    failed = manager.schedules.get(cal["schedule_id"])
    assert not failed["enabled"] and failed["last_error"]["code"] == "ARTIFACT_CORRUPT"
    assert not manager.schedules.deliveries(cal["schedule_id"])
    assert manager.schedules.deliveries(original["schedule_id"])[0]["status"] == "submitted"


def test_market_day_drives_daily_capture_workers_from_separate_partitions(
    make_manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    manager = make_manager(worker_runner=run_worker_task)
    source = {
        "kind": "tsp_repository_daily",
        "layout": "daily_raw_v1",
        "calendar_version": "2026.1.0",
        "expected_provider": "local_archive",
        "symbols": [{"instrument_id": "security-A", "symbol": "600000.SH", "venue_id": "XSHG"}],
        "request": {
            "dataset_id": "daily",
            "record_type": "bar",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["security-A"],
            "price_basis": "raw",
            "timeframe": "1d",
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-01T16:00:00Z",
        },
    }
    spec = trading(start_date="2026-09-30", end_date="2026-10-08").model_dump(mode="json")
    spec["trigger"]["daily_capture_nodes"] = ["prices"]
    spec["template"] = {"nodes": [{"node_id": "prices", "capture": {"datasets": [source]}}]}
    request = ScheduleRequest.model_validate(spec)
    state = manager.create_schedule(request)
    manager.set_schedule_enabled(state["schedule_id"], True)
    snapshots = []
    for day, price, expected_start in (
        (date(2026, 9, 30), 10, "2026-09-29T16:00:00Z"),
        (date(2026, 10, 8), 11, "2026-10-07T16:00:00Z"),
    ):
        directory = tmp_path / "kline_daily" / f"date={day}"
        directory.mkdir(parents=True)
        pl.DataFrame(
            [
                dict(
                    date=day,
                    symbol="600000.SH",
                    open=price,
                    high=price + 1,
                    low=price - 1,
                    close=price,
                    volume=10,
                )
            ]
        ).write_parquet(directory / "row.parquet")
        assert manager.dispatch_due(f"{day}T07:30:00Z") == 1
        delivery = manager.schedules.deliveries(state["schedule_id"])[0]
        assert delivery["trading_date"] == day.isoformat()
        job_id = delivery["job_id"]
        plan = manager.store.plan(job_id)
        selection = plan.request.nodes[0].capture.datasets[0].request.model_dump(mode="json")
        assert (selection["start"], selection["end"]) == (expected_start, f"{day}T16:00:00Z")
        assert drain(manager, job_id)["status"] == "succeeded"
        artifact = verify_completed(manager.store, plan, "prices.snapshot")
        snapshots.append((manager.store.path(artifact.path), day, price))
    for path, day, price in snapshots:
        with PortableSnapshotReader(path) as reader:
            bars = reader.bars(
                BarQuery(
                    instrument_ids=("security-A",),
                    timeframe="1d",
                    start="2026-09-01T00:00:00Z",
                    end="2026-10-31T00:00:00Z",
                    as_of=datetime.now(UTC),
                )
            )
            assert [(b.trading_date, b.close) for b in bars] == [(day, Decimal(price))]
    assert (
        manager.schedules.request(state["schedule_id"])
        .template.nodes[0]
        .capture.datasets[0]
        .request.model_dump(mode="json")["start"]
        == source["request"]["start"]
    )
    assert not manager.schedules.get(state["schedule_id"])["enabled"]


def test_market_capture_rejects_unbound_dates_and_unrelated_templates(make_manager):
    manager = make_manager()
    app = FastAPI()
    app.state.engine_job_manager = manager
    app.include_router(router)
    with TestClient(app) as client:
        choices = client.get("/api/engine-jobs/schedules/calendar-presets").json()["items"]
        preset = next(p for p in choices if p["version"] == "2026.1.0")
        assert len(preset["preset_sha256"]) == 64 and 2026 in preset["venues"]["XSHE"]
        for nodes in (["first"], ["absent"], ["first", "first"]):
            body = trading().model_dump(mode="json")
            body["trigger"]["daily_capture_nodes"] = nodes
            assert client.post("/api/engine-jobs/schedules", json=body).status_code == 422
        request = {
            "dataset_id": "quotes",
            "record_type": "bar",
            "timeframe": "1d",
            "price_basis": "raw",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["demo"],
            "start": "2026-09-29T16:00:00Z",
            "end": "2026-09-30T16:00:00Z",
        }
        for changes in ({"timeframe": "1m"}, {"market_id": "us.equity"}, {"asset_class": "future"}):
            source = {
                "kind": "engine_data_provider",
                "provider_id": "demo.source",
                "request": request | changes,
            }
            body = trading().model_dump(mode="json")
            body["template"]["nodes"] = [{"node_id": "prices", "capture": {"datasets": [source]}}]
            body["trigger"]["daily_capture_nodes"] = ["prices"]
            result = client.post("/api/engine-jobs/schedules", json=body)
            assert result.status_code == 422 and "a trading-date window" in result.text
    saved = manager.create_schedule(trading())
    manager.set_schedule_enabled(saved["schedule_id"], True)
    (delivery,) = manager.schedules.claim_due("2026-09-28T07:30:00Z")
    for corrupted in (
        dict(delivery, trading_date="2026-09-30"),
        dict(delivery, request_key="schedule.invalid"),
    ):
        with pytest.raises(JobError) as failure:
            manager.schedules.delivery_request(corrupted)
        assert failure.value.code == "ARTIFACT_CORRUPT"
