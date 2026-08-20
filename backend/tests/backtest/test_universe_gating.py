"""universe_gating 单元测试 — 上市天数门控 + 幸存者偏差警告拆分 (B6)。

断言全部基于手算边界: 边界日恰好 N 天保留 / null 上市日期 fail-open 且显式计数 /
门控关闭原样返回 / 统计字段自洽 / 警告三种组合的 code 集合。
"""

from datetime import date

import polars as pl

from app.backtest.universe_gating import (
    apply_listing_age_gate,
    split_survivorship_warnings,
)


def _panel(rows: list[tuple[str, date]]) -> pl.DataFrame:
    """最小面板: symbol/date/close, 验证无关列在门控后原样存活。"""
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "close": [10.0 + i for i in range(len(rows))],
        },
        schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64},
    )


def _listing(rows: list[tuple[str, date | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "listing_date": [r[1] for r in rows],
        },
        schema={"symbol": pl.String, "listing_date": pl.Date},
    )


# ---------------------------------------------------------------------------
# 过滤正确性
# ---------------------------------------------------------------------------


def test_gate_keeps_boundary_day_exactly_n():
    # A 上市 2024-01-01: 2024-01-31 恰好 30 天 → 保留; 2024-01-30 为 29 天 → 剔除
    panel = _panel([("A", date(2024, 1, 31)), ("A", date(2024, 1, 30))])
    listing = _listing([("A", date(2024, 1, 1))])
    gated, stats = apply_listing_age_gate(panel, listing, 30)
    assert gated.get_column("date").to_list() == [date(2024, 1, 31)]
    assert stats["rows_dropped"] == 1


def test_gate_filters_young_and_keeps_old_rows():
    panel = _panel(
        [
            ("OLD", date(2024, 6, 1)),
            ("YOUNG", date(2024, 6, 1)),
            ("YOUNG", date(2024, 12, 1)),
        ]
    )
    listing = _listing(
        [("OLD", date(2020, 1, 1)), ("YOUNG", date(2024, 5, 1))]
    )
    gated, stats = apply_listing_age_gate(panel, listing, 120)
    # YOUNG: 31 天 / 214 天 → 第二行 (>=120) 保留, 第一行剔除
    assert gated.get_column("symbol").to_list() == ["OLD", "YOUNG"]
    assert gated.get_column("date").to_list() == [
        date(2024, 6, 1),
        date(2024, 12, 1),
    ]
    # 无关列存活且值未被 join 污染
    assert "close" in gated.columns
    assert "__gating_listing_date" not in gated.columns
    assert stats["rows_dropped"] == 1


# ---------------------------------------------------------------------------
# fail-open: null 上市日期 / symbol 无映射
# ---------------------------------------------------------------------------


def test_null_listing_date_rows_kept_and_counted():
    panel = _panel(
        [
            ("NUL", date(2024, 6, 1)),
            ("MISSING", date(2024, 6, 1)),
            ("KNOWN", date(2024, 6, 1)),
        ]
    )
    listing = _listing(
        [("NUL", None), ("KNOWN", date(2020, 1, 1))]
    )
    gated, stats = apply_listing_age_gate(panel, listing, 3650)
    # listing_date=null 与 symbol 无映射 → 均保留并计入 unknown
    assert gated.get_column("symbol").to_list() == ["NUL", "MISSING"]
    assert stats["unknown_listing_date"] == 2
    assert stats["rows_dropped"] == 1  # 仅 KNOWN(上市 4.4 年) 被门槛剔除


# ---------------------------------------------------------------------------
# 门控关闭
# ---------------------------------------------------------------------------


def test_min_listed_days_non_positive_returns_panel_unchanged():
    panel = _panel([("A", date(2024, 1, 1)), ("B", date(2024, 2, 1))])
    listing = _listing([("A", date(2023, 12, 1))])
    for min_days in (0, -5):
        gated, stats = apply_listing_age_gate(panel, listing, min_days)
        assert gated.equals(panel)
        assert stats == {
            "enabled": False,
            "min_listed_days": min_days,
            "rows_before": 2,
            "rows_after": 2,
            "rows_dropped": 0,
            "symbols_dropped": 0,
            "unknown_listing_date": 0,
        }


# ---------------------------------------------------------------------------
# 统计字段
# ---------------------------------------------------------------------------


def test_stats_fields_and_symbols_dropped_semantics():
    # B 全部行被滤掉 → 计入 symbols_dropped; F 部分行保留 → 不计入
    panel = _panel(
        [
            ("B", date(2024, 6, 3)),
            ("F", date(2024, 6, 3)),
            ("F", date(2025, 6, 3)),
        ]
    )
    listing = _listing(
        [("B", date(2024, 6, 1)), ("F", date(2023, 1, 1))]
    )
    gated, stats = apply_listing_age_gate(panel, listing, 30)
    assert set(stats) == {
        "enabled",
        "min_listed_days",
        "rows_before",
        "rows_after",
        "rows_dropped",
        "symbols_dropped",
        "unknown_listing_date",
    }
    assert stats["enabled"] is True
    assert stats["min_listed_days"] == 30
    assert stats["rows_before"] == 3
    assert stats["rows_after"] == 2  # B(2天) 剔除; F 两行均 >= 30 天保留
    assert stats["rows_before"] == stats["rows_after"] + stats["rows_dropped"]
    assert stats["symbols_dropped"] == 1  # 仅 B 整段消失


# ---------------------------------------------------------------------------
# 幸存者偏差警告拆分
# ---------------------------------------------------------------------------


def test_full_market_without_gate_flags_both_biases():
    warnings = split_survivorship_warnings(True, False)
    assert [w["code"] for w in warnings] == ["delisting_bias", "listing_age_bias"]
    for w in warnings:
        assert set(w) == {"code", "message"}
        assert w["message"].strip()


def test_full_market_with_gate_replaces_bias_with_info():
    warnings = split_survivorship_warnings(True, True, min_listed_days=120)
    assert [w["code"] for w in warnings] == ["delisting_bias", "listing_age_gated"]
    gated = warnings[1]
    assert set(gated) == {"code", "message", "min_listed_days"}
    assert gated["min_listed_days"] == 120
    assert "120" in gated["message"]


def test_explicit_universe_returns_no_survivorship_warnings():
    assert split_survivorship_warnings(False, False) == []
    assert split_survivorship_warnings(False, True, 120) == []
