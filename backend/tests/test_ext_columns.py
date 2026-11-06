"""#547: 重复扩展列不能使梯队 500 或向自选泄漏 _right 列。"""
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import screener, watchlist
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.screener import ScreenerService

_DAY = date(2026, 11, 5)
_FIELD = "ext_gn_ths.所属概念"
_OUTPUT = "ext_gn_ths__所属概念"
_PARSERS = [screener._parse_ext_columns, watchlist._parse_ext_columns]


@pytest.mark.parametrize("parse", _PARSERS, ids=["screener", "watchlist"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"{_FIELD},{_FIELD}", [("ext_gn_ths", "所属概念", None)]),
        (f" {_FIELD} , {_FIELD} ", [("ext_gn_ths", "所属概念", None)]),
        (
            f"{_FIELD}@09:20,{_FIELD}@09:20:00",
            [("ext_gn_ths", "所属概念", "09:20:00")],
        ),
        (
            f"{_FIELD},other.所属概念,{_FIELD},ext_gn_ths.所属行业",
            [("ext_gn_ths", "所属概念", None), ("other", "所属概念", None),
             ("ext_gn_ths", "所属行业", None)],
        ),
        (
            f"{_FIELD}@25:00,bad-id.field,{_FIELD}",
            [("ext_gn_ths", "所属概念", None)],
        ),
    ],
)
def test_duplicate_columns_preserve_order_and_normalized_time(parse, raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("parse", _PARSERS, ids=["screener", "watchlist"])
@pytest.mark.parametrize(
    "raw", [f"{_FIELD}@09:20,{_FIELD}", f"{_FIELD}@09:20,{_FIELD}@09:25", "a__b.c,a.b__c"],
)
def test_conflicting_output_columns_are_rejected(parse, raw):
    with pytest.raises(HTTPException) as exc:
        parse(raw)
    assert exc.value.status_code == 400


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    symbols = ["600000.SH", "000001.SZ"]
    snapshot = pl.DataFrame({
        "symbol": symbols, "name": ["涨停样例", "跌停样例"],
        "close": [11.0, 9.0], "change_pct": [0.1, -0.1],
        "signal_limit_up": [True, False], "signal_limit_down": [False, True],
        "signal_broken_limit_up": [False, False], "signal_limit_down_recovery": [False, False],
        "consecutive_limit_ups": [2, 0], "consecutive_limit_downs": [0, 2],
    })
    config = ExtConfig(
        id="ext_gn_ths", label="测试扩展", mode="snapshot",
        fields=[ExtField("所属概念"), ExtField("所属行业"), ExtField("t")],
        pull=PullConfig(url="https://example.test", time_field="t"),
    )
    ExtConfigStore(tmp_path).upsert(config)
    parquet = tmp_path / "ext_data" / config.id / "part.parquet"
    pl.DataFrame({
        "symbol": [s for s in symbols for _ in range(2)],
        "t": ["09:19:00", "09:25:00"] * 2,
        "所属概念": ["早盘概念", "最新概念"] * 2,
        "所属行业": ["早盘行业", "最新行业"] * 2,
    }).write_parquet(parquet)
    config_path = parquet.with_name("config.json")
    originals = {p: p.read_bytes() for p in (parquet, config_path)}
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path, db=None),
        get_enriched_latest=lambda: (snapshot, _DAY),
        get_enriched_latest_asset=lambda _: (pl.DataFrame(), None),
        get_etf_symbol_set=set, get_index_symbol_set=set,
        get_instruments=pl.DataFrame,
        get_name_map=lambda _: dict(zip(symbols, ["涨停样例", "跌停样例"], strict=True)),
    )
    monkeypatch.setattr(watchlist.watchlist, "list_symbols", lambda: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda self, day: snapshot)
    monkeypatch.setattr(ScreenerService, "load_prior_consecutive", lambda self, day, col: pl.DataFrame())
    app = FastAPI()
    app.state.repo = repo
    app.state.depth_service = SimpleNamespace(
        get_sealed_map=lambda *args, **kwargs: {}, is_sealed_ready=lambda _: False,
    )
    app.include_router(screener.router)
    app.include_router(watchlist.router)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    assert {p: p.read_bytes() for p in originals} == originals


_ENDPOINTS = [
    ("/api/screener/limit-ladder", {"as_of": str(_DAY), "direction": "up"}),
    ("/api/screener/limit-ladder", {"as_of": str(_DAY), "direction": "down"}),
    ("/api/watchlist/enriched", {}),
]


def _rows(response):
    payload = response.json()
    rows = payload.get("rows", [s for tier in payload.get("tiers", []) for s in tier["stocks"]])
    # 新鲜度元信息含请求时间；这里比较重复列请求的实际业务数据。
    return [{key: value for key, value in row.items() if key != "quote_freshness"} for row in rows]


@pytest.mark.parametrize(("path", "params"), _ENDPOINTS, ids=["up", "down", "watchlist"])
@pytest.mark.parametrize("spec", [_FIELD, f"{_FIELD}@09:20", f"{_FIELD}@09:20:00"])
def test_repeated_columns_match_single_column_response(client, path, params, spec):
    single = client.get(path, params={**params, "ext_columns": spec})
    repeated = client.get(path, params={**params, "ext_columns": ",".join([spec] * 3)})
    assert single.status_code == repeated.status_code == 200
    assert _rows(single) == _rows(repeated)
    rows = _rows(repeated)
    assert len(rows) == (2 if path.startswith("/api/watchlist") else 1)
    assert {row[_OUTPUT] for row in rows} == {"早盘概念" if "@" in spec else "最新概念"}
    assert all(not key.endswith("_right") for row in rows for key in row)


@pytest.mark.parametrize(("path", "params"), _ENDPOINTS, ids=["up", "down", "watchlist"])
def test_distinct_columns_keep_both_values(client, path, params):
    response = client.get(path, params={**params, "ext_columns": f"{_FIELD},ext_gn_ths.所属行业"})
    assert response.status_code == 200
    for row in _rows(response):
        assert row[_OUTPUT] == "最新概念"
        assert row["ext_gn_ths__所属行业"] == "最新行业"


@pytest.mark.parametrize(("path", "params"), _ENDPOINTS, ids=["up", "down", "watchlist"])
def test_conflicting_times_return_400(client, path, params):
    response = client.get(path, params={**params, "ext_columns": f"{_FIELD}@09:20,{_FIELD}@09:25"})
    assert response.status_code == 400
    assert "时刻" in response.json()["detail"]
