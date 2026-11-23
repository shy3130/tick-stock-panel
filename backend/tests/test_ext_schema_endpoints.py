"""扩展表 schema 端点 — 必须返回 Parquet 里的真实列类型。

回归: schema 端点用 `SELECT column_name, data_type FROM (DESCRIBE ...)` 取类型,
而 DuckDB 1.5 起 DESCRIBE 的输出列名是 `column_type`。写死旧名会让整个查询抛
BinderException, except 分支退回配置字段 —— 那些对象只有 `dtype` 没有 `type`,
前端按 `type` 判断数值列的扩展列/资金流下拉因此全部为空。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import ext_data as ext_api
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig


def _config(mode: str = "timeseries") -> ExtConfig:
    return ExtConfig(
        id="flow", label="资金流", mode=mode,
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("net", "float", "主力净流入"),
            ExtField("rank", "int", "排名"),
        ],
        pull=PullConfig(url="https://example.test/flow"),
    )


def _seed(tmp_path: Path, config: ExtConfig) -> None:
    ExtConfigStore(tmp_path).upsert(config)
    part = tmp_path / "ext_data" / config.id / "timeseries" / "date=2026-10-23"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "net": [1.5],
        "rank": [3],
    }).write_parquet(part / "part.parquet")


def _request(tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    config = _config()
    _seed(tmp_path, config)
    monkeypatch.setattr(ext_api, "_store", lambda request: ExtConfigStore(tmp_path))
    monkeypatch.setattr(ext_api, "_data_dir", lambda request: tmp_path)
    return tmp_path


def test_schema_returns_real_parquet_types(seeded):
    """有数据时返回 DuckDB 真实类型, 且列对象带 type 键 (前端按 type 判数值列)。"""
    res = ext_api.discover_schema(_request(seeded), "flow")
    types = {c["name"]: c["type"] for c in res["columns"]}
    assert types["symbol"] == "VARCHAR"
    assert types["net"] == "DOUBLE"
    assert types["rank"] == "BIGINT"
    assert all("type" in c for c in res["columns"])


def test_schema_all_returns_real_parquet_types(seeded):
    res = ext_api.discover_all_schemas(_request(seeded))
    item = next(i for i in res["items"] if i["id"] == "flow")
    types = {c["name"]: c["type"] for c in item["columns"]}
    assert types["net"] == "DOUBLE"
    assert types["rank"] == "BIGINT"
    # label 取自配置字段定义
    labels = {c["name"]: c["label"] for c in item["columns"]}
    assert labels["net"] == "主力净流入"


def test_schema_all_fallback_uses_type_key_when_no_data(tmp_path, monkeypatch):
    """无数据 (读 Parquet 失败) 时退回配置字段, 键名必须仍是 type, 不能是 dtype。"""
    ExtConfigStore(tmp_path).upsert(_config())
    monkeypatch.setattr(ext_api, "_store", lambda request: ExtConfigStore(tmp_path))
    monkeypatch.setattr(ext_api, "_data_dir", lambda request: tmp_path)

    res = ext_api.discover_all_schemas(_request(tmp_path))
    item = res["items"][0]
    assert [c["type"] for c in item["columns"]] == ["string", "float", "int"]
    assert all("type" in c and "dtype" not in c for c in item["columns"])


def test_schema_describe_helper_works_on_current_duckdb(seeded):
    """直接验证 DESCRIBE 取值: 不依赖具体版本的输出列名。"""
    glob = str(seeded / "ext_data" / "flow" / "timeseries" / "**" / "*.parquet")
    cols = ext_api._describe_parquet_columns(glob)
    assert ("net", "DOUBLE") in cols


def test_schema_does_not_leak_hive_partition_column(seeded):
    """`date=YYYY-MM-DD` 目录不得变成真实列 (DuckDB 默认会推断 hive 分区)。"""
    res = ext_api.discover_schema(_request(seeded), "flow")
    names = [c["name"] for c in res["columns"]]
    assert "date" not in names
    assert names == ["symbol", "net", "rank"]
