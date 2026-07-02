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
