"""自定义信号可引用不复权价 raw_close / raw_high / raw_low。

按实际成交价位设条件(如"最高价涨到 12 元卖出")必须用不复权价: 前复权价会被
历次分红送转整体下调, 京能热力 2020 年送转后前复权价与实际价最多相差 35%。
raw_* 是 enriched 物理存储列, 策略指南也列为可用字段, 但此前不在信号白名单。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy import custom_signals


def _signal(left: str) -> dict:
    return {
        "id": "sell_at_12", "name": "不复权最高价≥12", "kind": "exit", "enabled": True,
        "conditions": [{"left": left, "op": ">=", "right": "12", "leftDays": 0, "rightDays": 0}],
    }


@pytest.mark.parametrize("field", ["raw_close", "raw_high", "raw_low"])
def test_validate_accepts_raw_price_fields(field: str) -> None:
    custom_signals.validate(_signal(field))


def test_raw_high_signal_uses_unadjusted_price_and_declares_dependency() -> None:
    # 前复权 high 11.2 未到 12, 不复权 raw_high 12.3 已到 12 → 必须按 raw 触发
    df = pl.DataFrame({
        "symbol": ["002893.SZ", "002893.SZ"],
        "date": [date(2020, 3, 2), date(2020, 3, 3)],
        "high": [11.2, 8.0],
        "raw_high": [12.3, 10.8],
    })
    exprs = custom_signals.build_expressions([_signal("raw_high")])

    out = df.with_columns(**exprs)

    assert out["csg_sell_at_12"].to_list() == [True, False]
    assert custom_signals.expression_dependencies(exprs)["csg_sell_at_12"] == frozenset({"raw_high"})
