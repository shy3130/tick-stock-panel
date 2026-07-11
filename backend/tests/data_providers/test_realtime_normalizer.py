from app.data_providers.fquant_provider import FQuantProvider
from app.data_providers.normalizer import REALTIME_COLS, normalize_realtime


class FakeFStore:
    def __init__(self, rows=None):
        self.rows = rows or []

    def query(self, sql, params=None):  # noqa: ARG002
        return self.rows


def test_normalize_realtime_preserves_quote_service_contract():
    df = normalize_realtime([{
        "symbol": "600519.sh",
        "price": "1500.5",
        "pre_close": "1490",
        "change_pct": 0.7,
    }], source="unit")

    assert df.columns == REALTIME_COLS
    row = df.to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1500.5
    assert row["prev_close"] == 1490.0
    assert row["source"] == "unit"
    assert row["ext"]["change_pct"] == 0.7


def test_fquant_realtime_symbols_use_duckdb_snapshot_and_normalizer():
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant_local"
    provider._fstore = FakeFStore([{
        "code": "600519",
        "name": "贵州茅台",
        "tdate": "2026-07-03",
        "price": 1185.49,
        "zrspj": 1180.0,
        "cjl": 34268,
    }])

    df = provider.get_realtime(symbols=["600519.SH"])

    assert df.columns == REALTIME_COLS
    assert df["source"][0] == "fquant_local:fstore:daily_markets"


def test_fstore_quote_source_uses_provider_name():
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant_local"

    row = provider._fstore_quote_to_row({"code": "600519", "price": 1185.49, "cjl": 34268}, 1)

    assert row["source"] == "fquant_local:fstore:daily_markets"
    assert row["volume"] == 3_426_800


def test_latest_market_supplements_returns_ratio_fields():
    provider = object.__new__(FQuantProvider)
    provider._get_fstore_realtime = lambda symbols: [{
        "symbol": symbols[0],
        "timestamp": "2026-07-03",
        "ext": {"change_pct": 0.6, "amplitude": 5.67, "turnover_rate": 1.86},
    }]

    row = provider.get_latest_market_supplements(["300492.SZ"]).to_dicts()[0]

    assert row["change_pct"] == 0.006
    assert row["amplitude"] == 0.0567
    assert row["turnover_rate"] == 1.86
