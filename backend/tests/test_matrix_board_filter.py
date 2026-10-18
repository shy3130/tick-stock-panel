"""回测矩阵的板块筛选应包含创业板 302 号段与科创板 689 号段。"""
from app.backtest.matrix import _symbol_in_boards


def test_chinext_302_is_in_chinext_board():
    assert _symbol_in_boards("302132.SZ", ["创业板"])
    assert not _symbol_in_boards("302132.SZ", ["深主板"])


def test_star_689_is_in_star_board():
    assert _symbol_in_boards("689009.SH", ["科创板"])
    assert not _symbol_in_boards("689009.SH", ["沪主板"])
