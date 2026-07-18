import pytest

from app.market_rules import (
    market_for_symbol,
    market_rule_for_symbol,
    round_lot_size,
)


def test_market_rules_cover_cn_hk_us() -> None:
    assert market_for_symbol("000001.SZ") == "cn"
    assert market_for_symbol("600000.SH") == "cn"
    assert market_for_symbol("920000.BJ") == "cn"
    assert market_for_symbol("1.HK") == "hk"
    assert market_for_symbol("A.US") == "us"

    assert market_rule_for_symbol("000001.SZ").currency == "CNY"
    assert market_rule_for_symbol("1.HK").same_day_sell_allowed is True
    assert market_rule_for_symbol("A.US").price_limit_policy == "none"


def test_round_lot_uses_market_default_and_hk_metadata() -> None:
    assert round_lot_size("000001.SZ") == 100
    assert round_lot_size("A.US") == 1
    assert round_lot_size("1.HK") == 1
    assert round_lot_size("1.HK", {"lot_size": 500}) == 500


def test_unknown_symbol_suffix_is_rejected() -> None:
    with pytest.raises(ValueError, match="无法识别证券市场"):
        market_for_symbol("UNKNOWN")


@pytest.mark.parametrize("value", [0, -1, "", "bad", None])
def test_invalid_lot_metadata_falls_back_to_market_default(value) -> None:
    assert round_lot_size("1.HK", {"lot_size": value}) == 1
