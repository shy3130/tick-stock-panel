from datetime import date

import polars as pl
import pytest

from app.services.screener_financials import (
    FinancialSnapshotError,
    load_financial_snapshot,
    load_industry_announcements,
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

    assert result.select(["quarter_num", "basic_eps"]).to_dicts() == [
        {"quarter_num": 1, "basic_eps": 1.0}
    ]


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


def test_eps_ttm_composes_trailing_twelve_month_eps(tmp_path):
    """TTM: Q4 直接取全年累计；Q1–Q3 = 本期累计 + 上年Q4全年 − 上年同期累计。"""
    _write_metrics(
        tmp_path,
        [
            # 最新期为 Q3: 2025Q3 累计 3.0 + 2024Q4 全年 4.0 − 2024Q3 累计 2.0 → 5.0
            _row(
                "Q3LATEST",
                report_year=2024,
                quarter="2024Q3",
                basic_eps=2.0,
                notice_date="2024-10-31",
            ),
            _row(
                "Q3LATEST",
                report_year=2024,
                quarter="2024Q4",
                basic_eps=4.0,
                notice_date="2025-04-30",
            ),
            _row(
                "Q3LATEST",
                report_year=2025,
                quarter="2025Q3",
                basic_eps=3.0,
                notice_date="2025-10-31",
            ),
            # 最新期为 Q2: 2025Q2 累计 2.0 + 2024Q4 全年 4.0 − 2024Q2 累计 1.0 → 5.0
            _row(
                "Q2LATEST",
                report_year=2024,
                quarter="2024Q2",
                basic_eps=1.0,
                notice_date="2024-08-31",
            ),
            _row(
                "Q2LATEST",
                report_year=2024,
                quarter="2024Q4",
                basic_eps=4.0,
                notice_date="2025-04-30",
            ),
            _row(
                "Q2LATEST",
                report_year=2025,
                quarter="2025Q2",
                basic_eps=2.0,
                notice_date="2025-08-31",
            ),
            # 最新期为 Q4: 2025Q4 全年累计 6.0 即 TTM，无需上年数据
            _row(
                "Q4ONLY",
                report_year=2025,
                quarter="2025Q4",
                basic_eps=6.0,
                notice_date="2026-04-30",
            ),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16)).sort("symbol")

    assert result.select(["symbol", "quarter_num", "eps_ttm"]).to_dicts() == [
        {"symbol": "Q2LATEST", "quarter_num": 2, "eps_ttm": 5.0},
        {"symbol": "Q3LATEST", "quarter_num": 3, "eps_ttm": 5.0},
        {"symbol": "Q4ONLY", "quarter_num": 4, "eps_ttm": 6.0},
    ]


def test_eps_ttm_is_null_when_prior_inputs_missing(tmp_path):
    """Q1–Q3 缺上年 Q4 全年、缺上年同期或任一输入为空 → NULL，绝不外推。"""
    _write_metrics(
        tmp_path,
        [
            # 上市当年仅 3 期: 上年同期与上年 Q4 均缺 → NULL
            _row("THREE", report_year=2025, quarter="2025Q1", basic_eps=1.0),
            _row("THREE", report_year=2025, quarter="2025Q2", basic_eps=3.0),
            _row("THREE", report_year=2025, quarter="2025Q3", basic_eps=6.0),
            # 有上年同期但缺上年 Q4 全年 → NULL
            _row(
                "NOQ4PRIOR",
                report_year=2024,
                quarter="2024Q3",
                basic_eps=2.0,
                notice_date="2024-10-31",
            ),
            _row(
                "NOQ4PRIOR",
                report_year=2025,
                quarter="2025Q3",
                basic_eps=3.0,
                notice_date="2025-10-31",
            ),
            # 有上年 Q4 全年但缺上年同期 → NULL
            _row(
                "NOSAME",
                report_year=2024,
                quarter="2024Q4",
                basic_eps=4.0,
                notice_date="2025-04-30",
            ),
            _row(
                "NOSAME",
                report_year=2025,
                quarter="2025Q3",
                basic_eps=3.0,
                notice_date="2025-10-31",
            ),
            # 上年 Q4 报告存在但 EPS 为空 → NULL
            _row(
                "NULLQ4",
                report_year=2024,
                quarter="2024Q3",
                basic_eps=2.0,
                notice_date="2024-10-31",
            ),
            _row(
                "NULLQ4",
                report_year=2024,
                quarter="2024Q4",
                basic_eps=None,
                notice_date="2025-04-30",
            ),
            _row(
                "NULLQ4",
                report_year=2025,
                quarter="2025Q3",
                basic_eps=3.0,
                notice_date="2025-10-31",
            ),
        ],
    )

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16)).sort("symbol")

    assert result.select(["symbol", "eps_ttm"]).to_dicts() == [
        {"symbol": "NOQ4PRIOR", "eps_ttm": None},
        {"symbol": "NOSAME", "eps_ttm": None},
        {"symbol": "NULLQ4", "eps_ttm": None},
        {"symbol": "THREE", "eps_ttm": None},
    ]


def test_null_eps_propagates_to_eps_ttm(tmp_path):
    _write_metrics(tmp_path, [_row(basic_eps=None)])

    result = load_financial_snapshot(tmp_path, date(2026, 7, 16))

    assert result["basic_eps"].to_list() == [None]
    assert result["eps_ttm"].to_list() == [None]


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
        "eps_ttm",
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


def test_industry_announcements_are_pit_and_deterministic(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row(
                "600519.SH",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="旧行业",
            ),
            _row(
                "600519.SH",
                report_year=2025,
                quarter="2025Q2",
                notice_date="2025-08-01",
                industry="新行业",
            ),
            # 同日多报告期时，确定性选择更晚报告期。
            _row(
                "000001.SZ",
                report_year=2024,
                quarter="2024Q4",
                notice_date="2025-05-01",
                industry="旧分类",
            ),
            _row(
                "000001.SZ",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="新分类",
            ),
        ],
    )

    result = load_industry_announcements(tmp_path).sort(["symbol", "notice_date"])

    assert result.to_dicts() == [
        {"symbol": "000001.SZ", "industry": "新分类", "notice_date": date(2025, 5, 1)},
        {"symbol": "600519.SH", "industry": "旧行业", "notice_date": date(2025, 5, 1)},
        {"symbol": "600519.SH", "industry": "新行业", "notice_date": date(2025, 8, 1)},
    ]


def test_industry_announcements_fail_closed_on_same_version_conflict(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row(
                "600519.SH",
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="行业一",
            ),
            _row(
                "600519.SH",
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="行业二",
            ),
        ],
    )

    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_industry_announcements(tmp_path)

    assert exc_info.value.reason == "industry_conflict"


def test_industry_announcements_conflict_detected_after_symbol_normalization(tmp_path):
    _write_metrics(
        tmp_path,
        [
            # 同一标的两种写法: 规范化后同 symbol 同日同版本不同行业 → 冲突。
            _row(
                "600519.SH",
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="行业一",
            ),
            _row(
                " 600519.sh ",
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="行业二",
            ),
        ],
    )

    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_industry_announcements(tmp_path)

    assert exc_info.value.reason == "industry_conflict"


def test_industry_announcements_missing_report_year_column_maps_schema_invalid(tmp_path):
    path = tmp_path / "financials" / "metrics"
    path.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "industry": ["白酒"],
            "notice_date": ["2025-05-01"],
            "quarter": ["2025Q1"],
        }
    ).write_parquet(path / "part.parquet")

    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_industry_announcements(tmp_path)

    # 缺列是 schema 问题, 不是源不可读。
    assert exc_info.value.reason == "schema_invalid"
    assert str(tmp_path) not in str(exc_info.value)


def test_industry_announcements_drop_inconsistent_year_quarter_rows(tmp_path):
    _write_metrics(
        tmp_path,
        [
            # 脏行: quarter 年份与 report_year 不一致, 且季度高于同日合法行,
            # 不得抬高同日最高季度消解结果, 也不得参与冲突判定。
            _row(
                "600519.SH",
                report_year=2024,
                quarter="2025Q4",
                notice_date="2025-05-01",
                industry="脏行业",
            ),
            _row(
                "600519.SH",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="合法行业",
            ),
            # 全部行都不一致 → 该 symbol 无任何输出。
            _row(
                "000001.SZ",
                report_year=2023,
                quarter="2024Q1",
                notice_date="2025-05-01",
                industry="行业",
            ),
        ],
    )

    result = load_industry_announcements(tmp_path)

    assert result.to_dicts() == [
        {"symbol": "600519.SH", "industry": "合法行业", "notice_date": date(2025, 5, 1)}
    ]


def test_industry_announcements_normalize_symbols_and_drop_unusable_ones(tmp_path):
    _write_metrics(
        tmp_path,
        [
            _row(
                " 600519.sh ",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="白酒",
            ),
            _row(
                "   ",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="空白symbol",
            ),
            _row(
                "notasymbol",
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="垃圾symbol",
            ),
            _row(
                None,
                report_year=2025,
                quarter="2025Q1",
                notice_date="2025-05-01",
                industry="空值symbol",
            ),
        ],
    )

    result = load_industry_announcements(tmp_path)

    # 输出 symbol 为 canonical 形状, 可与 canonical 行情精确匹配;
    # 无法规范化的记录被整体剔除, 不静默污染映射。
    assert result.to_dicts() == [
        {"symbol": "600519.SH", "industry": "白酒", "notice_date": date(2025, 5, 1)}
    ]


def test_industry_announcements_missing_source_is_sanitized(tmp_path):
    with pytest.raises(FinancialSnapshotError) as exc_info:
        load_industry_announcements(tmp_path)

    assert exc_info.value.reason == "source_unavailable"
    assert str(tmp_path) not in str(exc_info.value)
