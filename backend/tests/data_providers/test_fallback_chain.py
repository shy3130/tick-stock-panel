from app.data_providers.fquant.fallback import get_fallback_chain


def test_fallback_chain_matches_local_disk_sources():
    assert get_fallback_chain("get_moneyflow_daily") == ["duckdb:market_fund_flow"]
    assert get_fallback_chain("get_realtime") == ["duckdb:daily_markets"]
    assert get_fallback_chain("get_moneyflow_minute") == []
