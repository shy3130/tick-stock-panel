from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.sync_financials_pg_to_clickhouse import (
    FinancialMigration,
    MigrationState,
    normalize_source_row,
)


def test_normalize_source_row_is_clickhouse_json_safe_and_omits_large_columns() -> None:
    source = {
        "id": 123,
        "symbol": "700.HK",
        "market": "hk",
        "report_type": "IS",
        "report_period": "Q1 2026",
        "fiscal_year": 2026,
        "fp_end": date(2026, 3, 30),
        "field": "OperatingRevenue",
        "name": "营业收入(HKD)",
        "value": Decimal("222732641073.4178"),
        "ratio": "",
        "yoy": "15.52561599260133",
        "percent": False,
        "industry_ranking": "1/32",
        "ranking_code": "REV",
        "currency": "HKD",
        "payload": {"large": "raw-json"},
        "updated_at": datetime(
            2026,
            5,
            14,
            21,
            1,
            21,
            123000,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    }

    normalized = normalize_source_row(source)

    assert normalized == {
        "symbol": "700.HK",
        "market": "hk",
        "report_type": "IS",
        "report_period": "Q1 2026",
        "fiscal_year": 2026,
        "fp_end": "2026-03-30",
        "field": "OperatingRevenue",
        "name": "营业收入(HKD)",
        "value": 222732641073.4178,
        "ratio": None,
        "yoy": 15.52561599260133,
        "percent": False,
        "industry_ranking": "1/32",
        "ranking_code": "REV",
        "currency": "HKD",
        "updated_at": "2026-05-14 21:01:21.123",
    }
    assert "id" not in normalized
    assert "payload" not in normalized


def _migration_row(row_id: int, updated_at: datetime) -> dict:
    return {
        "id": row_id,
        "symbol": f"S{row_id}.US",
        "market": "us",
        "report_type": "IS",
        "report_period": "Q1 2026",
        "fiscal_year": 2026,
        "fp_end": date(2026, 3, 31),
        "field": "EPS",
        "name": "EPS",
        "value": row_id,
        "ratio": None,
        "yoy": None,
        "percent": False,
        "industry_ranking": "",
        "ranking_code": "",
        "currency": "USD",
        "updated_at": updated_at,
    }


class MemoryCheckpoint:
    def __init__(self, state: MigrationState | None = None) -> None:
        self.state = state or MigrationState()
        self.saved: list[MigrationState] = []

    def load(self) -> MigrationState:
        return self.state

    def save(self, state: MigrationState) -> None:
        self.state = state
        self.saved.append(state)


class FakeSource:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.full_starts: list[int] = []
        self.incremental_starts: list[datetime | None] = []

    def iter_full(self, last_id: int, batch_size: int):
        self.full_starts.append(last_id)
        remaining = [row for row in self.rows if row["id"] > last_id]
        for offset in range(0, len(remaining), batch_size):
            yield remaining[offset : offset + batch_size]

    def iter_incremental(self, start: datetime | None, batch_size: int):
        self.incremental_starts.append(start)
        yield from ()


def test_full_migration_saves_checkpoint_only_after_successful_batch() -> None:
    base = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    source = FakeSource([_migration_row(i, base + timedelta(minutes=i)) for i in range(1, 4)])
    checkpoint = MemoryCheckpoint()
    calls = 0

    def fail_second_batch(rows: list[dict]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("write failed")

    migration = FinancialMigration(source, fail_second_batch, checkpoint, batch_size=2)

    with pytest.raises(RuntimeError, match="write failed"):
        migration.run()

    assert checkpoint.state.full_complete is False
    assert checkpoint.state.last_id == 2

    written: list[dict] = []
    resumed = FinancialMigration(source, written.extend, checkpoint, batch_size=2)
    summary = resumed.run()

    assert source.full_starts[-1] == 2
    assert [row["symbol"] for row in written] == ["S3.US"]
    assert checkpoint.state.full_complete is True
    assert summary.rows == 1


def test_incremental_migration_uses_five_minute_overlap() -> None:
    latest = datetime(2026, 7, 19, 1, 0, tzinfo=UTC)
    source = FakeSource([])
    checkpoint = MemoryCheckpoint(
        MigrationState(full_complete=True, last_id=100, updated_at=latest)
    )

    FinancialMigration(source, lambda _rows: None, checkpoint, batch_size=10).run()

    assert source.incremental_starts == [latest - timedelta(minutes=5)]
