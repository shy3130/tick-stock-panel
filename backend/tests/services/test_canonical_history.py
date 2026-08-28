from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import data as data_api
from app.services import canonical_history


class FakeProvider:
    def __init__(self, *, fail: bool = False, gate: threading.Event | None = None) -> None:
        self.fail = fail
        self.gate = gate
        self.entered = threading.Event()
        self.closed = False

    def get_instruments(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame(
            {
                "symbol": ["000001.SZ", "600519.SH"],
                "name": ["平安银行", "贵州茅台"],
                "float_shares": [1_000_000_000.0, 100_000_000.0],
                "total_shares": [2_000_000_000.0, 120_000_000.0],
            }
        )

    def get_daily(self, symbols, start, end, asset_type):
        assert asset_type in {"stock", "index"}
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=5)
        if self.fail:
            raise RuntimeError("injected provider failure")
        days = (end.date() - start.date()).days + 1
        rows = []
        for symbol in symbols:
            for index in range(days):
                value = 10.0 + index
                rows.append(
                    {
                        "symbol": symbol,
                        "date": start.date() + timedelta(days=index),
                        "open": value,
                        "high": value + 0.5,
                        "low": value - 0.5,
                        "close": value,
                        "volume": 1_000.0,
                        "amount": 10_000.0,
                    }
                )
        return pl.DataFrame(rows)

    def get_adj_factors(self, symbols, start, end, asset_type):
        return pl.DataFrame()

    def close(self) -> None:
        self.closed = True


def _install_provider(monkeypatch, provider: FakeProvider, base) -> None:
    published = base / "published"
    for logical in canonical_history._REQUIRED_SNAPSHOT_LOGICALS:
        generation = published / logical / "20260812T000000"
        generation.mkdir(parents=True, exist_ok=True)
        (generation / f"{logical}.duckdb").write_bytes(b"snapshot")
        (generation / "manifest.json").write_text(json.dumps({"generation": "20260812T000000", "entries": [{"logical": logical, "file": f"{logical}.duckdb"}]}))
    monkeypatch.setattr(canonical_history, "get_active_provider_name", lambda _cap: "fquant_local")
    monkeypatch.setattr(canonical_history, "get_provider", lambda _name, **_kwargs: provider)
    monkeypatch.setattr(canonical_history, "current_path", lambda logical: str(published / logical / "20260812T000000" / f"{logical}.duckdb"))


def _join(manager: canonical_history.CanonicalHistoryManager) -> None:
    thread = manager._thread
    assert thread is not None
    thread.join(timeout=30)
    assert not thread.is_alive()


def test_status_does_not_create_unpublished_root(tmp_path):
    root = tmp_path / "external-history"
    manager = canonical_history.CanonicalHistoryManager(root)

    status = manager.status()

    assert status["available"] is False
    assert status["reason"] == "not_published"
    assert not root.exists()


def test_backfill_rejects_root_inside_user_data(tmp_path, monkeypatch):
    from app.config import settings

    user_data = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", user_data)
    manager = canonical_history.CanonicalHistoryManager(user_data / "canonical-history")

    with pytest.raises(RuntimeError, match="outside DATA_DIR"):
        manager.start()

    assert not user_data.exists()


def test_backfill_rejects_invalid_worker_count(tmp_path):
    manager = canonical_history.CanonicalHistoryManager(tmp_path / "external-history")

    with pytest.raises(ValueError, match="workers must be between 1 and 8"):
        manager.start(workers=9)


def test_status_marks_orphaned_running_job_failed(tmp_path):
    root = tmp_path / "external-history"
    root.mkdir()
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "job_id": "orphaned",
                "started_at": "2026-08-11T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = canonical_history.CanonicalHistoryManager(root).status()

    assert status["status"] == "failed"
    assert status["error"] == "canonical history backfill was interrupted"
    assert status["finished_at"]


def test_backfill_publishes_actual_coverage_outside_user_data(tmp_path, monkeypatch):
    root = tmp_path / "external-history"
    user_data = tmp_path / "data"
    user_data.mkdir()
    sentinel = user_data / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    provider = FakeProvider()
    _install_provider(monkeypatch, provider, tmp_path)
    manager = canonical_history.CanonicalHistoryManager(root)

    started = manager.start(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
        batch_size=1,
        workers=2,
    )
    _join(manager)

    status = manager.status()
    assert started["status"] == "running"
    assert status["status"] == "succeeded"
    assert status["available"] is True
    assert status["manifest"]["start_date"] == "2024-01-02"
    assert status["manifest"]["end_date"] == "2024-01-04"
    assert status["manifest"]["trading_days"] == 3
    assert status["manifest"]["symbols"] == 2
    assert status["manifest"]["rows"] == 6
    assert status["manifest"]["workers"] == 2
    identities = status["manifest"]["source_generations"]
    assert set(identities) == {"tdx", "fstore", "extended", "markets", "klines"}
    assert all(set(value) == {"generation", "manifest_sha256"} for value in identities.values())
    assert all(value["generation"] == "20260812T000000" for value in identities.values())
    published = canonical_history.resolve_published_history(root)
    assert published is not None
    assert list(published[1].glob("date=*/*.parquet"))
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert provider.closed is True


def test_incremental_publish_clones_parent_and_copies_validated_local_partition(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "external-history"
    provider = FakeProvider()
    _install_provider(monkeypatch, provider, tmp_path)
    manager = canonical_history.CanonicalHistoryManager(root)
    manager.start(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        batch_size=2,
    )
    _join(manager)
    parent = manager.status()["generation"]

    data_dir = tmp_path / "data"
    local_partition = data_dir / "kline_daily_enriched" / "date=2024-01-04"
    local_partition.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ", "600519.SH"],
            "date": [date(2024, 1, 4), date(2024, 1, 4)],
            "open": [12.0, 12.0],
            "high": [12.5, 12.5],
            "low": [11.5, 11.5],
            "close": [12.0, 12.0],
            "volume": [1_000.0, 1_000.0],
            "amount": [12_000.0, 12_000.0],
            "raw_open": [12.0, 12.0],
            "raw_close": [12.0, 12.0],
            "raw_high": [12.5, 12.5],
            "raw_low": [11.5, 11.5],
            "turnover_rate": [0.1, 0.1],
            "consecutive_limit_ups": [0, 0],
            "consecutive_limit_downs": [0, 0],
        }
    ).write_parquet(local_partition / "part.parquet")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))

    result = manager.publish_incremental_from_local(repo, date(2024, 1, 4))

    assert result["status"] == "succeeded"
    published = canonical_history.resolve_published_history(root)
    assert published is not None
    manifest, generation_dir = published
    assert manifest["parent_generation"] == parent
    assert manifest["update_type"] == "incremental_local_partitions"
    assert set(manifest["source_generations"]) == {
        "tdx",
        "fstore",
        "markets",
        "klines",
        "extended",
    }
    assert set(manifest["calendar_source_generations"]) == {"tdx", "markets"}
    assert manifest["end_date"] == "2024-01-04"
    assert manifest["incremental_partitions"]["2024-01-04"]["rows"] == 2
    assert list((generation_dir / "date=2024-01-02").glob("*.parquet"))
    assert list((generation_dir / "date=2024-01-04").glob("*.parquet"))
    assert (root / "generations" / parent).is_dir()


    gap_partition = data_dir / "kline_daily_enriched" / "date=2024-01-06"
    gap_partition.mkdir()
    (
        pl.read_parquet(local_partition / "part.parquet")
        .with_columns(pl.lit(date(2024, 1, 6)).alias("date"))
        .write_parquet(gap_partition / "part.parquet")
    )

    gap_result = manager.publish_incremental_from_local(repo, date(2024, 1, 6))

    assert gap_result == {
        "status": "skipped",
        "reason": "calendar_partition_mismatch",
        "missing_dates": ["2024-01-05"],
        "unexpected_dates": [],
    }

def test_failed_rebuild_keeps_previous_current_generation(tmp_path, monkeypatch):
    root = tmp_path / "external-history"
    good = FakeProvider()
    _install_provider(monkeypatch, good, tmp_path)
    manager = canonical_history.CanonicalHistoryManager(root)
    manager.start(start_date=date(2024, 1, 2), end_date=date(2024, 1, 3), batch_size=2)
    _join(manager)
    previous = (root / "current.json").read_text(encoding="utf-8")

    failing = FakeProvider(fail=True)
    _install_provider(monkeypatch, failing, tmp_path)
    manager.start(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        batch_size=1,
        workers=2,
    )
    _join(manager)

    status = manager.status()
    assert status["status"] == "failed"
    assert status["available"] is True
    assert "injected provider failure" in status["error"]
    assert (root / "current.json").read_text(encoding="utf-8") == previous


def test_duplicate_start_reuses_active_job(tmp_path, monkeypatch):
    gate = threading.Event()
    provider = FakeProvider(gate=gate)
    _install_provider(monkeypatch, provider, tmp_path)
    manager = canonical_history.CanonicalHistoryManager(tmp_path / "external-history")

    first = manager.start(start_date=date(2024, 1, 2), end_date=date(2024, 1, 2))
    assert provider.entered.wait(timeout=5)
    second = manager.start(start_date=date(2024, 1, 2), end_date=date(2024, 1, 2))
    assert second["job_id"] == first["job_id"]

    gate.set()
    _join(manager)
    assert manager.status()["status"] == "succeeded"


def test_invalid_generation_pointer_is_not_readable(tmp_path):
    root = tmp_path / "external-history"
    root.mkdir()
    (root / "current.json").write_text(
        json.dumps(
            {
                "generation": "20260812T000000-deadbeef",
                "path": "../outside",
            }
        ),
        encoding="utf-8",
    )

    assert canonical_history.resolve_published_history(root) is None
    status = canonical_history.CanonicalHistoryManager(root).status()
    assert status["available"] is False
    assert status["reason"] == "invalid_generation_path"


def test_api_status_shape_allows_first_backfill(monkeypatch):
    received = {}

    class Manager:
        def status(self):
            return {
                "status": "idle",
                "available": False,
                "reason": "not_published",
            }

        def start(self, **kwargs):
            received.update(kwargs)
            return {"job_id": "job-1", "status": "running"}

    manager = Manager()
    monkeypatch.setattr(canonical_history, "canonical_history_manager", lambda: manager)

    status = data_api.canonical_history_status()
    started = data_api.canonical_history_backfill(
        data_api.CanonicalHistoryBackfillRequest(batch_size=10, workers=4)
    )

    assert status == {
        "available": False,
        "reason": "not_published",
        "published": None,
        "job": None,
    }
    assert started == {"job_id": "job-1", "status": "running"}
    assert received["workers"] == 4
def test_incremental_identity_failure_keeps_current(tmp_path, monkeypatch):
    root = tmp_path / "external-history"
    provider = FakeProvider()
    _install_provider(monkeypatch, provider, tmp_path)
    manager = canonical_history.CanonicalHistoryManager(root)
    manager.start(start_date=date(2024, 1, 2), end_date=date(2024, 1, 3), batch_size=2)
    _join(manager)
    before = (root / "current.json").read_bytes()
    broken = tmp_path / "published" / "markets" / "20260812T000000" / "manifest.json"
    broken.write_text(json.dumps({"generation": "broken", "entries": []}))
    data_dir = tmp_path / "data"
    partition = data_dir / "kline_daily_enriched" / "date=2024-01-04"
    partition.mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "date": [date(2024, 1, 4)], "open": [12.0], "high": [12.5], "low": [11.5], "close": [12.0], "volume": [1000.0], "amount": [12000.0], "raw_open": [12.0], "raw_close": [12.0], "raw_high": [12.5], "raw_low": [11.5], "turnover_rate": [0.1], "consecutive_limit_ups": [0], "consecutive_limit_downs": [0]}).write_parquet(partition / "part.parquet")
    with pytest.raises(RuntimeError, match="calendar snapshot identity unavailable"):
        manager.publish_incremental_from_local(SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)), date(2024, 1, 4))
    assert (root / "current.json").read_bytes() == before
