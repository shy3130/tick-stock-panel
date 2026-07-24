from __future__ import annotations

from typing import Any

from app.plugins.clickhouse.provider import ClickHouseProvider


class QueryRecorder:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def __call__(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return self.rows


def _row(field: str, value: float | None, **extra: Any) -> dict[str, Any]:
    return {
        "symbol": "700.HK",
        "report_period": "Q1 2026",
        "fp_end": "2026-03-30",
        "field": field,
        "value": value,
        "yoy": None,
        "currency": "HKD",
        **extra,
    }


def test_clickhouse_provider_maps_latest_fields_to_all_financial_tables() -> None:
    query = QueryRecorder(
        [
            _row("EPS", 7.1449),
            _row("BPS", 141.5328),
            _row("ROE", 20.3672),
            _row("GrossMgn", 56.6355),
            _row("NetProfitMargin", 29.5701),
            _row("OperatingRevenue", 200.0, yoy=15.5),
            _row("OperatingIncome", 80.0),
            _row("NetProfit", 60.0, yoy=28.6),
            _row("TotalAssets", 1000.0),
            _row("TotalLiability", 400.0),
            _row("CashSTInvest", 250.0),
            _row("TotalReceiv", 50.0),
            _row("Inventory", 10.0),
            _row("NPPE", 90.0),
            _row("NetOperateCashFlow", 100.0),
            _row("NetInvestCashFlow", -20.0),
            _row("NetFinanceCashFlow", -10.0),
            _row("CapEx", -30.0),
        ]
    )
    provider = ClickHouseProvider(query_fn=query)

    metrics = provider.get_financials("metrics", ["700.HK"]).to_dicts()[0]
    income = provider.get_financials("income", ["700.HK"]).to_dicts()[0]
    balance = provider.get_financials("balance_sheet", ["700.HK"]).to_dicts()[0]
    cash_flow = provider.get_financials("cash_flow", ["700.HK"]).to_dicts()[0]

    assert metrics == {
        "symbol": "700.HK",
        "period_end": "2026-03-30",
        "announce_date": None,
        "report_period": "Q1 2026",
        "currency": "HKD",
        "eps_basic": 7.1449,
        "bps": 141.5328,
        "roe": 20.3672,
        "gross_margin": 56.6355,
        "net_margin": 29.5701,
        "debt_to_asset_ratio": 40.0,
        "revenue_yoy": 15.5,
        "net_income_yoy": 28.6,
        "operating_cash_to_revenue": 50.0,
    }
    assert income["revenue"] == 200.0
    assert income["operating_profit"] == 80.0
    assert income["net_income"] == 60.0
    assert income["basic_eps"] == 7.1449
    assert balance["total_assets"] == 1000.0
    assert balance["total_liabilities"] == 400.0
    assert balance["total_equity"] == 600.0
    assert balance["cash_and_equivalents"] == 250.0
    assert balance["accounts_receivable"] == 50.0
    assert balance["inventory"] == 10.0
    assert balance["fixed_assets"] == 90.0
    assert cash_flow["net_operating_cash_flow"] == 100.0
    assert cash_flow["net_investing_cash_flow"] == -20.0
    assert cash_flow["net_financing_cash_flow"] == -10.0
    assert cash_flow["capex"] == -30.0
    assert cash_flow["net_cash_change"] == 70.0
    assert len(query.queries) == 1
    assert "lb_financial_report" in query.queries[0]
    assert "LIMIT 1 BY symbol, field" in query.queries[0]
    assert "'GrossMgn'" in query.queries[0]
    assert "'GROSSMGN'" not in query.queries[0]
