"""`/rows` 查询增强 (V-开放能力): filter 等值过滤 / sort 排序 / offset 分页 /
timeseries 日期范围合并读取。

范围读取是路径敏感操作 (拼 `date=` 分区目录), 与 date_guard 同一套防穿越
校验 (_partition_date); 过滤/排序的 400 语义单独覆盖。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.ext_data import _apply_row_filters, _apply_row_sort, _read_ext_dataframe
from app.services.ext_data import ExtConfig, ExtField


def _cfg(mode: str = "timeseries") -> ExtConfig:
    return ExtConfig(
        id="hot",
        label="人气",
        mode=mode,  # type: ignore[arg-type]
        fields=[ExtField("symbol", "string"), ExtField("concept", "string"), ExtField("heat", "float")],
    )


def _layout(tmp_path: Path) -> Path:
    """三个时序分区: 09-09 / 09-10 / 09-11。"""
    data_dir = tmp_path / "data"
    rows_by_day = {
        "2026-09-09": [("000001.SZ", "AI", 1.0), ("600519.SH", "白酒", 2.0)],
        "2026-09-10": [("000001.SZ", "AI", 3.0)],
        "2026-09-11": [("300750.SZ", "AI", 5.0), ("600519.SH", "白酒", 4.0)],
    }
    for day, rows in rows_by_day.items():
        part = data_dir / "ext_data" / "hot" / "timeseries" / f"date={day}"
        part.mkdir(parents=True)
        pl.DataFrame(
            {"symbol": [r[0] for r in rows], "concept": [r[1] for r in rows], "heat": [r[2] for r in rows]}
        ).write_parquet(part / "part.parquet")
    return data_dir


# ── 日期范围读取 ──────────────────────────────────────────
def test_read_range_merges_partitions(tmp_path: Path) -> None:
    df, active = _read_ext_dataframe(_cfg(), _layout(tmp_path), start_date="2026-09-09", end_date="2026-09-10")
    assert len(df) == 3
    assert active == "2026-09-09..2026-09-10"
    # 范围合并自动补行级分区日期 (表本身无 date 列 → 补 date 列)
    assert "date" in df.columns
    assert set(df.filter(pl.col("symbol") == "600519.SH")["date"].to_list()) == {"2026-09-09"}


def test_read_range_open_ended(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)
    df, active = _read_ext_dataframe(_cfg(), data_dir, start_date="2026-09-10")
    assert len(df) == 3 and active == "2026-09-10..2026-09-11"
    df, active = _read_ext_dataframe(_cfg(), data_dir, end_date="2026-09-09")
    assert len(df) == 2 and active == "2026-09-09..2026-09-09"


def test_read_range_reversed_rejected(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as e:
        _read_ext_dataframe(_cfg(), _layout(tmp_path), start_date="2026-09-11", end_date="2026-09-09")
    assert e.value.status_code == 400


def test_read_range_bad_date_rejected(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as e:
        _read_ext_dataframe(_cfg(), _layout(tmp_path), start_date="../../kline_daily")
    assert e.value.status_code == 400


def test_snapshot_mode_rejects_range(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)
    snap = data_dir / "ext_data" / "hot" / "part.parquet"
    snap.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["AI"], "heat": [1.0]}).write_parquet(snap)
    with pytest.raises(HTTPException) as e:
        _read_ext_dataframe(_cfg(mode="snapshot"), data_dir, start_date="2026-09-09")
    assert e.value.status_code == 400


def test_exact_date_still_wins_over_range(tmp_path: Path) -> None:
    df, active = _read_ext_dataframe(
        _cfg(), _layout(tmp_path),
        snapshot_date="2026-09-10", start_date="2026-09-09", end_date="2026-09-11",
    )
    assert len(df) == 1 and active == "2026-09-10"


def test_read_range_tolerates_schema_drift(tmp_path: Path) -> None:
    """分区 schema 不一致 (配置中途加字段 → 早期分区少列): diagonal 合并, 缺列补 null。

    真实场景: ext_fuyao_hot 早期 4 列、后期 7 列, vertical concat 直接 ShapeError。
    """
    data_dir = _layout(tmp_path)
    # 给最后一天追加一列新字段 (模拟中途加列)
    last = data_dir / "ext_data" / "hot" / "timeseries" / "date=2026-09-11" / "part.parquet"
    df = pl.read_parquet(last).with_columns(pl.lit("x").alias("extra_tag"))
    df.write_parquet(last)

    out, _active = _read_ext_dataframe(_cfg(), data_dir, start_date="2026-09-09", end_date="2026-09-11")
    assert len(out) == 5
    assert "extra_tag" in out.columns
    # 早期分区的 extra_tag 为 null, 后期有值
    tags = out.sort("symbol").get_column("extra_tag").to_list()
    assert None in tags and "x" in tags


# ── 过滤 / 排序 ────────────────────────────────────────────
def _df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ", "600519.SH", "300750.SZ"],
        "concept": ["AI", "白酒", "AI"],
        "heat": [1.0, 2.5, 3.0],
    })


def test_filter_single_value_and_in_list() -> None:
    out = _apply_row_filters(_df(), ["concept:AI"])
    assert out["symbol"].to_list() == ["000001.SZ", "300750.SZ"]
    # 值间 OR
    out = _apply_row_filters(_df(), ["concept:AI|白酒"])
    assert len(out) == 3
    # 数值列按字符串等值匹配
    out = _apply_row_filters(_df(), ["heat:2.5"])
    assert out["symbol"].to_list() == ["600519.SH"]


def test_filter_multiple_conditions_are_and() -> None:
    out = _apply_row_filters(_df(), ["concept:AI", "heat:3.0"])
    assert out["symbol"].to_list() == ["300750.SZ"]


def test_filter_unknown_field_and_malformed_rejected() -> None:
    with pytest.raises(HTTPException) as e:
        _apply_row_filters(_df(), ["nope:1"])
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        _apply_row_filters(_df(), ["concept"])  # 缺 :值
    with pytest.raises(HTTPException):
        _apply_row_filters(_df(), [":AI"])  # 缺字段


def test_sort_asc_desc_and_unknown_rejected() -> None:
    out = _apply_row_sort(_df(), "heat")
    assert out["heat"].to_list() == [1.0, 2.5, 3.0]
    out = _apply_row_sort(_df(), "heat:desc")
    assert out["heat"].to_list() == [3.0, 2.5, 1.0]
    with pytest.raises(HTTPException) as e:
        _apply_row_sort(_df(), "nope")
    assert e.value.status_code == 400
