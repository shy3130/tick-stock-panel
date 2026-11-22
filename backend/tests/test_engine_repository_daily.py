import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery
from tivelo_data.cli import main

from app.api.engine_jobs import router
from app.services.engine_jobs.manager import EngineJobManager
from app.services.engine_jobs.storage import JobStore
from app.tickflow.repository import DataStore, KlineRepository


def test_v1_writer_to_hosted_capture_and_independent_reader(tmp_path, monkeypatch, capsys):
    import app.services.heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    repo = KlineRepository(DataStore(tmp_path))
    row = {
        "date": date(2025, 7, 1),
        "symbol": "600000.SH",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 12.5,
        "amount": 13125.0,
    }
    try:
        repo.append_daily(pl.DataFrame([row]))
        repo.append_enriched(
            pl.DataFrame(
                [
                    dict(
                        row,
                        open=5.0,
                        high=5.5,
                        low=4.5,
                        close=5.2500000000005,
                        raw_open=10.0,
                        raw_high=11.0,
                        raw_low=9.0,
                        raw_close=10.5,
                    )
                ]
            )
        )
    finally:
        repo.db.close()
    symbol = {"instrument_id": "security-A", "symbol": "600000.SH", "venue_id": "XSHG"}
    base = {
        "record_type": "bar",
        "market_id": "cn.equity",
        "asset_class": "equity",
        "timeframe": "1d",
        "instrument_ids": ["security-A"],
        "start": "2025-06-30T16:00:00Z",
        "end": "2025-07-01T16:00:00Z",
    }
    sources = [
        {
            "kind": "tsp_repository_daily",
            "symbols": [symbol],
            "expected_provider": "local_archive",
            "layout": layout,
            "request": dict(base, price_basis=basis, dataset_id=basis),
        }
        for layout, basis in (("enriched_v1", "qfq"), ("daily_raw_v1", "raw"))
    ]
    service = EngineJobManager(tmp_path, autostart=False)
    app = FastAPI()
    app.state.engine_job_manager = service
    app.include_router(router)
    api_root = "/api/engine-jobs"
    job = {
        "idempotency_key": "legacy-research",
        "nodes": [{"node_id": "prices", "capture": {"datasets": sources}}],
    }
    try:
        with TestClient(app) as api:
            response = api.post(api_root + "/preview", json=job)
            assert response.status_code == 200, response.text
            preview = response.json()
            locked = preview["plan"]["inputs"][0]["preparation"]["sources"]
            for spec, item in zip(sources, locked, strict=True):
                config = tmp_path / "capture.json"
                config.write_text(json.dumps({k: v for k, v in spec.items() if k != "kind"}))
                assert (
                    main(
                        [
                            "--root",
                            str(tmp_path / "separate"),
                            "plan-repository",
                            "--repository",
                            str(tmp_path),
                            "--capture",
                            str(config),
                        ]
                    )
                    == 0
                )
                assert json.loads(capsys.readouterr().out) == item["plan"]
            job_id = api.post(
                api_root, params={"expected_plan_id": preview["plan_id"]}, json=job
            ).json()["job_id"]
            for _ in range(8):
                service.run_once()
                state = api.get(f"{api_root}/{job_id}").json()
                if state["status"] in ("failed", "succeeded"):
                    break
            assert state["status"] == "succeeded", state
            assert all(
                stage["attempts"][0]["worker"]["pid"] != os.getpid()
                for stage in state["nodes"].values()
            )
            summary = api.get(f"{api_root}/{job_id}/snapshots/prices").json()
            assert {d["price_basis"] for d in summary["datasets"]} == {"raw", "qfq"}
            with PortableSnapshotReader(
                service.store.path(summary["path"]), snapshot_id=summary["snapshot_id"]
            ) as reader:
                reader.verify()
                query = BarQuery(
                    instrument_ids=("security-A",),
                    timeframe="1d",
                    start=base["start"],
                    end=base["end"],
                    as_of=datetime.now(UTC),
                )
                # Both prices refer to the same instrument and day.
                for basis, close in (("raw", Decimal("10.5")), ("qfq", Decimal("5.25"))):
                    priced_query = query.model_copy(update={"price_basis": basis})
                    (bar,) = reader.bars(priced_query)
                    assert (bar.close, bar.volume, bar.turnover) == (close, 1250, 13125)
                    panel = read_legacy_daily_panel(
                        reader, priced_query, symbols={"security-A": "600000.SH"}
                    )
                    assert panel.select("symbol", "close", "volume", "amount").row(0) == (
                        "600000.SH", float(close), 12.5, 13125,
                    )
    finally:
        assert service.shutdown()


def test_host_persists_decimal_source_location_and_replays_before_and_after_repair(
    tmp_path, monkeypatch
):
    from app.services import heavy_job_limiter

    monkeypatch.setattr(heavy_job_limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    day = date(2025, 7, 1)
    partition = tmp_path / "kline_daily" / f"date={day}" / "part.parquet"
    partition.parent.mkdir(parents=True)
    row = dict(date=day, symbol="600000.SH", open=11.0, high=12.0, low=10.0, volume=11.0)

    def write(close, **extra):
        pl.DataFrame(
            [dict(row, symbol="000001.SZ", close=10.0, **extra), dict(row, close=close, **extra)]
        ).write_parquet(partition)

    dataset = {
        "kind": "tsp_repository_daily",
        "layout": "daily_raw_v1",
        "calendar_version": "2026.1.0",
        "expected_provider": "local_archive",
        "symbols": [{"instrument_id": "security-A", "symbol": row["symbol"], "venue_id": "XSHG"}],
        "request": {
            "dataset_id": "raw",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "record_type": "bar",
            "timeframe": "1d",
            "price_basis": "raw",
            "instrument_ids": ["security-A"],
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-01T16:00:00Z",
        },
    }
    service = EngineJobManager(tmp_path, autostart=False)
    app = FastAPI()
    app.state.engine_job_manager = service
    app.include_router(router)
    endpoint = "/api/engine-jobs"

    def submit(api, key):
        body = {
            "idempotency_key": key,
            "nodes": [{"node_id": "prices", "capture": {"datasets": [dataset]}}],
        }
        preview = api.post(endpoint + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        job = api.post(endpoint, params={"expected_plan_id": preview.json()["plan_id"]}, json=body)
        assert job.is_success, job.text
        return job.json()["job_id"]

    def finish(api, job_id):
        for _ in range(8):
            service.run_once()
            state = api.get(f"{endpoint}/{job_id}").json()
            if state["status"] in {"succeeded", "failed"}:
                return state
        raise AssertionError(state)

    try:
        with TestClient(app) as api:
            write(11.5)
            original = submit(api, "original")
            assert finish(api, original)["status"] == "succeeded"
            write(11.123456789012345)
            digest = hashlib.sha256(partition.read_bytes()).hexdigest()
            faulty = submit(api, "faulty")
            state = finish(api, faulty)
            assert state["status"] == "failed", state
            stage_name = next(k for k, v in state["nodes"].items() if v["status"] == "failed")
            failure = state["nodes"][stage_name]["attempts"][-1]["error"]
            assert failure == {
                "code": "CONTRACT_INVALID",
                "source_issues": [
                    {
                        "layout": "daily_raw_v1",
                        "trading_date": day.isoformat(),
                        "symbol": row["symbol"],
                        "field": "close",
                        "row_number": 2,
                        "file_sha256": digest,
                        "reason": "fractional_precision",
                    }
                ],
            }
            assert (
                JobStore(tmp_path).get(faulty)["nodes"][stage_name]["attempts"][0]["error"]
                == failure
            )
            unsealed = api.post(f"{endpoint}/{faulty}/retry")
            assert (
                unsealed.status_code == 409 and unsealed.json()["detail"]["code"] == "DATA_CONFLICT"
            )
            repeated = submit(api, "observe-again")
            assert finish(api, repeated)["nodes"][stage_name]["attempts"][0]["error"] == failure
            write(
                11.123456789012345,
                quote_ts=int(datetime(2025, 7, 1, 7, tzinfo=UTC).timestamp() * 1000),
            )
            unfinished = submit(api, "unfinished")
            quote_error = finish(api, unfinished)["nodes"][stage_name]["attempts"][0]["error"]
            assert quote_error == {
                "code": "DATA_COVERAGE_INSUFFICIENT",
                "source_issues": [
                    {
                        **failure["source_issues"][0],
                        "field": "quote_ts",
                        "reason": "quote_before_finalization",
                        "file_sha256": hashlib.sha256(partition.read_bytes()).hexdigest(),
                    }
                ],
            }
            write(11.25)
            stale = api.post(f"{endpoint}/{faulty}/retry")
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "DATA_CONFLICT"
            history = api.get(f"{endpoint}/{faulty}").json()["nodes"][stage_name]["attempts"]
            assert [a["error"] for a in history] == [failure]
            fixed = submit(api, "corrected")
            assert finish(api, fixed)["status"] == "succeeded"
            snapshots = []
            for job_id, expected in ((original, "11.5"), (fixed, "11.25")):
                summary = api.get(f"{endpoint}/{job_id}/snapshots/prices").json()
                snapshots.append(summary["snapshot_id"])
                with PortableSnapshotReader(
                    service.store.path(summary["path"]), snapshot_id=summary["snapshot_id"]
                ) as reader:
                    reader.verify()
                    (bar,) = reader.bars(
                        BarQuery(
                            instrument_ids=("security-A",),
                            timeframe="1d",
                            start=dataset["request"]["start"],
                            end=dataset["request"]["end"],
                            as_of=datetime.now(UTC),
                        )
                    )
                    assert bar.close == Decimal(expected)
            assert len(set(snapshots)) == 2
    finally:
        assert service.shutdown()
