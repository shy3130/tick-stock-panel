from datetime import datetime, time

from app.markets import market_of


def test_a_share_profile():
    p = market_of("600519.SH")
    assert p.market == "a_share"
    assert p.has_price_limit is True
    assert p.adjustment == "xdxr"
    assert p.timezone == "Asia/Shanghai"


def test_hk_profile():
    p = market_of("00700.HK")
    assert p.market == "hk"
    assert p.has_price_limit is False
    assert p.adjustment == "none"
    assert (time(13, 0), time(16, 0)) in p.sessions


def test_bj_and_etf_are_a_share():
    assert market_of("830799.BJ").market == "a_share"
    assert market_of("510330.SH").market == "a_share"


def test_is_open_at_union_of_sessions():
    from app.markets import any_market_open_at

    assert any_market_open_at(datetime(2026, 7, 1, 15, 30)) is True
    assert any_market_open_at(datetime(2026, 7, 1, 20, 0)) is False
    assert any_market_open_at(datetime(2026, 7, 4, 10, 0)) is False
