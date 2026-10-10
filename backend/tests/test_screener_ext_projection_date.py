"""策略结果展示的扩展列投影必须与结果同日 (过滤口径 == 展示口径)。

背景 (真实故障): 人气时序扩展表每 10 分钟刷新到最新分区。策略按 10-09 跑扫描,
叠加条件 `人气<=1000` 用 10-09 分区过滤 (日期正确); 但结果表展示的人气列取的是
「最新分区」(10-10 实时人气), 隔夜排名漂移后同一只股票显示成 1316 —— 用户看到的
是「过滤了人气<=1000, 结果里却有 1300 多」的假故障。

修复: `_load_ext_value_maps` 增加 as_of; 扫描结果类调用方 (run/run_preset/run_all/
cached/cached-result) 按结果日期取分区, 该日期无分区时回退最新; as_of=None 的
实时/自选/个股详情场景维持「最新分区」的当前值语义。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import screener
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig, rows_to_parquet

D1 = date(2026, 10, 9)
D2 = date(2026, 10, 10)


def _config() -> ExtConfig:
    return ExtConfig(
        id="heat",
        label="人气",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("rank", "int", "人气"),
        ],
        pull=PullConfig(url="https://x"),
    )


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    screener._ext_value_map_cache.clear()
    d = tmp_path / "data"
    d.mkdir()
    return d


def _write(data_dir: Path, day: date, values: dict[str, int]) -> None:
    cfg = _config()
    ExtConfigStore(data_dir).upsert(cfg)
    rows = [{"symbol": sym, "rank": v} for sym, v in values.items()]
    rows_to_parquet(rows, cfg, data_dir, snapshot_date=day)


def _repo(data_dir: Path):
    return SimpleNamespace(store=SimpleNamespace(db=None, data_dir=data_dir))


def test_ext_projection_uses_result_date_partition(data_dir):
    """as_of 给定时取结果日分区 (与叠加条件过滤同一天), 与最新分区互不串用。"""
    _write(data_dir, D1, {"000058.SZ": 779, "002617.SZ": 39})
    _write(data_dir, D2, {"000058.SZ": 1316, "002617.SZ": 133})
    repo = _repo(data_dir)

    # 结果日 (10-09) 口径 == 过滤口径
    same_day = screener._load_ext_value_maps(repo, "heat.rank", as_of=str(D1))["heat__rank"]
    assert same_day == {"000058.SZ": 779, "002617.SZ": 39}
    # 无日期 (实时/自选等当前值场景) 维持最新分区
    latest = screener._load_ext_value_maps(repo, "heat.rank")["heat__rank"]
    assert latest == {"000058.SZ": 1316, "002617.SZ": 133}
    # memoize 键含日期: 同日重复取仍是结果日口径, 不被最新分区覆盖
    again = screener._load_ext_value_maps(repo, "heat.rank", as_of=str(D1))["heat__rank"]
    assert again == same_day


def test_ext_projection_falls_back_to_latest_when_date_missing(data_dir):
    """结果日无分区 (扩展表上线前) → 回退最新分区, 避免整列空白。"""
    _write(data_dir, D2, {"000058.SZ": 1316})
    repo = _repo(data_dir)

    vmap = screener._load_ext_value_maps(repo, "heat.rank", as_of="2026-08-01")["heat__rank"]
    assert vmap == {"000058.SZ": 1316}


def test_cached_payload_projection_matches_each_result_date(data_dir):
    """缓存 payload 的盘后结果与实时叠加结果各自用其 as_of 日的投影。"""
    _write(data_dir, D1, {"A.SZ": 779, "B.SZ": 1087})
    _write(data_dir, D2, {"A.SZ": 1316, "B.SZ": 942})
    repo = _repo(data_dir)

    cached = {
        "as_of": str(D1),
        "results": {
            "daily": {"total": 2, "as_of": str(D1), "rows": [
                {"symbol": "A.SZ", "date": str(D1)}, {"symbol": "B.SZ", "date": str(D1)}]},
            "realtime": {"total": 2, "as_of": str(D2), "rows": [
                {"symbol": "A.SZ", "date": str(D2)}, {"symbol": "B.SZ", "date": str(D2)}]},
        },
        "today_ever_rows": {
            "daily": {"A.SZ": {"symbol": "A.SZ", "date": str(D1)}},
        },
    }
    dates = {r.get("as_of") for r in cached["results"].values()} | {None}
    maps = screener._ext_maps_for_dates(repo, "heat.rank", dates)
    payload = screener._cache_payload_with_ext(cached, maps)

    assert payload["results"]["daily"]["rows"][0]["heat__rank"] == 779
    assert payload["results"]["daily"]["rows"][1]["heat__rank"] == 1087
    assert payload["results"]["realtime"]["rows"][0]["heat__rank"] == 1316
    assert payload["results"]["realtime"]["rows"][1]["heat__rank"] == 942
    assert payload["today_ever_rows"]["daily"]["A.SZ"]["heat__rank"] == 779


def test_ext_maps_for_dates_skips_projection_without_columns(data_dir):
    """未请求 ext 列时返回空字典 (调用方保持原 payload)。"""
    assert screener._ext_maps_for_dates(_repo(data_dir), None, {str(D1)}) == {}
