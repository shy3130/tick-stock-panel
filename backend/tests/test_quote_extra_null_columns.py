"""quote_extra 全空列过滤 (issue 反馈: fuyao 实时链路当日换手/振幅全空)。

数据源 (如 fuyao) 对未提供的字段显式给出值为 None 的键; 若 _build_quote_extra
原样转发全空列, 下游 compute_enriched_today 对这些列是「列存在即直接采用」,
会跳过从 float_shares/价格 的回退计算, 当日指标永远为空且每轮实时覆写自锁。
"""

from __future__ import annotations

import polars as pl

from app.services.quote_service import QuoteService


def test_all_null_columns_are_dropped():
    records = [
        {
            "symbol": "600001.SH",
            "prev_close": 10.0,
            "change_pct": 0.05,
            "change_amount": 0.5,
            "amplitude": None,       # fuyao: 快照未提供
            "turnover_rate": None,   # fuyao: 需股本口径, 交给 enriched 管道
        },
        {
            "symbol": "000820.SZ",
            "prev_close": 20.0,
            "change_pct": -0.02,
            "change_amount": -0.4,
            "amplitude": None,
            "turnover_rate": None,
        },
    ]

    out = QuoteService._build_quote_extra(records)

    assert out.columns == ["symbol", "prev_close", "change_pct", "change_amount"]


def test_partial_null_column_is_kept_and_scaled():
    # 部分标的缺失时列仍有效 (提供方已给出可用数据), null 行由下游「有则用」语义跳过
    records = [
        {"symbol": "600001.SH", "prev_close": 10.0, "turnover_rate": None},
        {"symbol": "000820.SZ", "prev_close": 20.0, "turnover_rate": 0.05},
    ]

    out = QuoteService._build_quote_extra(records)

    assert out.columns == ["symbol", "prev_close", "turnover_rate"]
    # 小数制 (0.05) → enriched 百分比口径 (5.0)
    assert out["turnover_rate"].to_list() == [None, 5.0]


def test_all_columns_null_returns_symbol_only():
    records = [
        {"symbol": "600001.SH", "amplitude": None, "turnover_rate": None},
        {"symbol": "000820.SZ", "amplitude": None, "turnover_rate": None},
    ]

    out = QuoteService._build_quote_extra(records)

    # 只剩 symbol 的 join 等价于无补充字段, 但保持可 join 形态无害
    assert out.columns == ["symbol"]
    assert isinstance(out, pl.DataFrame)
