"""维度值清洗口径。

类别名里的 NaN/None 占位值清洗已随 fanout 迁移到 ext_fanout.clean_label
(见 test_ext_fanout.py); 此处保留消费侧的维度值清洗测试。
"""
from __future__ import annotations

from app.services.market_overview_builder import _dimension_values


def test_overview_dimension_values_ignore_legacy_nan_group():
    assert _dimension_values("人工智能;nan;芯片;NULL") == ["人工智能", "芯片"]
