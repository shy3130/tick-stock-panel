"""策略引擎板块过滤的号段口径回归。

与 #485/#488 对齐: 创业板 300/301/302, 科创板 688/689(CDR 号段,
如 689009.SH 九号公司)。689 此前只在 price_limits/matrix/overview 覆盖,
引擎选股漏配。
"""
from __future__ import annotations

import polars as pl

from app.strategy.engine import StrategyEngine


def _df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH", "302132.SZ", "688001.SH", "689009.SH"],
        "close": [10.0, 10.0, 10.0, 10.0],
    })


def test_star_board_filter_includes_689():
    expr = StrategyEngine._basic_filter_expr(_df(), {"boards": ["科创板"]})
    kept = _df().filter(expr)["symbol"].to_list()
    assert kept == ["688001.SH", "689009.SH"]


def test_chinext_board_filter_includes_302():
    expr = StrategyEngine._basic_filter_expr(_df(), {"boards": ["创业板"]})
    kept = _df().filter(expr)["symbol"].to_list()
    assert kept == ["302132.SZ"]
