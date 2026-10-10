"""日内序列表 (PullConfig.time_field) 读侧取值: 每个 symbol 必须收敛到最新一盘。

多盘并存的分区里, 策略/自选/告警/个股详情 (_load_ext_value_maps)、enriched 因子帧
(ext_factors._timeseries_frame) 与板块资金流 (sector_rotation._load_sector_flow)
都按 symbol 去重取一行。去重只看行序, 所以写入端必须保证分区内按时间列升序:
合并去重后的行序不稳定时, 各 symbol 取到的是随机一盘。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import screener
from app.factors import ext_factors
from app.services import sector_rotation
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig, rows_to_parquet

DAY = date(2026, 9, 15)
SESSIONS = ("09:15:00", "09:20:00", "09:25:00")
N_SYMBOLS = 2000


def _config() -> ExtConfig:
    return ExtConfig(
        id="auction",
        label="集合竞价",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("ts", "string", "时刻"),
            ExtField("v", "float", "值"),
        ],
        pull=PullConfig(url="https://x", time_field="ts"),
    )


def _symbols() -> list[str]:
    return [f"{i:06d}.SZ" for i in range(1, N_SYMBOLS + 1)]


def _write_three_sessions(data_dir: Path) -> ExtConfig:
    """按真实拉取节奏逐盘写入: 每一盘一次 rows_to_parquet, 走合并去重分支。"""
    cfg = _config()
    ExtConfigStore(data_dir).upsert(cfg)
    for value, ts in enumerate(SESSIONS, start=1):
        rows = [{"symbol": sym, "ts": ts, "v": float(value)} for sym in _symbols()]
        rows_to_parquet(rows, cfg, data_dir, snapshot_date=DAY)
    return cfg


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    screener._ext_value_map_cache.clear()
    sector_rotation.invalidate_cache()
    d = tmp_path / "data"
    d.mkdir()
    return d


def test_value_maps_take_latest_session_after_merge(data_dir):
    _write_three_sessions(data_dir)
    repo = SimpleNamespace(store=SimpleNamespace(db=None, data_dir=data_dir))

    vmap = screener._load_ext_value_maps(repo, "auction.v")["auction__v"]

    assert len(vmap) == N_SYMBOLS
    picked = {v for v in vmap.values()}
    assert picked == {3.0}, f"应全部取最新一盘 09:25 (v=3.0), 实际取到 {sorted(picked)}"


def test_ext_factor_frame_takes_latest_session_after_merge(data_dir):
    cfg = _write_three_sessions(data_dir)

    frame = ext_factors._timeseries_frame(data_dir, cfg, cfg.fields)

    value_col = next(c for c in frame.columns if c not in ("symbol", "_ext_date") and c.endswith("v"))
    assert frame.height == N_SYMBOLS
    assert set(frame[value_col].to_list()) == {3.0}


def test_sector_flow_takes_latest_session_after_merge(data_dir):
    _write_three_sessions(data_dir)

    flow = sector_rotation._load_sector_flow(data_dir, "auction.v")

    assert flow is not None
    assert flow.height == N_SYMBOLS
    assert set(flow["_flow"].to_list()) == {3.0}


def test_fresh_partition_sorted_even_if_upstream_returns_latest_first(data_dir):
    """首次落盘 (无旧分区, 不经合并去重): 上游按时间倒序返回也要按时间列升序落盘。"""
    cfg = _config()
    ExtConfigStore(data_dir).upsert(cfg)
    rows = [
        {"symbol": "000001.SZ", "ts": "09:25:00", "v": 3.0},
        {"symbol": "000001.SZ", "ts": "09:20:00", "v": 2.0},
        {"symbol": "000001.SZ", "ts": "09:15:00", "v": 1.0},
    ]
    rows_to_parquet(rows, cfg, data_dir, snapshot_date=DAY)
    repo = SimpleNamespace(store=SimpleNamespace(db=None, data_dir=data_dir))

    vmap = screener._load_ext_value_maps(repo, "auction.v")["auction__v"]
    assert vmap == {"000001.SZ": 3.0}
    flow = sector_rotation._load_sector_flow(data_dir, "auction.v")
    assert flow is not None and flow["_flow"].to_list() == [3.0]


def test_snapshot_table_without_time_field_row_order_unchanged(data_dir):
    """未配置 time_field 的时序表: 写入不排序, 行为与改造前一致 (每 symbol 一行)。"""
    cfg = ExtConfig(
        id="plain",
        label="普通时序表",
        mode="timeseries",
        fields=[ExtField("symbol", "string", "代码"), ExtField("v", "float", "值")],
        pull=PullConfig(url="https://x"),
    )
    ExtConfigStore(data_dir).upsert(cfg)
    rows = [{"symbol": "000002.SZ", "v": 2.0}, {"symbol": "000001.SZ", "v": 1.0}]
    rows_to_parquet(rows, cfg, data_dir, snapshot_date=DAY)

    out = pl.read_parquet(
        data_dir / "ext_data" / "plain" / "timeseries" / f"date={DAY.isoformat()}" / "part.parquet"
    )
    assert out["symbol"].to_list() == ["000002.SZ", "000001.SZ"]
