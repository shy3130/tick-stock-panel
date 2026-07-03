from app.data_providers.fquant.symbols import asset_type_str_to_nums, code_to_symbol, is_a_stock, symbol_to_market
from app.data_providers.fquant_provider import FQuantProvider


def test_stock_asset_type_excludes_hk():
    assert asset_type_str_to_nums("stock") == [1]
    assert asset_type_str_to_nums("hk") == [3]
    assert symbol_to_market("00700.HK") == (3, "hk")


def test_etf_symbols_use_exchange_suffixes():
    assert code_to_symbol("513050", 20) == "513050.SH"
    assert code_to_symbol("159915", 20) == "159915.SZ"


def test_exchange_suffixed_etf_reverse_mapping():
    assert FQuantProvider._asset_type_num_for_symbol("513050.SH") == 20
    assert FQuantProvider._asset_type_num_for_symbol("159915.SZ") == 20
    assert FQuantProvider._asset_type_num_for_symbol("516110.SH") == 20
    assert FQuantProvider._asset_type_num_for_symbol("510300.ETF") == 20
    assert FQuantProvider._asset_type_num_for_symbol("600519.SH") == 1
    assert FQuantProvider._asset_type_num_for_symbol("000001.SZ") == 1
    assert FQuantProvider._asset_type_num_for_symbol("128012.SZ") == 1
    assert FQuantProvider._asset_type_num_for_symbol("127045.SZ") == 1
    assert FQuantProvider._asset_type_num_for_symbol("000300.INDEX") == 10
    assert FQuantProvider._asset_type_num_for_symbol("00700.HK") == 3

    assert symbol_to_market("513050.SH") == (20, "etf")
    assert symbol_to_market("159915.SZ") == (20, "etf")
    assert symbol_to_market("516110.SH") == (20, "etf")
    assert symbol_to_market("600519.SH") == (1, "a")
    assert symbol_to_market("000001.SZ") == (1, "a")
    assert symbol_to_market("128012.SZ") == (1, "a")
    assert symbol_to_market("127045.SZ") == (1, "a")
    assert symbol_to_market("00700.HK") == (3, "hk")
    assert symbol_to_market("000300.INDEX") is None

    assert not is_a_stock("513050.SH")
    assert not is_a_stock("159915.SZ")
    assert not is_a_stock("516110.SH")
    assert is_a_stock("600519.SH")
    assert is_a_stock("000001.SZ")
    assert is_a_stock("128012.SZ")
    assert is_a_stock("127045.SZ")
