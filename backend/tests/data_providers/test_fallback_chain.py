from app.data_providers.fquant.fallback import get_fallback_chain


def test_fallback_chain_matches_local_disk_sources():
    assert get_fallback_chain("get_moneyflow_daily") == ["tdx-disk:fund", "moneyflow:daily"]
    assert get_fallback_chain("get_realtime") == [
        "tdx-api:quote",
        "sina/tencent:quote",
        "fstore:daily_markets",
    ]
