"""日内序列表 (PullConfig.time_field) 测试: 写入多盘并存 + 默认行为不变。"""
from __future__ import annotations

from datetime import date

from app.services.ext_data import (
    PLATE_TS_FIELD,
    ExtConfig,
    ExtField,
    PullConfig,
    collapse_ext_rows_at,
    rows_to_parquet,
)


def _config(time_field: str | None) -> ExtConfig:
    return ExtConfig(
        id="auction",
        label="集合竞价",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("ts", "string", "时刻"),
            ExtField("v", "float", "值"),
        ],
        pull=PullConfig(url="https://x", time_field=time_field),
    )


def test_pull_config_time_field_roundtrip_and_normalize():
    assert PullConfig().time_field is None                      # 缺省
    assert PullConfig(time_field="  ").time_field is None       # 空白归一 None
    p = PullConfig(time_field="ts")
    assert PullConfig.from_dict(p.to_dict()).time_field == "ts"
    assert PullConfig.from_dict({}).time_field is None          # 旧配置文件兼容


def test_rows_to_parquet_time_field_keeps_multiple_disks(tmp_path):
    cfg = _config(time_field="ts")
    day = date(2026, 9, 15)
    first = [
        {"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.0},
        {"symbol": "000001.SZ", "ts": "09:18:00", "v": 2.0},
    ]
    assert rows_to_parquet(first, cfg, tmp_path, snapshot_date=day) == 2

    # 重拉同一日: 同 [symbol, ts] 覆盖 (keep=last), 新盘追加
    second = [
        {"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.5},   # 更新
        {"symbol": "000001.SZ", "ts": "09:21:00", "v": 3.0},   # 新盘
    ]
    # 返回值 = 合并去重后的分区总行数 (2 旧 + 2 新 - 1 覆盖 = 3)
    assert rows_to_parquet(second, cfg, tmp_path, snapshot_date=day) == 3

    import polars as pl

    out = pl.read_parquet(
        tmp_path / "ext_data" / "auction" / "timeseries"
        / f"date={day.isoformat()}" / "part.parquet"
    ).sort("ts")
    assert out.height == 3
    by_ts = {r["ts"]: r["v"] for r in out.to_dicts()}
    assert by_ts == {"09:15:03": 1.5, "09:18:00": 2.0, "09:21:00": 3.0}


def test_rows_to_parquet_without_time_field_unchanged(tmp_path):
    """未配置 time_field: 默认按 symbol 去重, 行为与改造前完全一致。"""
    cfg = _config(time_field=None)
    day = date(2026, 9, 15)
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.0}], cfg, tmp_path,
        snapshot_date=day,
    ) == 1
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "09:18:00", "v": 2.0}], cfg, tmp_path,
        snapshot_date=day,
    ) == 1

    import polars as pl

    out = pl.read_parquet(
        tmp_path / "ext_data" / "auction" / "timeseries"
        / f"date={day.isoformat()}" / "part.parquet"
    )
    assert out.height == 1
    assert out.row(0, named=True)["v"] == 2.0


# ---------------------------------------------------------------- __pull_ts__ 注入
def test_plate_ts_field_disables_since_param():
    """__pull_ts__ 是本地注入的拉取时刻, 源无法按它过滤 → since_param 归一失效。"""
    p = PullConfig(url="https://x", time_field=PLATE_TS_FIELD, since_param="since")
    assert p.time_field == PLATE_TS_FIELD
    assert p.since_param is None
    # 往返不丢 time_field 本身
    assert PullConfig.from_dict(p.to_dict()).time_field == PLATE_TS_FIELD


def test_rows_to_parquet_pull_ts_injection(tmp_path, monkeypatch):
    """无时间列的每日快照表配 __pull_ts__: 盘中每次拉取各存一盘; 同秒重拉覆盖。"""
    import app.services.ext_data as ext_data_module

    cfg = _config(time_field=PLATE_TS_FIELD)
    day = date(2026, 9, 15)
    clock = {"now": "2026-09-15T09:30:00+08:00"}
    monkeypatch.setattr(ext_data_module, "_pull_ts_value", lambda: clock["now"])

    first = [{"symbol": "000001.SZ", "ts": "x", "v": 1.0}]
    assert rows_to_parquet(first, cfg, tmp_path, snapshot_date=day) == 1

    # 同一秒内重拉: [symbol, __pull_ts__] 同键 → keep=last 覆盖, 不追加盘
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "x", "v": 1.5}], cfg, tmp_path, snapshot_date=day
    ) == 1

    # 不同时刻再拉: 新的一盘追加
    clock["now"] = "2026-09-15T14:00:00+08:00"
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "x", "v": 2.0}], cfg, tmp_path, snapshot_date=day
    ) == 2

    import polars as pl

    out = pl.read_parquet(
        tmp_path / "ext_data" / "auction" / "timeseries"
        / f"date={day.isoformat()}" / "part.parquet"
    ).sort(PLATE_TS_FIELD)
    assert out.height == 2
    assert [r["v"] for r in out.to_dicts()] == [1.5, 2.0]  # 第一盘被同秒重拉修正为 1.5

    # collapse 语义与真实时间列一致: @时刻选盘 / 缺省末盘
    at_13 = collapse_ext_rows_at(out, "13:00", PLATE_TS_FIELD)
    assert at_13.height == 1 and at_13.row(0, named=True)["v"] == 1.5
    last = collapse_ext_rows_at(out, None, PLATE_TS_FIELD)
    assert last.height == 1 and last.row(0, named=True)["v"] == 2.0


def test_plate_stats(tmp_path):
    """盘次概览: 多盘表返回 {date, plates, from, to}; 非多盘表/无分区为 None。"""
    from app.api.ext_data import _plate_stats

    cfg = _config(time_field="ts")
    day = date(2026, 9, 15)
    rows_to_parquet(
        [
            {"symbol": "000001.SZ", "ts": "2026-09-15T09:15:03+08:00", "v": 1.0},
            {"symbol": "000001.SZ", "ts": "2026-09-15T09:25:05+08:00", "v": 2.0},
            {"symbol": "600519.SH", "ts": "2026-09-15T09:15:03+08:00", "v": 3.0},
        ],
        cfg, tmp_path, snapshot_date=day,
    )
    assert _plate_stats(cfg, tmp_path) == {
        "date": "2026-09-15", "plates": 2, "from": "09:15:03", "to": "09:25:05",
        "granularity": "intraday",
    }
    # 未配 time_field 的每日快照表 → None
    assert _plate_stats(_config(time_field=None), tmp_path) is None
    # 多盘表但无任何分区 → None
    assert _plate_stats(_config(time_field="ts"), tmp_path / "empty") is None


def test_list_configs_plate_field(tmp_path):
    """列表端点为多盘表附带 plate 概览, 非多盘表为 None (源卡片展示契约)。"""
    from types import SimpleNamespace

    from app.api import ext_data as ext_api
    from app.services.ext_data import ExtConfigStore

    day = date(2026, 9, 15)
    multi = _config(time_field="ts")
    single = _config(time_field=None)
    single.id = "pop"
    rows_to_parquet(
        [
            {"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.0},
            {"symbol": "000001.SZ", "ts": "09:25:05", "v": 2.0},
        ],
        multi, tmp_path, snapshot_date=day,
    )
    rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "09:25:05", "v": 2.0}],
        single, tmp_path, snapshot_date=day,
    )
    # rows_to_parquet 按传入 config 入库; store 注册两份配置供 load_all
    ExtConfigStore(tmp_path).upsert(multi)
    ExtConfigStore(tmp_path).upsert(single)

    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    items = {c["id"]: c for c in ext_api.list_configs(SimpleNamespace(app=SimpleNamespace(state=state)))["items"]}
    assert items["auction"]["plate"] == {
        "date": "2026-09-15", "plates": 2, "from": "09:15:03", "to": "09:25:05",
        "granularity": "intraday",
    }
    assert items["pop"]["plate"] is None
