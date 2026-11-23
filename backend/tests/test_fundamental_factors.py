"""财务因子点时接入测试: 公告日门控 / 无数据 null 安全 / 双路径一致 / 历史累积同步。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.fundamentals import (
    FUNDAMENTAL_FACTOR_NAMES,
    attach_fundamental_factors,
    build_fundamental_matrices,
    load_fundamental_snapshot,
)
from app.backtest.matrix import build_market_data_matrix


def _snapshot_frame(rows: list[dict]) -> pl.DataFrame:
    frame = pl.DataFrame({
        "period_end": ["2026-03-31"] * len(rows),
        "symbol": [r["symbol"] for r in rows],
        "announce_date": [r["announce"] for r in rows],
        "roe": [r.get("roe", 10.0) for r in rows],
        "bps": [r.get("bps", 5.0) for r in rows],
        "revenue_yoy": [r.get("revenue_yoy", 8.0) for r in rows],
    })
    return (
        frame.with_columns(
            pl.col("announce_date").str.slice(0, 10).str.to_date().alias("_announce")
        )
        .sort(["symbol", "_announce"])
    )


def _daily_panel(start: date, days: int, symbols: tuple[str, ...]) -> pl.DataFrame:
    rows = []
    for offset in range(days):
        for symbol in symbols:
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0 + offset * 0.1,
                "volume": 1000.0,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_attach_gates_on_announce_date_strictly():
    panel = _daily_panel(date(2026, 4, 1), 10, ("600000.SH", "000001.SZ"))
    snapshot = _snapshot_frame([
        # 公告日 4-5: 4-5 当天不可用, 4-6 起 roe=20
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0, "bps": 4.0},
        # 无财务数据的标的
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest", "pb_latest"])

    values = attached.filter(pl.col("symbol") == "600000.SH").sort("date")
    roe = values["roe_latest"].to_list()
    assert roe[:5] == [None] * 5  # 4-1 ~ 4-5 均不可用 (含公告当日)
    assert roe[5:] == [20.0] * 5  # 4-6 起生效

    # 无财务数据标的全 null, 绝不填 0
    other = attached.filter(pl.col("symbol") == "000001.SZ")["roe_latest"]
    assert other.null_count() == other.len()

    # pb = close / bps, 公告前 null
    pb = values["pb_latest"].to_list()
    assert pb[:5] == [None] * 5
    close_6 = values.filter(pl.col("date") == date(2026, 4, 6))["close"].item()
    assert abs(pb[5] - close_6 / 4.0) < 1e-12


def test_attach_replaces_with_newer_announcement():
    panel = _daily_panel(date(2026, 4, 1), 20, ("600000.SH",))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0},
        {"symbol": "600000.SH", "announce": "2026-04-15", "roe": 33.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest"]).sort("date")
    roe = attached["roe_latest"].to_list()
    assert roe[4] is None            # 4-5 公告日
    assert roe[5] == 20.0            # 4-6 起 20.0
    assert roe[13] == 20.0           # 4-14
    assert roe[14] == 20.0           # 4-15 二次公告日: 新一期尚未生效, 仍保留上一期
    assert roe[15] == 33.0           # 4-16 起新公告生效


def test_attach_without_snapshot_keeps_null_columns():
    panel = _daily_panel(date(2026, 4, 1), 5, ("600000.SH",))
    attached = attach_fundamental_factors(panel, None, ["roe_latest", "pb_latest"])
    for name in ("roe_latest", "pb_latest"):
        assert name in attached.columns
        assert attached[name].null_count() == attached.height


def test_matrix_field_matches_polars_attach():
    panel = _daily_panel(date(2026, 4, 1), 12, ("600000.SH", "000001.SZ"))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0, "bps": 4.0},
        {"symbol": "000001.SZ", "announce": "2026-04-08", "roe": -5.0, "bps": -1.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest", "pb_latest"])
    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(market, snapshot, ["roe_latest", "pb_latest"])
    symbols = {s: i for i, s in enumerate(market.symbols)}
    dates = {str(d): t for t, d in enumerate(
        sorted({row["date"] for row in panel.iter_rows(named=True)})
    )}
    for name in ("roe_latest", "pb_latest"):
        matrix = matrices[name]
        for row in attached.iter_rows(named=True):
            expected = row[name]
            actual = matrix[dates[str(row["date"])], symbols[row["symbol"]]]
            if expected is None:
                assert np.isnan(actual), (name, row["date"], row["symbol"], actual)
            else:
                np.testing.assert_allclose(actual, expected, rtol=1e-6)


def test_matrix_field_matches_polars_attach_across_two_announcements():
    """换报告期时两条路径仍须一致: 新公告当日应保留上一期值(前向填充不断档)。"""
    panel = _daily_panel(date(2026, 4, 1), 20, ("600000.SH",))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0},
        {"symbol": "600000.SH", "announce": "2026-04-15", "roe": 33.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest"]).sort("date")
    market = build_market_data_matrix(panel)
    matrix = build_fundamental_matrices(market, snapshot, ["roe_latest"])["roe_latest"]
    column = market.symbols.index("600000.SH")
    for row_index, value in enumerate(attached["roe_latest"].to_list()):
        actual = matrix[row_index, column]
        if value is None:
            assert np.isnan(actual), (row_index, actual)
        else:
            np.testing.assert_allclose(actual, value, rtol=1e-6)


def test_matrix_field_clears_value_when_newer_report_lacks_metric():
    """新一期财报缺该指标时不得继续沿用上一期值, 否则同一行混用两期报告。

    矩阵路径逐列前向填充, 若跳过空值写入, 4-16 起 pb 已换到新期 bps=6,
    roe 却仍停在上一期的 20 —— polars 侧 join_asof 只认最新一期整行 (roe 为
    null), 两条路径给出不同的因子值。
    """
    panel = _daily_panel(date(2026, 4, 1), 20, ("600000.SH",))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0, "bps": 4.0},
        {"symbol": "600000.SH", "announce": "2026-04-15", "roe": None, "bps": 6.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest", "pb_latest"]).sort("date")
    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(market, snapshot, ["roe_latest", "pb_latest"])
    column = market.symbols.index("600000.SH")

    # polars 口径: 新公告生效 (4-16) 后 roe 无值, pb 走新一期 bps
    assert attached["roe_latest"].to_list()[15:] == [None] * 5
    assert all(value is not None for value in attached["pb_latest"].to_list()[15:])

    for name in ("roe_latest", "pb_latest"):
        for row_index, value in enumerate(attached[name].to_list()):
            actual = matrices[name][row_index, column]
            if value is None:
                assert np.isnan(actual), (name, row_index, actual)
            else:
                np.testing.assert_allclose(actual, value, rtol=1e-6)


def test_bps_nonpositive_gives_null_pb():
    panel = _daily_panel(date(2026, 4, 1), 8, ("000001.SZ",))
    snapshot = _snapshot_frame([
        {"symbol": "000001.SZ", "announce": "2026-04-02", "bps": -1.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["pb_latest"])
    assert attached["pb_latest"].null_count() == attached.height


def test_pb_uses_raw_close_on_both_paths_after_announcement():
    panel = _daily_panel(date(2026, 4, 1), 4, ("000001.SZ",)).with_columns(
        pl.Series("raw_close", [10.0, 10.1, 10.2, None]),
        (pl.col("close") * 0.5).alias("close"),
    )
    snapshot = _snapshot_frame([{"symbol": "000001.SZ", "announce": "2026-04-01", "bps": 4.0}])
    actual = attach_fundamental_factors(panel, snapshot, ["pb_latest"])["pb_latest"]
    market = build_market_data_matrix(panel, field_columns={"raw_close"})
    matrix = build_fundamental_matrices(market, snapshot, ["pb_latest"])["pb_latest"][:, 0]

    np.testing.assert_allclose(actual.to_numpy(), [np.nan, 2.525, 2.55, np.nan])
    np.testing.assert_allclose(matrix, actual.to_numpy())


def test_load_snapshot_from_missing_dir_returns_none(tmp_path: Path):
    assert load_fundamental_snapshot(tmp_path) is None
    assert load_fundamental_snapshot(None) is None


def test_snapshot_requires_announce_date(tmp_path: Path):
    out = tmp_path / "financials" / "metrics"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "announce_date": [None],
        "roe": [10.0],
        "gross_margin": [30.0],
        "net_margin": [5.0],
        "revenue_yoy": [8.0],
        "net_income_yoy": [6.0],
        "debt_to_asset_ratio": [40.0],
        "bps": [5.0],
    }).write_parquet(out / "part.parquet")
    # 公告日缺失的行无法做点时门控, 视为无有效快照
    assert load_fundamental_snapshot(tmp_path) is None


def test_fundamental_factor_names_are_catalogued():
    from app.backtest.factor import FACTOR_COLUMNS

    catalog_ids = {item["id"] for item in FACTOR_COLUMNS}
    assert catalog_ids >= FUNDAMENTAL_FACTOR_NAMES


def test_financial_sync_merges_history(tmp_path: Path):
    from app.services import financial_sync as fs

    old = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2025-09-30", "2025-12-31"],
        "announce_date": ["2025-10-28", "2026-01-20"],
        "roe": [8.0, 9.0],
    })
    latest = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "period_end": ["2026-03-31", "2026-03-31"],
        "announce_date": ["2026-04-25", "2026-04-24"],
        "roe": [10.0, 5.0],
    })
    merged = fs._merge_report_history(old, latest)
    assert merged.height == 4  # 旧各期保留 + 新一期并入
    # 同期修正: 旧 2025-12-31 公告 2026-01-20 vs 更晚的修正公告
    revised = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2025-12-31"],
        "announce_date": ["2026-02-01"],
        "roe": [9.5],
    })
    merged2 = fs._merge_report_history(old, revised)
    row = merged2.filter(
        (pl.col("symbol") == "600000.SH") & (pl.col("period_end") == "2025-12-31")
    )
    assert row.height == 1
    assert row["roe"].item() == 9.5
    assert merged2.height == 2  # 修正不增加行数


def test_financial_sync_merge_unknown_announce_date_does_not_win():
    """公告日未知的旧行不得压过带公告日的新行。

    多源并存时旧行可能没有 announce_date (provider 不提供 / 上游缺该字段)。
    这类"公告日未知"的行若排在真实公告日之后, 逐列取最后一个非空值时反而胜出,
    合并结果会出现「announce_date 是新公告、数值仍是旧值」的自相矛盾行,
    点时因子据此在公告日之后放出的是修正前的数。
    """
    from app.services import financial_sync as fs

    revised = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2025-12-31"],
        "announce_date": ["2026-02-01"],
        "roe": [9.5],
    })
    # 旧行有 announce_date 列但取值为空
    old_null = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2025-12-31"],
        "announce_date": [None],
        "roe": [9.0],
    })
    row = fs._merge_report_history(old_null, revised).to_dicts()[0]
    assert row["announce_date"] == "2026-02-01"
    assert row["roe"] == 9.5

    # 旧帧整列缺失 (另一数据源不提供该字段) — 文档承诺"后写优先"
    old_missing = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2025-12-31"],
        "roe": [9.0],
    })
    row = fs._merge_report_history(old_missing, revised).to_dicts()[0]
    assert row["announce_date"] == "2026-02-01"
    assert row["roe"] == 9.5
