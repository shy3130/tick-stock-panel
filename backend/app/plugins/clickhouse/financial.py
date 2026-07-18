"""Convert field-level Longbridge financial rows to TickFlow table records."""

from __future__ import annotations

import math
from typing import Any

import polars as pl

FINANCIAL_TABLES = ("metrics", "income", "balance_sheet", "cash_flow")
FINANCIAL_FIELDS = (
    "EPS",
    "BPS",
    "ROE",
    "GrossMgn",
    "NetProfitMargin",
    "OperatingRevenue",
    "OperatingIncome",
    "NetProfit",
    "TotalAssets",
    "TotalLiability",
    "CashSTInvest",
    "TotalReceiv",
    "Inventory",
    "NPPE",
    "NetOperateCashFlow",
    "NetInvestCashFlow",
    "NetFinanceCashFlow",
    "CapEx",
)


def build_financial_frames(rows: list[dict[str, Any]]) -> dict[str, pl.DataFrame]:
    latest_period: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        period = _date_text(row.get("fp_end"))
        if symbol and period and period > latest_period.get(symbol, ""):
            latest_period[symbol] = period

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        field_name = str(row.get("field") or "")
        if not symbol or not field_name:
            continue
        if _date_text(row.get("fp_end")) != latest_period.get(symbol):
            continue
        grouped.setdefault(symbol, {})[field_name] = row

    output: dict[str, list[dict[str, Any]]] = {table: [] for table in FINANCIAL_TABLES}
    for symbol in sorted(grouped):
        fields = grouped[symbol]
        sample = next(iter(fields.values()))
        common = {
            "symbol": symbol,
            "period_end": latest_period[symbol],
            "announce_date": None,
            "report_period": sample.get("report_period"),
            "currency": sample.get("currency"),
        }
        revenue = _field_value(fields, "OperatingRevenue")
        assets = _field_value(fields, "TotalAssets")
        liabilities = _field_value(fields, "TotalLiability")
        operating_cash = _field_value(fields, "NetOperateCashFlow")
        investing_cash = _field_value(fields, "NetInvestCashFlow")
        financing_cash = _field_value(fields, "NetFinanceCashFlow")

        output["metrics"].append(
            {
                **common,
                "eps_basic": _field_value(fields, "EPS"),
                "bps": _field_value(fields, "BPS"),
                "roe": _field_value(fields, "ROE"),
                "gross_margin": _field_value(fields, "GrossMgn"),
                "net_margin": _field_value(fields, "NetProfitMargin"),
                "debt_to_asset_ratio": _ratio(liabilities, assets),
                "revenue_yoy": _field_number(fields, "OperatingRevenue", "yoy"),
                "net_income_yoy": _field_number(fields, "NetProfit", "yoy"),
                "operating_cash_to_revenue": _ratio(operating_cash, revenue),
            }
        )
        output["income"].append(
            {
                **common,
                "revenue": revenue,
                "operating_profit": _field_value(fields, "OperatingIncome"),
                "net_income": _field_value(fields, "NetProfit"),
                "basic_eps": _field_value(fields, "EPS"),
            }
        )
        output["balance_sheet"].append(
            {
                **common,
                "total_assets": assets,
                "cash_and_equivalents": _field_value(fields, "CashSTInvest"),
                "accounts_receivable": _field_value(fields, "TotalReceiv"),
                "inventory": _field_value(fields, "Inventory"),
                "fixed_assets": _field_value(fields, "NPPE"),
                "total_liabilities": liabilities,
                "total_equity": _subtract(assets, liabilities),
            }
        )
        output["cash_flow"].append(
            {
                **common,
                "net_operating_cash_flow": operating_cash,
                "net_investing_cash_flow": investing_cash,
                "net_financing_cash_flow": financing_cash,
                "capex": _field_value(fields, "CapEx"),
                "net_cash_change": _sum_required(operating_cash, investing_cash, financing_cash),
            }
        )

    return {
        table: pl.DataFrame(records) if records else pl.DataFrame()
        for table, records in output.items()
    }


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field_number(fields: dict[str, dict[str, Any]], field_name: str, column: str) -> float | None:
    row = fields.get(field_name)
    return _number(row.get(column)) if row else None


def _field_value(fields: dict[str, dict[str, Any]], field_name: str) -> float | None:
    return _field_number(fields, field_name, "value")


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * 100.0


def _subtract(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _sum_required(*values: float | None) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)
