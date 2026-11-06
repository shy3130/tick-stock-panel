"""扩展数据允许初始快照无值，后续出现的字段必须完整落盘。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services.ext_data import (
    ExtConfig,
    ExtField,
    PullConfig,
    rows_to_parquet,
    write_ext_parquet,
)

DAY = date(2026, 11, 6)


def config(mode: str = "timeseries", time_field: str | None = "ts") -> ExtConfig:
    return ExtConfig(
        id="sparse", label="稀疏扩展字段", mode=mode,
        fields=[
            ExtField("symbol", "string"), ExtField("ts", "string"),
            ExtField("price", "float"), ExtField("volume", "float"),
            ExtField("imbalance", "float"), ExtField("rank", "int"),
            ExtField("note", "string"), ExtField("active", "bool"),
        ],
        pull=PullConfig(url="https://example.test/sparse", time_field=time_field),
    )


def partition(root: Path, cfg: ExtConfig) -> Path:
    directory = root / "ext_data" / cfg.id
    if cfg.mode == "timeseries":
        directory = directory / "timeseries" / f"date={DAY.isoformat()}"
    return directory / "part.parquet"


@pytest.mark.parametrize("mode,time_field", [
    ("snapshot", None), ("timeseries", None), ("timeseries", "ts"),
])
def test_fields_appearing_after_early_snapshots_are_saved(tmp_path, mode, time_field):
    cfg = config(mode, time_field)
    early = [{"symbol": f"{i:06d}.SZ", "ts": "09:15:00"} for i in range(1, 601)]
    later = {
        "symbol": "600000.SH", "ts": "09:25:01", "price": 9.88,
        "volume": 0.0, "imbalance": -173.0, "provider_extra": "late field",
    }

    assert rows_to_parquet([*early, later], cfg, tmp_path, DAY) == 601

    saved = pl.read_parquet(partition(tmp_path, cfg))
    assert saved.filter(pl.col("ts") == "09:15:00")["price"].null_count() == 600
    actual = saved.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert actual["price"] == 9.88
    assert actual["volume"] == 0.0
    assert actual["imbalance"] == -173.0
    assert actual["provider_extra"] == "late field"


def test_declared_optional_fields_can_be_entirely_absent(tmp_path):
    cfg = config()
    rows_to_parquet([{"symbol": "600000.SH", "ts": "09:15:00"}], cfg, tmp_path, DAY)

    saved = pl.read_parquet(partition(tmp_path, cfg))
    for name, dtype in {
        "price": pl.Float64, "volume": pl.Float64, "imbalance": pl.Float64,
        "rank": pl.Int64, "note": pl.Utf8, "active": pl.Boolean,
    }.items():
        assert saved.schema[name] == dtype
        assert saved[name].to_list() == [None]


def test_initial_null_type_does_not_discard_later_numeric_values(tmp_path):
    cfg = config()
    rows = [
        {"symbol": f"{i:06d}.SZ", "ts": "09:15:00", "price": None}
        for i in range(1, 131)
    ]
    rows.extend([
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 9},
        {"symbol": "600001.SH", "ts": "09:25:01", "price": 9.88},
    ])

    rows_to_parquet(rows, cfg, tmp_path, DAY)

    saved = pl.read_parquet(partition(tmp_path, cfg))
    assert saved["price"].drop_nulls().to_list() == [9.0, 9.88]
    assert saved["price"].null_count() == 130


def test_list_fields_still_use_existing_string_mapping(tmp_path):
    cfg = config()
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:15:00", "note": ["芯片", "设备"]},
        {"symbol": "600001.SH", "ts": "09:25:01", "note": None},
    ], cfg, tmp_path, DAY)

    saved = pl.read_parquet(partition(tmp_path, cfg))
    assert saved["note"].to_list() == ["芯片;设备", None]


@pytest.mark.parametrize("mode,time_field", [
    ("snapshot", None), ("timeseries", None), ("timeseries", "ts"),
])
def test_new_columns_preserve_older_rows_and_older_columns(tmp_path, mode, time_field):
    cfg = config(mode, time_field)
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:15:00", "legacy": 3.0},
    ], cfg, tmp_path, DAY)
    rows_to_parquet([
        {"symbol": "000001.SZ", "ts": "09:25:01", "price": 11.63, "extra": 2.0},
    ], cfg, tmp_path, DAY)

    saved = pl.read_parquet(partition(tmp_path, cfg))
    assert saved.height == 2
    early = saved.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    later = saved.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert early["legacy"] == 3.0 and early["price"] is None and early["extra"] is None
    assert later["price"] == 11.63 and later["legacy"] is None and later["extra"] == 2.0


def test_initial_null_and_numeric_type_changes_merge_without_losing_snapshots(tmp_path):
    cfg = config()
    for ts, price, extra in [
        ("09:15:00", None, None), ("09:20:00", 10, 1), ("09:25:01", 10.25, 1.5),
    ]:
        rows_to_parquet([
            {"symbol": "600000.SH", "ts": ts, "price": price, "extra": extra},
        ], cfg, tmp_path, DAY)

    saved = pl.read_parquet(partition(tmp_path, cfg))
    assert saved["price"].to_list() == [None, 10.0, 10.25]
    assert saved["extra"].to_list() == [None, 1.0, 1.5]


def test_same_key_keeps_existing_last_record_semantics_including_explicit_null(tmp_path):
    cfg = config()
    first = [
        {"symbol": "600000.SH", "ts": "09:15:00", "price": 10.0},
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.25},
    ]
    rows_to_parquet(first, cfg, tmp_path, DAY)
    second = [
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.5},
        {"symbol": "600000.SH", "ts": "09:25:01", "price": None},
    ]
    assert rows_to_parquet(second, cfg, tmp_path, DAY) == 2
    assert rows_to_parquet(second, cfg, tmp_path, DAY) == 2
    assert pl.read_parquet(partition(tmp_path, cfg))["price"].to_list() == [10.0, None]


def test_fresh_partition_also_deduplicates_the_same_snapshot(tmp_path):
    cfg = config()
    rows = [
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.0},
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.25},
    ]
    assert rows_to_parquet(rows, cfg, tmp_path, DAY) == 1
    assert pl.read_parquet(partition(tmp_path, cfg))["price"].to_list() == [10.25]


@pytest.mark.parametrize("mode", ["snapshot", "timeseries"])
@pytest.mark.parametrize("empty_kind", ["json", "dataframe"])
def test_empty_input_does_not_overwrite_or_refresh_existing_data(tmp_path, mode, empty_kind):
    cfg = config(mode, "ts" if mode == "timeseries" else None)
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.25},
    ], cfg, tmp_path, DAY)
    path = partition(tmp_path, cfg)
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    if empty_kind == "json":
        assert rows_to_parquet([], cfg, tmp_path, DAY) == 0
    else:
        assert write_ext_parquet(pl.DataFrame(), cfg, tmp_path, DAY) == 0

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified


@pytest.mark.parametrize("mode", ["snapshot", "timeseries"])
def test_incompatible_dynamic_type_fails_without_overwriting(tmp_path, mode):
    cfg = config(mode, "ts" if mode == "timeseries" else None)
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:15:00", "extra": 3.0},
    ], cfg, tmp_path, DAY)
    path = partition(tmp_path, cfg)
    before = path.read_bytes()

    with pytest.raises(ValueError, match=r"extra.*类型不兼容"):
        rows_to_parquet([
            {"symbol": "000001.SZ", "ts": "09:25:01", "extra": "invalid type"},
        ], cfg, tmp_path, DAY)

    assert path.read_bytes() == before


def test_unreadable_existing_partition_is_not_replaced(tmp_path):
    cfg = config()
    path = partition(tmp_path, cfg)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid parquet")

    with pytest.raises(pl.exceptions.ComputeError, match=r"parquet|Parquet"):
        rows_to_parquet([
            {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.25},
        ], cfg, tmp_path, DAY)

    assert path.read_bytes() == b"invalid parquet"


@pytest.mark.parametrize("observation", [{}, {"ts": None}])
def test_missing_snapshot_time_cannot_replace_existing_snapshots(tmp_path, observation):
    cfg = config()
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:25:01", "price": 10.25},
    ], cfg, tmp_path, DAY)
    path = partition(tmp_path, cfg)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="时间字段"):
        rows_to_parquet([
            {"symbol": "600000.SH", "price": 10.5, **observation},
        ], cfg, tmp_path, DAY)

    assert path.read_bytes() == before


def test_nullable_name_still_uses_the_existing_instrument_lookup(tmp_path):
    from app.api.ext_data import _with_instrument_name

    cfg = config()
    cfg.fields.append(ExtField("name", "string"))
    instruments = tmp_path / "instruments" / "instruments.parquet"
    instruments.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["浦发银行"]}).write_parquet(instruments)
    rows_to_parquet([
        {"symbol": "600000.SH", "ts": "09:15:00"},
        {"symbol": "600001.SH", "ts": "09:25:01", "name": "源名称"},
    ], cfg, tmp_path, DAY)

    saved = _with_instrument_name(pl.read_parquet(partition(tmp_path, cfg)), tmp_path)
    assert saved["name"].to_list() == ["浦发银行", "源名称"]
