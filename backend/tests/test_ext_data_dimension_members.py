import polars as pl
import pytest
from fastapi import HTTPException

from app.api.ext_data import _filter_dimension_member_rows


def test_filter_dimension_member_rows_matches_complete_tags() -> None:
    rows = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
        "所属概念": ["人工智能;芯片", "人工智能体;机器人", "芯片 / 人工智能", None],
    })

    result = _filter_dimension_member_rows(rows, "所属概念", "人工智能")

    assert result.get_column("symbol").to_list() == ["000001.SZ", "000003.SZ"]


def test_filter_dimension_member_rows_matches_industry_hierarchy() -> None:
    rows = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ", "000003.SZ"],
        "所属行业": ["金融-银行-股份制银行", "电子-半导体-数字芯片", "电子元件"],
    })

    result = _filter_dimension_member_rows(rows, "所属行业", "电子")

    assert result.get_column("symbol").to_list() == ["000002.SZ"]


def test_filter_dimension_member_rows_rejects_unknown_field() -> None:
    rows = pl.DataFrame({"symbol": ["000001.SZ"]})

    with pytest.raises(HTTPException, match="字段 '所属行业' 不存在"):
        _filter_dimension_member_rows(rows, "所属行业", "银行")
