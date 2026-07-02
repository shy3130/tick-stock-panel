from app.data_providers.fquant_provider import FQuantProvider
from app.data_providers.normalizer import REALTIME_COLS, normalize_realtime


class FakeSinaTencent:
    def __init__(self):
        self.calls = []

    def get_quotes(self, symbols, prefer="tencent"):
        self.calls.append((list(symbols), prefer))
        return [{
            "symbol": symbols[0],
            "last_price": 10.0,
            "volume": 100.0,
            "source": prefer,
            "ext": {},
        }]


class FakeFStore:
    def query(self, sql, params=None):
        return []


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


def test_realtime_source_cools_down_after_three_failures():
    provider = object.__new__(FQuantProvider)
    provider._realtime_failures = {}
    provider._realtime_cooldown_until = {}

    provider._record_realtime_failure("tdx-api")
    provider._record_realtime_failure("tdx-api")
    assert provider._realtime_source_available("tdx-api")

    provider._record_realtime_failure("tdx-api")
    assert not provider._realtime_source_available("tdx-api")

    provider._record_realtime_success("tdx-api")
    assert provider._realtime_source_available("tdx-api")


def test_fquant_realtime_symbols_use_tencent_and_normalizer():
    provider = object.__new__(FQuantProvider)
    provider._tdx_api_base = ""
    provider._sina_tencent = FakeSinaTencent()
    provider._fstore = FakeFStore()

    df = provider.get_realtime(symbols=["600519.SH"])

    assert provider._sina_tencent.calls == [(["600519.SH"], "tencent")]
    assert df.columns == REALTIME_COLS
    assert df["source"][0] == "tencent"


def test_tdx_quote_source_uses_provider_name():
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant_local"

    row = provider._tdx_quote_to_row({
        "Code": "SH600519",
        "TotalHand": 1,
        "Amount": 2,
        "K": {"Last": 1180000, "Close": 1185490},
    })

    assert row["source"] == "fquant_local:tdx-api:/api/quote"


def test_fstore_quote_source_uses_provider_name():
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant_local"

    row = provider._fstore_quote_to_row({"code": "600519", "price": 1185.49}, 1)

    assert row["source"] == "fquant_local:fstore:daily_markets"
