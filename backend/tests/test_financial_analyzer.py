from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_financial_analysis_stops_when_any_required_table_is_missing(
    monkeypatch,
    tmp_path,
):
    from app.services import financial_analyzer

    financials = {
        table: [{"period_end": "2026-06-30"}]
        for table in financial_analyzer.FINANCIAL_TABLES
    }
    financials["balance_sheet"] = []
    monkeypatch.setattr(
        financial_analyzer,
        "_load_stock_financials",
        lambda _data_dir, _symbol: financials,
    )

    messages = [
        json.loads(chunk)
        async for chunk in financial_analyzer.analyze_financials_stream(
            tmp_path,
            "600000.SH",
        )
    ]

    assert messages == [
        {
            "type": "error",
            "message": "标的 600000.SH 财务数据不完整，缺少资产负债表，不能生成完整财务分析",
        }
    ]
