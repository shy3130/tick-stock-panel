"""日内多盘扩展列的选盘时刻 (@HH:MM[:SS]) 与序列端点。

多盘表 (配置 time_field, 如集合竞价) 一天多行; 列表取值默认收敛末行 (末盘)。
@时刻 后缀取「该时刻或之前最近一盘」(与 tickflow-hub /snapshot?at= 同语义),
序列端点 /series 供前端迷你图 (折线/柱状) 拉全天点列。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.services.ext_data import (
    ExtConfig,
    ExtField,
    PullConfig,
    collapse_ext_rows_at,
    norm_ext_snapshot_at,
)


def _auction_config(time_field: str | None = "ts") -> ExtConfig:
    return ExtConfig(
        id="ext_auc", label="竞价", mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("auction_price", "float")],
        pull=PullConfig(url="https://x", time_field=time_field) if time_field else PullConfig(url="https://x"),
    )


def _rows() -> pl.DataFrame:
    # 000001: 09:19→10.0, 09:20→10.0, 09:25→10.5; 000002: 9:21 才有首盘
    return pl.DataFrame({
        "symbol": ["000001.SZ"] * 3 + ["000002.SZ"],
        "ts": [
            "2026-10-16T09:19:01+08:00", "2026-10-16T09:20:04+08:00",
            "2026-10-16T09:25:04+08:00", "2026-10-16T09:21:00+08:00",
        ],
        "auction_price": [10.0, 10.0, 10.5, 7.0],
    })


# ── 时刻归一 ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [("09:20", "09:20:00"), ("09:20:05", "09:20:05"), ("23:59", "23:59:00")])
def test_norm_at_valid(raw, want):
    assert norm_ext_snapshot_at(raw) == want


@pytest.mark.parametrize("bad", ["", "9:20", "24:00", "09:60", "09:20:60", "aa:bb"])
def test_norm_at_invalid_raises(bad):
    with pytest.raises(ValueError):
        norm_ext_snapshot_at(bad)


# ── 收敛语义 ───────────────────────────────────────────────

def test_collapse_default_is_last_snapshot():
    out = collapse_ext_rows_at(_rows(), None, "ts")
    got = {r["symbol"]: r["auction_price"] for r in out.to_dicts()}
    assert got == {"000001.SZ": 10.5, "000002.SZ": 7.0}


def test_collapse_at_takes_nearest_not_after():
    # 09:20:00 → 000001 取 09:20 盘 (10.0), 不取 09:25 盘
    out = collapse_ext_rows_at(_rows(), "09:20:00", "ts")
    got = {r["symbol"]: r["auction_price"] for r in out.to_dicts()}
    assert got["000001.SZ"] == 10.0


def test_collapse_at_drops_symbols_with_no_prior_snapshot():
    # 000002 首盘 09:21 > 09:20 → 该标的不出现 (fail-closed, 不拿后盘伪造前盘)
    out = collapse_ext_rows_at(_rows(), "09:20:00", "ts")
    assert "000002.SZ" not in out["symbol"].to_list()


def test_collapse_at_accepts_hhmm_and_ignores_table_without_time_field():
    assert collapse_ext_rows_at(_rows(), "09:20", "ts")["auction_price"].to_list().count(10.0) == 1
    # 表未配置时间列 → 忽略 at, 维持末行收敛
    out = collapse_ext_rows_at(_rows(), "09:20", None)
    assert out.height == 2


def test_collapse_at_with_bare_time_column():
    # 纯时刻列 (无日期前缀) 原样比较
    df = pl.DataFrame({"symbol": ["A", "A"], "t": ["09:19:00", "09:22:00"], "v": [1.0, 2.0]})
    out = collapse_ext_rows_at(df, "09:20:00", "t")
    assert out["v"].to_list() == [1.0]


# ── 解析器: @后缀 ─────────────────────────────────────────

def test_screener_parser_supports_at_suffix():
    from app.api.screener import _parse_ext_columns

    assert _parse_ext_columns("ext_auc.auction_price@09:20") == [("ext_auc", "auction_price", "09:20:00")]
    assert _parse_ext_columns("ext_auc.auction_price@09:20:30") == [("ext_auc", "auction_price", "09:20:30")]
    assert _parse_ext_columns("ext_auc.auction_price") == [("ext_auc", "auction_price", None)]
    # 非法时刻 → 整条 spec 丢弃 (fail-closed)
    assert _parse_ext_columns("ext_auc.auction_price@25:00,ext_auc.v") == [("ext_auc", "v", None)]


def test_watchlist_parser_supports_at_suffix():
    from app.api.watchlist import _parse_ext_columns as parse_wl

    assert parse_wl("ext_auc.auction_price@09:20") == [("ext_auc", "auction_price", "09:20:00")]
    assert parse_wl("ext_auc.auction_price") == [("ext_auc", "auction_price", None)]


# ── /series 端点与 schema-all ─────────────────────────────

async def test_series_endpoint_shape_and_filters(tmp_path, monkeypatch):
    from app.api import ext_data as ext_api

    part = tmp_path / "ext_data" / "ext_auc" / "timeseries" / "date=2026-10-16"
    part.mkdir(parents=True)
    _rows().write_parquet(part / "part.parquet")

    class _Req:
        app = type("App", (), {})()

    req = _Req()

    def _store(request):
        class S:
            data_dir = tmp_path
            def get(self, config_id):
                return _auction_config()
        return S()

    monkeypatch.setattr(ext_api, "_store", _store)
    monkeypatch.setattr(ext_api, "_data_dir", lambda request: tmp_path)

    res = ext_api.ext_series(req, "ext_auc", field="auction_price", date="2026-10-16", symbols="000001.SZ")
    assert res["date"] == "2026-10-16"
    assert list(res["items"]) == ["000001.SZ"]
    assert res["items"]["000001.SZ"] == [["09:19:01", 10.0], ["09:20:04", 10.0], ["09:25:04", 10.5]]

    # 无该日分区 → 空 items (fail-closed, 不回退他日)
    res2 = ext_api.ext_series(req, "ext_auc", field="auction_price", date="2026-10-15")
    assert res2["items"] == {}


def test_series_rejects_table_without_time_field(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api import ext_data as ext_api

    class _Req:
        app = type("App", (), {})()

    monkeypatch.setattr(ext_api, "_store", lambda request: type(
        "S", (), {"get": lambda self, cid: _auction_config(time_field=None), "data_dir": tmp_path})())
    monkeypatch.setattr(ext_api, "_data_dir", lambda request: tmp_path)

    with pytest.raises(HTTPException) as ei:
        ext_api.ext_series(_Req(), "ext_auc", field="auction_price")
    assert ei.value.status_code == 400


def test_schema_all_exposes_time_field(tmp_path):
    from app.api.ext_data import ExtConfig

    d = ExtConfig(
        id="ext_auc", label="竞价", mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("v", "float")],
        pull=PullConfig(url="https://x", time_field="ts"),
    ).to_dict()
    # schema-all 的 time_field 取自 pull 配置; 直接验证序列化链路字段存在
    assert d["pull"]["time_field"] == "ts"
