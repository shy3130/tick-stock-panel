from app.data_providers.fquant.raw_reconstruct import reconstruct_raw_rows


def test_inverse_adjusts_rows_before_event():
    rows = [{"date": "2024-01-01", "open": 9.0, "high": 10.0, "low": 8.0, "close": 9.5}]
    events = [{"trade_date": "2024-02-01", "category": 1, "fenhong": 10}]

    out = reconstruct_raw_rows(rows, events)

    assert out[0]["open"] == 10.0
    assert out[0]["close"] == 10.5


def test_oracle_overrides_inverse_prices_only():
    rows = [{
        "date": "2012-10-26",
        "open": -105.170712,
        "high": -104.955836,
        "low": -112.319473,
        "close": -111.476498,
        "volume": 2_971_600,
    }]
    events = [{"trade_date": "2024-06-19", "category": 1, "fenhong": 30}]
    oracle = [{
        "date": "2012-10-26",
        "oracle_open": 248.72,
        "oracle_high": 248.98,
        "oracle_low": 240.07,
        "oracle_close": 241.0,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["open"] == 248.72
    assert out[0]["close"] == 241.0
    assert out[0]["volume"] == 2_971_600


def test_rows_without_oracle_keep_inverse_result():
    rows = [{"date": "2024-01-01", "open": 9.0, "high": 10.0, "low": 8.0, "close": 9.5}]
    events = [{"date": "2024-02-01", "category": 1, "songzhuangu": 10}]

    out = reconstruct_raw_rows(rows, events, oracle_rows=[])

    assert out[0]["open"] == 18.0
    assert out[0]["high"] == 20.0


def test_oracle_merges_volume_and_amount():
    rows = [{
        "date": "2012-10-26",
        "open": -105.170712,
        "high": -104.955836,
        "low": -112.319473,
        "close": -111.476498,
        "volume": -1,
        "amount": -1,
    }]
    events = [{"trade_date": "2024-06-19", "category": 1, "fenhong": 30}]
    oracle = [{
        "date": "2012-10-26",
        "oracle_open": 248.72,
        "oracle_high": 248.98,
        "oracle_low": 240.07,
        "oracle_close": 241.0,
        "oracle_volume": 1_000_000,
        "oracle_amount": 5_000_000_000,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["volume"] == 1_000_000
    assert out[0]["amount"] == 5_000_000_000


def test_inverse_scales_volume_on_share_change():
    rows = [{"date": "2024-01-01", "open": 9.0, "high": 10.0, "low": 8.0, "close": 9.5, "volume": 2000}]
    events = [{"date": "2024-02-01", "category": 1, "songzhuangu": 10}]

    out = reconstruct_raw_rows(rows, events, oracle_rows=[])

    assert out[0]["volume"] == 1000

    cash_rows = [{"date": "2024-01-01", "open": 9.0, "high": 10.0, "low": 8.0, "close": 9.5, "volume": 2000}]
    cash_events = [{"date": "2024-02-01", "category": 1, "fenhong": 10}]

    cash_out = reconstruct_raw_rows(cash_rows, cash_events, oracle_rows=[])

    assert cash_out[0]["volume"] == 2000


# --------------------------------------------------------------------------- #
# volume=0 dirty-data guard (fstore daily_markets stale Cjl=0)
# --------------------------------------------------------------------------- #
def test_oracle_dirty_zero_volume_with_positive_amount_keeps_engine_volume():
    """Oracle volume=0 paired with amount>0 is physically impossible dirty data
    (fstore daily_markets carries stale Cjl=0 on real trading days). The
    engine/base volume must be kept, not overwritten."""
    rows = [{
        "date": "2024-03-01",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 1_000_000, "amount": 10_500_000,
    }]
    events = []
    oracle = [{
        "date": "2024-03-01",
        "oracle_volume": 0,
        "oracle_amount": 10_500_000,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["volume"] == 1_000_000


def test_oracle_dirty_zero_volume_kept_when_only_merged_amount_positive():
    """Even when oracle amount is itself zero/absent, a positive engine amount
    signals real trading — a zero oracle volume is still dirty."""
    rows = [{
        "date": "2024-03-01",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 800_000, "amount": 8_400_000,
    }]
    events = []
    oracle = [{
        "date": "2024-03-01",
        "oracle_volume": 0,
        "oracle_amount": 0,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["volume"] == 800_000


def test_oracle_positive_volume_still_overrides():
    """Normal positive oracle volume must still override the engine value."""
    rows = [{
        "date": "2024-03-01",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 1_000_000, "amount": 10_500_000,
    }]
    events = []
    oracle = [{
        "date": "2024-03-01",
        "oracle_volume": 2_000_000,
        "oracle_amount": 21_000_000,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["volume"] == 2_000_000


def test_oracle_true_zero_volume_zero_amount_overrides():
    """Genuine halt: oracle volume=0 AND amount=0 AND engine amount=0. The
    guard must not fire — semantics preserved, volume overridden to 0."""
    rows = [{
        "date": "2024-03-01",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 1_000_000, "amount": 0,
    }]
    events = []
    oracle = [{
        "date": "2024-03-01",
        "oracle_volume": 0,
        "oracle_amount": 0,
    }]

    out = reconstruct_raw_rows(rows, events, oracle)

    assert out[0]["volume"] == 0


# --------------------------------------------------------------------------- #
# Oracle table priority: t_1_day_klines wins over daily_markets same-date
# --------------------------------------------------------------------------- #
def test_oracle_day_klines_wins_over_daily_markets_same_date():
    """t_1_day_klines is the dedicated daily-K oracle; daily_markets must only
    fill dates missing from day_klines, never overwrite a same-date row."""
    from app.data_providers.fquant_provider import FQuantProvider

    class _FakeFStore:
        def __init__(self) -> None:
            self.day_rows: list[dict] = []
            self.market_rows: list[dict] = []
            self.market_queries = 0

        def query(self, sql: str, params=None):
            up = sql.upper()
            if "T_1_DAY_KLINES" in up:
                return self.day_rows
            if "DAILY_MARKETS" in up:
                self.market_queries += 1
                return self.market_rows
            return []

    fake = _FakeFStore()
    # Both tables have 2024-03-01 with conflicting values — day_klines wins.
    fake.day_rows = [
        {"date": "2024-03-01", "oracle_close": 100.0, "oracle_volume": 5000},
    ]
    fake.market_rows = [
        {"date": "2024-03-01", "oracle_close": 999.0, "oracle_volume": 0},
    ]

    provider = FQuantProvider.__new__(FQuantProvider)
    provider._fstore = fake
    out = provider._get_raw_oracle_rows("600519", [{"date": "2024-03-01"}])

    assert len(out) == 1
    assert out[0]["oracle_close"] == 100.0
    assert out[0]["oracle_volume"] == 5000
    assert fake.market_queries == 0

    # daily_markets fills only the date absent from day_klines.
    fake.day_rows = [{"date": "2024-03-01", "oracle_close": 100.0}]
    fake.market_rows = [
        {"date": "2024-03-01", "oracle_close": 999.0},
        {"date": "2024-03-02", "oracle_close": 200.0},
    ]
    out = provider._get_raw_oracle_rows(
        "600519", [{"date": "2024-03-01"}, {"date": "2024-03-02"}],
    )

    assert [r["date"] for r in out] == ["2024-03-01", "2024-03-02"]
    assert out[0]["oracle_close"] == 100.0   # day_klines wins
    assert out[1]["oracle_close"] == 200.0   # daily_markets fills missing date
    assert fake.market_queries == 1
