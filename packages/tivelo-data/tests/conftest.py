from datetime import UTC, datetime

import pytest
from tivelo_contracts.data import Bar, Coverage

from tivelo_data.engine import DataEngine
from tivelo_data.providers import (
    FetchPage,
    FixedSourceResolver,
    ProviderCapability,
    ProviderDescriptor,
    SyncRequest,
)


@pytest.fixture
def make_bar():
    def create(**changes):
        values = dict(
            instrument_id="security-1",
            timeframe="1d",
            bar_start="2025-01-02T01:30:00Z",
            bar_end="2025-01-02T07:00:00Z",
            trading_date="2025-01-02",
            session_id="cn.synthetic.day",
            open="10",
            high="12",
            low="9",
            close="11",
            volume="1000",
            quantity_unit="share",
            quote_currency="CNY",
            turnover="11000",
            price_basis="raw",
            available_at="2025-01-02T07:00:00Z",
            ingested_at="2025-01-02T07:01:00Z",
            source_id="fixture",
            revision_id="r1",
        )
        values.update(changes)
        return Bar(**values)

    return create


class MemoryProvider:
    def __init__(
        self,
        records,
        *,
        record_type="bar",
        page_size=2,
        coverage=None,
        knowledge_basis="historical_versions",
    ):
        self.records = tuple(records)
        self.page_size = page_size
        self.calls = 0
        self.closed = 0
        self.coverage = coverage
        self.record_type = record_type
        self.descriptor = ProviderDescriptor(
            provider_id="fixture",
            version="1",
            source_ids=("fixture",),
            capabilities=(
                ProviderCapability(
                    record_type=record_type,
                    market_id="cn.equity",
                    asset_class="equity",
                    timeframes=("1d",) if record_type == "bar" else (),
                    price_bases=("raw", "qfq", "hfq") if record_type == "bar" else (),
                    knowledge_basis=knowledge_basis,
                    normalization_version="fixture-v1",
                    bar_convention_id="synthetic.day" if record_type == "bar" else None,
                ),
            ),
        )

    def describe(self):
        return self.descriptor

    def check_ready(self, context):
        context.check_cancelled()

    def fetch(self, request, cursor, context):
        self.calls += 1
        start = int(cursor) if cursor is not None else 0
        end = start + self.page_size
        final = end >= len(self.records)
        coverage = self.coverage or Coverage(
            status="complete",
            requested_units=1,
            completed_units=1,
            row_count=len(self.records),
        )
        return FetchPage(
            self.records[start:end], None if final else str(end), coverage if final else None
        )

    def close(self):
        self.closed += 1


@pytest.fixture
def make_engine(tmp_path):
    def create(records, *, record_type="bar", directory=None, **options):
        provider = MemoryProvider(records, record_type=record_type, **options)
        engine = DataEngine(
            directory or tmp_path / "store", FixedSourceResolver(provider, "a" * 64)
        )
        request = SyncRequest(
            dataset_id=record_type,
            record_type=record_type,
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=tuple(sorted({r.instrument_id for r in records})) or ("security-1",),
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 2, 1, tzinfo=UTC),
            timeframe="1d" if record_type == "bar" else None,
            price_basis=records[0].price_basis
            if record_type == "bar" and records
            else ("raw" if record_type == "bar" else None),
        )
        return engine, provider, engine.plan_sync(request)

    return create
