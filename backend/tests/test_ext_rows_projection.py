from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import ext_data
from app.services.ext_data import ExtConfig, ExtField, PullConfig


@pytest.fixture
def query(tmp_path, monkeypatch):
    cfg = ExtConfig(id="bench", label="测试", mode="timeseries", fields=[ExtField("symbol")])
    cfg.pull = PullConfig(time_field="time")
    for i, day in enumerate(["2026-10-28", "2026-10-29"]):
        path = tmp_path / "ext_data/bench/timeseries" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        frame = pl.DataFrame({
            "symbol": ["600000.SH", "000001.SZ", "600519.SH"],
            "score": [float("nan"), 1.0 + i, None],
            "group": ["A", "B", "A"], "time": ["09:32", "09:31", "09:33"],
            "unused": ["large payload"] * 3,
            "date": [day] * 3,
        })
        if i:
            frame = frame.with_columns(pl.lit("added").alias("extra"))
        frame.write_parquet(path)
    inst = tmp_path / "instruments/instruments.parquet"
    inst.parent.mkdir()
    pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]}).write_parquet(inst)
    monkeypatch.setattr(ext_data, "_store", lambda _: SimpleNamespace(get=lambda _: cfg))
    monkeypatch.setattr(ext_data, "_data_dir", lambda _: tmp_path)

    def run(**overrides):
        args = dict(snapshot_date=None, columns="score", limit=3, offset=1, filter=None,
                    sort=None, start_date="2026-10-28", end_date="2026-10-29")
        args.update(overrides)
        return ext_data.list_rows(None, "bench", **args)

    return run, cfg, tmp_path


@pytest.mark.parametrize("kwargs", [
    {},
    {"filter": ["group:A"], "sort": "time:desc"},
    {"filter": ["name:平安银行"], "sort": "score:desc"},
    {"columns": "extra,data_date,date,score,score", "limit": 10, "offset": 0},
    {"columns": "unknown"},
    {"columns": " , "},
    {"snapshot_date": "2026-10-28"},
    {"start_date": None, "end_date": None},
    {"start_date": "2026-11-01", "end_date": "2026-11-02"},
])
def test_projected_response_matches_full_read(query, monkeypatch, kwargs):
    run, _, _ = query
    actual = run(**kwargs)
    original = ext_data._read_ext_dataframe

    def full_read(*args, **ignored):
        return original(*args)

    monkeypatch.setattr(ext_data, "_read_ext_dataframe", full_read)
    assert actual == run(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"filter": ["absent:1"]}, {"filter": ["group"]}, {"sort": "absent:desc"},
])
def test_projection_preserves_error_details(query, monkeypatch, kwargs):
    run, _, _ = query
    with pytest.raises(HTTPException) as projected:
        run(**kwargs)
    original = ext_data._read_ext_dataframe
    monkeypatch.setattr(ext_data, "_read_ext_dataframe", lambda *a, **kw: original(*a))
    with pytest.raises(HTTPException) as full:
        run(**kwargs)
    assert (projected.value.status_code, projected.value.detail) == (full.value.status_code, full.value.detail)


def test_unused_columns_are_not_materialized(query, monkeypatch):
    run, _, _ = query
    original = ext_data._with_instrument_name
    materialized = []

    def inspect(frame, data_dir):
        materialized.append(frame.columns)
        return original(frame, data_dir)

    monkeypatch.setattr(ext_data, "_with_instrument_name", inspect)
    result = run(filter=["group:B"], sort="time:desc")
    assert set(materialized[0]) == {"symbol", "score", "group", "time"}
    assert result["total"] == 2
    assert list(result["rows"][0]) == ["symbol", "name", "score"]


def test_snapshot_unknown_projection_keeps_full_row_fallback(query):
    run, cfg, root = query
    cfg.mode, cfg.pull = "snapshot", None
    pl.DataFrame({"value": [1], "other": [2]}).write_parquet(root / "ext_data/bench/part.parquet")
    result = run(columns="missing", start_date=None, end_date=None, offset=0)
    assert result["rows"] == [{"value": 1, "other": 2}]
