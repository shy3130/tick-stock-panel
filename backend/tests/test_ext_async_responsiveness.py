import asyncio
import io
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import UploadFile

from app.api import ext_data
from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig, run_ext_data_io


def require_responsive_loop():
    loop = asyncio.get_running_loop()

    def check():
        acknowledged = threading.Event()
        loop.call_soon_threadsafe(acknowledged.set)
        assert acknowledged.wait(1), "synchronous work blocked the event loop"

    return check


@pytest.mark.parametrize("endpoint", ["upload", "detect"])
async def test_file_parsing_leaves_loop_responsive(tmp_path, monkeypatch, endpoint):
    config = ExtConfig(id="test", label="test", mode="snapshot", fields=[
        ExtField("symbol"), ExtField("value", dtype="float"),
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    check = require_responsive_loop()
    original = pl.read_csv

    def parse(*args, **kwargs):
        check()
        return original(*args, **kwargs)

    monkeypatch.setattr(ext_data.pl, "read_csv", parse)
    monkeypatch.setattr(ext_data, "_store", lambda _: SimpleNamespace(get=lambda _: config))
    monkeypatch.setattr(ext_data, "_refresh_views", lambda _: check())
    file = UploadFile(io.BytesIO(b"symbol,value\n600000.SH,1.5\n"), filename="test.csv")
    if endpoint == "upload":
        result = await ext_data.upload_data(request, "test", file, "2026-10-29")
        assert result == {"status": "ok", "rows": 1, "date": "2026-10-29"}
        saved = pl.read_parquet(next((tmp_path / "ext_data").rglob("*.parquet")))
        assert saved["value"].to_list() == [1.5]
    else:
        result = await ext_data.detect_fields(request, file)
        assert result["rows"] == 1
        assert "symbol" in result["symbol_candidates"]


async def test_scheduled_ingest_leaves_loop_responsive(tmp_path, monkeypatch):
    config = ExtConfig(id="test", label="test", mode="snapshot", fields=[
        ExtField("symbol"), ExtField("value", dtype="float"),
    ], pull=PullConfig(url="https://example.test/data", enabled=True))
    check = require_responsive_loop()
    original = ext_pull.rows_to_parquet

    async def fetch(*args):
        return [{"symbol": "600000.SH", "value": 1.5}]

    def write(*args, **kwargs):
        check()
        return original(*args, **kwargs)

    monkeypatch.setattr(ext_pull, "fetch_rows_for_date", fetch)
    monkeypatch.setattr(ext_pull, "rows_to_parquet", write)
    assert await ext_pull.fetch_and_ingest(config, tmp_path, date(2026, 10, 29)) == (1, "2026-10-29")


async def test_cancelling_request_does_not_release_running_write_slot():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    second_entered = threading.Event()

    def first():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2)
        finally:
            finished.set()

    def second():
        second_entered.set()
        assert finished.is_set(), "cancelled writer was still modifying the partition"

    task = asyncio.create_task(run_ext_data_io(first))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    follower = asyncio.create_task(run_ext_data_io(second))
    try:
        await asyncio.sleep(0.03)
        assert not second_entered.is_set()
    finally:
        release.set()
        await asyncio.wait_for(follower, 2)


async def test_cancelled_queued_import_never_runs():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls = []

    def first():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)

    running = asyncio.create_task(run_ext_data_io(first))
    await asyncio.wait_for(entered.wait(), 1)
    queued = asyncio.create_task(run_ext_data_io(calls.append, "cancelled"))
    try:
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    finally:
        release.set()
        await running
    await run_ext_data_io(calls.append, "finished")
    assert calls == ["finished"]


@pytest.mark.parametrize("kind", ["backup", "strategy"])
async def test_archive_inspection_and_reload_leave_loop_responsive(monkeypatch, kind):
    from app.api import config_backup, strategy

    check = require_responsive_loop()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=SimpleNamespace(reload=check),
    )))

    def inspect(data):
        check()
        assert data == b"archive"
        return {"items": []}

    def apply(data, **kwargs):
        check()
        assert data == b"archive"
        return {"reload_hints": {"strategies": True}}

    if kind == "backup":
        monkeypatch.setattr(config_backup.config_backup, "inspect_archive", inspect)
        monkeypatch.setattr(config_backup.config_backup, "apply_archive", apply)
        assert await config_backup.inspect_backup(UploadFile(io.BytesIO(b"archive"))) == {"items": []}
        result = await config_backup.apply_backup(request, UploadFile(io.BytesIO(b"archive")), False, "")
    else:
        monkeypatch.setattr(strategy._bundle_service(), "inspect_bundle", inspect)
        monkeypatch.setattr(strategy._bundle_service(), "apply_bundle", apply)
        assert await strategy.inspect_strategy_bundle(UploadFile(io.BytesIO(b"archive"))) == {"items": []}
        result = await strategy.apply_strategy_bundle(request, UploadFile(io.BytesIO(b"archive")), False)
    assert result["reload_hints"]["strategies"] is True


async def test_cancelled_parser_cleans_its_own_temporary_files(tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    paths = []

    def parse(path, **kwargs):
        paths.append(Path(path))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        assert paths[0].exists(), "request cancellation deleted the worker's input"
        return pl.DataFrame({"symbol": ["600000.SH"]})

    monkeypatch.setattr(ext_data.pl, "read_csv", parse)
    original_mkdtemp = ext_data.tempfile.mkdtemp
    monkeypatch.setattr(ext_data.tempfile, "mkdtemp", lambda: original_mkdtemp(dir=tmp_path))
    task = asyncio.create_task(ext_data.detect_fields(None, UploadFile(io.BytesIO(b"symbol\n600000.SH\n"), filename="x.csv")))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert paths[0].exists()
    finally:
        release.set()
        # Same limiter: this completes only after the parser and its finally block.
        await run_ext_data_io(lambda: None)
    assert not paths[0].parent.exists()
