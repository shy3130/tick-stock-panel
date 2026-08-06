from datetime import date

import polars as pl
import pytest

from app.services.screener_financials import (
    FinancialSnapshotError,
    load_financial_snapshot,
)

REQUIRED_COLUMNS = {
    "symbol",
    "report_year",
    "quarter",
    "notice_date",
    "basic_eps",
    "bps",
    "weight_avg_roe",
    "gross_margin",
    "industry",
    "yo_y_profit",
}


def _write_metrics(tmp_path, rows: list[dict]) -> None:
    path = tmp_path / "financials" / "metrics"
    path.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _row(symbol: str = "600519.SH", **overrides) -> dict:
    row = {
        "symbol": symbol,
        "report_year": 2025,
        "quarter": "2025Q1",
        "notice_date": "2025-05-01",
        "basic_eps": 1.0,
        "bps": 10.0,
        "weight_avg_roe": 0.1,
        "gross_margin": 0.2,
        "industry": "白酒",
        "yo_y_profit": 0.3,
    }
    row.update(overrides)
    return row


def test_notice_date_gates_before_latest_period_selection(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row(quarter="2025Q1", basic_eps=1.0, notice_date="2025-05-01"),
            _row(quarter="2025Q4", basic_eps=4.0, notice_date="2026-08-01"),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result.select(["quarter_num", "basic_eps"]).to_dicts() == [{"quarter_num": 1, "basic_eps": 1.0}]


def test_latest_selection_uses_numeric_year_quarter_and_one_row_per_symbol(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row("A", report_year=2024, quarter="2024Q4", basic_eps=4.0),
            _row("A", report_year=2025, quarter="2025Q1", basic_eps=1.0),
            _row("A", report_year=2025, quarter="2025Q3", basic_eps=3.0),
            _row("B", report_year=2025, quarter="2025Q2", basic_eps=2.0),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16)).sort("symbol")

    assert result.select(["symbol", "report_year", "quarter_num"]).to_dicts() == [
        {"symbol": "A", "report_year": 2025, "quarter_num": 3},
        {"symbol": "B", "report_year": 2025, "quarter_num": 2},
    ]


def test_latest_eligible_revision_wins_within_same_period(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row(basic_eps=1.0, notice_date="2026-01-01"),
            _row(basic_eps=9.0, notice_date="2026-02-01"),
            _row(basic_eps=99.0, notice_date="2026-08-01"),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result["basic_eps"].to_list() == [9.0]


def test_invalid_date_quarter_and_inconsistent_year_rows_are_dropped(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row("bad-date", notice_date="not-a-date"),
            _row("bad-quarter", quarter="2025Q5"),
            _row("bad-shape", quarter="25Q1"),
            _row("bad-year", report_year="not-a-year"),
            _row("inconsistent", report_year=2024, quarter="2025Q1"),
            _row("valid", report_year=2025, quarter="2025Q2", basic_eps=2.0),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result.get_column("symbol").to_list() == ["valid"]


def test_eps_annualization_keeps_cumulative_eps_for_each_quarter(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row("Q1", quarter="2025Q1", basic_eps=1.0),
            _row("Q2", quarter="2025Q2", basic_eps=3.0),
            _row("Q3", quarter="2025Q3", basic_eps=6.0),
            _row("Q4", quarter="2025Q4", basic_eps=10.0),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16)).sort("symbol")

    assert result.select(["basic_eps", "eps_annualized"]).to_dicts() == [
        {"basic_eps": 1.0, "eps_annualized": 4.0},
        {"basic_eps": 3.0, "eps_annualized": 6.0},
        {"basic_eps": 6.0, "eps_annualized": 8.0},
        {"basic_eps": 10.0, "eps_annualized": 10.0},
    ]


def test_null_eps_propagates_to_annualized_eps(tmp_path):
    _write_metrics(tmp_path, [_row(basic_eps=None)])

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result["basic_eps"].to_list() == [None]
    assert result["eps_annualized"].to_list() == [None]


def test_empty_eligible_result_is_typed(tmp_path):
    _write_metrics(tmp_path, [_row(notice_date="2027-01-01")])

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result.is_empty()
    assert set(result.columns) == {
        "symbol",
        "industry",
        "yo_y_profit",
        "weight_avg_roe",
        "basic_eps",
        "gross_margin",
        "bps",
        "eps_annualized",
        "report_year",
        "quarter_num",
    }
    assert result.schema["symbol"] == pl.String
    assert result.schema["report_year"] == pl.Int64
    assert result.schema["quarter_num"] == pl.Int64


def test_missing_source_is_sanitized(tmp_path):
    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert exc_info.value.reason == "source_unavailable"
    assert str(tmp_path) not in str(exc_info.value)
    assert "financials" not in str(exc_info.value)


def test_schema_failure_is_sanitized(tmp_path):
    path = tmp_path / "financials" / "metrics"
    path.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"]}).write_parquet(path / "part.parquet")

    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert exc_info.value.reason == "schema_invalid"
    assert str(tmp_path) not in str(exc_info.value)
    assert REQUIRED_COLUMNS.isdisjoint(str(exc_info.value))
