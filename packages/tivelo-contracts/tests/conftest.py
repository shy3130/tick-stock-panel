from datetime import UTC, datetime

import pytest

from tivelo_contracts.data import Bar, Instrument, KnownDate


@pytest.fixture
def make_bar():
    def factory(**changes):
        values = dict(
            instrument_id="security-1",
            timeframe="1d",
            bar_start="2025-01-02T01:30:00Z",
            bar_end="2025-01-02T07:00:00Z",
            trading_date="2025-01-02",
            session_id="cn.day",
            open="10.1",
            high="10.5",
            low="10",
            close="10.2",
            volume="10000",
            quantity_unit="share",
            quote_currency="CNY",
            turnover="102000",
            price_basis="raw",
            available_at="2025-01-02T07:00:00Z",
            ingested_at="2025-01-02T07:01:00Z",
            source_id="synthetic",
            revision_id="r1",
        )
        values.update(changes)
        return Bar(**values)

    return factory


@pytest.fixture
def make_instrument():
    def factory(**changes):
        values = dict(
            instrument_id="security-1",
            venue_id="XSHG",
            market_id="cn.equity",
            asset_class="equity",
            instrument_type="common_stock",
            display_symbol="600000",
            name="Synthetic Security",
            quote_currency="CNY",
            settlement_currency="CNY",
            calendar_id="XSHG",
            timezone="Asia/Shanghai",
            tradable=True,
            spec_revision="v1",
            listing_date=KnownDate(state="known", value="2020-01-01"),
            delisting_date=KnownDate(state="not_yet"),
            valid_from="2020-01-01T00:00:00Z",
            available_at="2020-01-01T00:00:00Z",
            revision_id="r1",
            source_id="synthetic",
        )
        values.update(changes)
        return Instrument(**values)

    return factory


@pytest.fixture
def now():
    return datetime(2025, 1, 3, tzinfo=UTC)
