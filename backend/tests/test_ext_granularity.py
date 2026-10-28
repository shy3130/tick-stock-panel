"""扩展序列粒度 — 声明/自动判断/生效解析 + series 端点双口径。

回归背景 (2026-10-27 用户反馈): 主力净流入列设为柱状图后渲染成满柱/塌线 —
日内多盘柱状图喂到「同日多行同值」数据时归一化退化。粒度声明让日级表走
「近 N 日每日一值」口径, 判断用时间列值形态 (非零时刻→日内) 而非盘数。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import ext_data as ext_api
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig


def _config(
    time_field: str | None = None,
    granularity: str = "auto",
    mode: str = "timeseries",
) -> ExtConfig:
    return ExtConfig(
        id="flow", label="资金流", mode=mode,  # type: ignore[arg-type]
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("net", "float", "主力净流入"),
            ExtField("ts", "string", "时刻"),
        ],
        pull=PullConfig(url="https://example.test/flow", time_field=time_field, granularity=granularity),
    )


def _write_part(tmp_path: Path, config_id: str, day: str, df: pl.DataFrame) -> None:
    part = tmp_path / "ext_data" / config_id / "timeseries" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    df.write_parquet(part / "part.parquet")


def _request(tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """挂好 _store/_data_dir 的请求工厂; 配置由用例自行 upsert。"""
    monkeypatch.setattr(ext_api, "_store", lambda request: ExtConfigStore(tmp_path))
    monkeypatch.setattr(ext_api, "_data_dir", lambda request: tmp_path)
    return tmp_path


# ---------- PullConfig.granularity ----------

def test_pull_config_granularity_roundtrip_and_normalize():
    for ok in ("auto", "intraday", "daily"):
        assert PullConfig(granularity=ok).granularity == ok
    assert PullConfig(granularity="bogus").granularity == "auto"
    assert PullConfig().granularity == "auto"
    d = PullConfig(granularity="daily").to_dict()
    assert d["granularity"] == "daily"
    back = PullConfig.from_dict({"url": "x", "granularity": "intraday"})
    assert back.granularity == "intraday"
    # 老配置无该键 → auto (无感兼容)
    assert PullConfig.from_dict({"url": "x"}).granularity == "auto"


# ---------- _granularity_effective: 判断表 ----------

def test_effective_snapshot_mode_is_none(store):
    assert ext_api._granularity_effective(_config(mode="snapshot"), store) is None


def test_effective_no_time_field_is_daily(store):
    # 落盘契约: 未配 time_field = 每日快照表 (按 symbol 去重) → 日级
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"], "net": [1.5], "ts": ["09:25:00"],
    }))
    assert ext_api._granularity_effective(_config(), store) == "daily"


def test_effective_time_field_no_data_defaults_intraday(store):
    # 配了时间列但尚无分区: 以配置意图为准 (与历史 UI gating 行为一致)
    assert ext_api._granularity_effective(_config(time_field="ts"), store) == "intraday"


def test_effective_nonzero_times_is_intraday(store):
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "net": [-100.0, -100.0, -100.0],
        "ts": ["2026-10-23 09:20:00", "09:25:00", "2026-10-23 09:30:00"],
    }))
    assert ext_api._granularity_effective(_config(time_field="ts"), store) == "intraday"


def test_effective_date_form_or_midnight_times_is_daily(store):
    # 时间列配了日期形态值或 00:00:00 → 日级 (把日期列配成时间列的典型错配被纠正)
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"] * 2,
        "net": [1.0, 2.0],
        "ts": ["2026-10-23 00:00:00", "2026-10-22"],
    }))
    assert ext_api._granularity_effective(_config(time_field="ts"), store) == "daily"


def test_effective_declared_overrides_detection(store):
    # 数据形态是日内, 但显式声明日级 → 日级 (用户可钉死)
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"], "net": [1.0], "ts": ["09:25:00"],
    }))
    assert ext_api._granularity_effective(_config(time_field="ts", granularity="daily"), store) == "daily"
    # 无时间列但显式声明日内 → 日内 (强意图)
    assert ext_api._granularity_effective(_config(granularity="intraday"), store) == "intraday"


# ---------- series 端点: 日级口径 ----------

def test_series_daily_near_n_days(store):
    ExtConfigStore(store).upsert(_config())  # 无 time_field → auto=daily
    for day, v in [("2026-10-21", 1.0), ("2026-10-22", 2.0), ("2026-10-23", 3.0)]:
        _write_part(store, "flow", day, pl.DataFrame({
            "symbol": ["600000.SH", "000001.SZ"], "net": [v, v * 10], "ts": [None, None],
        }))
    res = ext_api.ext_series(_request(store), "flow", field="net", days=2, date=None, symbols=None)
    assert res["granularity"] == "daily"
    assert res["date"] == "2026-10-22..2026-10-23"
    # 近 2 日 = 10-22/10-23, 不含 10-21
    assert res["items"]["600000.SH"] == [["2026-10-22", 2.0], ["2026-10-23", 3.0]]
    assert res["items"]["000001.SZ"] == [["2026-10-22", 20.0], ["2026-10-23", 30.0]]


def test_series_daily_same_day_multi_row_takes_last_non_null(store):
    ExtConfigStore(store).upsert(_config())
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"] * 3, "net": [1.0, None, 7.0], "ts": [None] * 3,
    }))
    res = ext_api.ext_series(_request(store), "flow", field="net", days=10, date=None, symbols=None)
    # 同日多行 → 末个非空值 (日末定型语义), 每日只有一点
    assert res["items"]["600000.SH"] == [["2026-10-23", 7.0]]


def test_series_daily_date_param_is_window_right_edge(store):
    ExtConfigStore(store).upsert(_config())
    for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
        _write_part(store, "flow", day, pl.DataFrame({
            "symbol": ["600000.SH"], "net": [1.0], "ts": [None],
        }))
    res = ext_api.ext_series(_request(store), "flow", field="net", days=10, date="2026-10-22", symbols=None)
    assert res["date"] == "2026-10-21..2026-10-22"
    assert [p[0] for p in res["items"]["600000.SH"]] == ["2026-10-21", "2026-10-22"]


def test_series_daily_symbols_filter_and_empty(store):
    ExtConfigStore(store).upsert(_config())
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"], "net": [1.0, 2.0], "ts": [None, None],
    }))
    res = ext_api.ext_series(_request(store), "flow", field="net", days=10, date=None, symbols="600000.SH")
    assert set(res["items"]) == {"600000.SH"}
    # 无分区 → items 空 (fail-closed)
    empty = ext_api.ext_series(_request(store), "flow", field="net", days=10, date="2020-01-01", symbols=None)
    assert empty["items"] == {}


def test_series_declared_daily_without_time_field_ok(store):
    # 显式声明日级 + 无时间列: 走近 N 日口径, 不再 400
    ExtConfigStore(store).upsert(_config(granularity="daily"))
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"], "net": [5.0], "ts": [None],
    }))
    res = ext_api.ext_series(_request(store), "flow", field="net", days=10, date=None, symbols=None)
    assert res["items"]["600000.SH"] == [["2026-10-23", 5.0]]


def test_series_declared_intraday_without_time_field_400(store):
    ExtConfigStore(store).upsert(_config(granularity="intraday"))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        ext_api.ext_series(_request(store), "flow", field="net", days=10, date=None, symbols=None)
    assert "time_field" in str(e.value.detail)


# ---------- series 端点: 日内口径不回归 ----------

def test_series_intraday_unchanged(store):
    ExtConfigStore(store).upsert(_config(time_field="ts"))
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
        "net": [1.0, 2.0, 3.0],
        "ts": ["09:20:00", "09:25:00", "09:20:00"],
    }))
    res = ext_api.ext_series(
        _request(store), "flow", field="net", days=10, date="2026-10-23", symbols="600000.SH",
    )
    assert res["granularity"] != "daily"
    assert res["items"]["600000.SH"] == [["09:20:00", 1.0], ["09:25:00", 2.0]]


# ---------- schema / list 透出 ----------

def test_schema_all_exposes_granularity(store):
    ExtConfigStore(store).upsert(_config(time_field="ts"))
    _write_part(store, "flow", "2026-10-23", pl.DataFrame({
        "symbol": ["600000.SH"], "net": [1.0], "ts": ["09:25:00"],
    }))
    res = ext_api.discover_all_schemas(_request(store))
    item = next(i for i in res["items"] if i["id"] == "flow")
    assert item["granularity"] == "intraday"


def test_list_configs_exposes_granularity_effective(store):
    ExtConfigStore(store).upsert(_config())
    res = ext_api.list_configs(_request(store))
    item = next(i for i in res["items"] if i["id"] == "flow")
    assert item["granularity_effective"] == "daily"
