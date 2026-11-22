"""Chart formula, parameter, history and persistence contracts."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path, PureWindowsPath

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.chart_indicators import router
from app.indicators.chart import ChartIndicator, evaluate_frame
from app.indicators.chart_presets import BUILTIN_CHART_INDICATORS
from app.services import chart_indicators, preferences


def definition(formula="MA(C,N)", **changes):
    return ChartIndicator.model_validate(
        {
            "id": "test_ma",
            "name": "自定义均线",
            "pane": "main",
            "format": "price",
            "parameters": [{"name": "N", "value": 3, "min": 2, "max": 60, "integer": True}],
            "outputs": [{"key": "FAST", "label": "均线", "formula": formula, "color": "#3b82f6"}],
            **changes,
        }
    )


def frame(n=12):
    return pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * n,
            "time": [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)],
            "date": [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)],
            "close": [float(i + 1) for i in range(n)],
            "volume": [10.0] * n,
        }
    )


def test_parameterized_mean_has_numeric_null_warmup_and_no_future():
    d = definition()
    result = evaluate_frame(d, frame(), "daily")
    assert result["outputs"][0]["values"] == [None, None, *map(float, range(2, 12))]
    amended = frame().with_columns(
        pl.when(pl.int_range(pl.len()) >= 8).then(999).otherwise(pl.col("close")).alias("close")
    )
    assert (
        evaluate_frame(d, amended, "daily")["outputs"][0]["values"][:8]
        == result["outputs"][0]["values"][:8]
    )


def test_chart_operators_are_available_only_with_chart_inputs():
    from app.factors.dsl import compile_formula

    assert [error.code for error in compile_formula("ts_ema(close, 3)").errors] == ["E002"]
    indicator = definition("EMA(C,N)")
    values = evaluate_frame(indicator, frame(), "daily")["outputs"][0]["values"]
    assert values[2] == pytest.approx(2.25)


def test_multiple_outputs_refer_to_previous_output_and_recurrence():
    d = definition(
        outputs=[
            {"key": "DIF", "label": "快线", "formula": "EMA(C,N)", "color": "#3b82f6"},
            {"key": "DEA", "label": "慢线", "formula": "SMA(DIF,3,1)", "color": "#a855f7"},
            {
                "key": "HIST",
                "label": "柱线",
                "formula": "(DIF-DEA)*2",
                "color": "#ef4444",
                "style": "histogram",
            },
        ],
        pane="sub",
        format="number",
    )
    result = evaluate_frame(d, frame(20), "daily")
    expected = frame(20).select(pl.col("close").ewm_mean(span=3, adjust=False, min_samples=3))[
        "close"
    ]
    np.testing.assert_allclose(result["outputs"][0]["values"][2:], expected.to_list()[2:])
    assert result["outputs"][2]["values"][-1] == pytest.approx(
        2 * (result["outputs"][0]["values"][-1] - result["outputs"][1]["values"][-1])
    )
    changed_future = frame(20).with_columns(
        pl.when(pl.int_range(pl.len()) >= 10).then(999).otherwise(pl.col("close")).alias("close")
    )
    changed_outputs = evaluate_frame(d, changed_future, "daily")["outputs"]
    for before, after in zip(result["outputs"], changed_outputs, strict=True):
        assert before["values"][:10] == after["values"][:10]


@pytest.mark.parametrize(
    "formula",
    ["REF(C,-1)", "rank(C)", "open('/tmp/x')", "MA(C,0)", "MA(C,99999)", "UNKNOWN(C)", "DIF+C"],
)
def test_unsupported_or_future_formula_is_rejected(formula):
    with pytest.raises(ValueError):
        evaluate_frame(definition(formula), frame(), "daily")


def test_intraday_windows_use_original_minutes_and_optional_day_reset():
    df = pl.DataFrame(
        {
            "symbol": ["x"] * 6,
            "date": ["2026-01-01"] * 3 + ["2026-01-02"] * 3,
            "time": [
                "2026-01-01 11:29:00",
                "2026-01-01 11:30:00",
                "2026-01-01 13:00:00",
                "2026-01-02 09:30:00",
                "2026-01-02 09:31:00",
                "2026-01-02 09:32:00",
            ],
            "close": [1.0, 2.0, 3.0, 10.0, 20.0, 30.0],
        }
    )
    assert evaluate_frame(definition(), df, "minute")["outputs"][0]["values"] == [
        None,
        None,
        2.0,
        5.0,
        11.0,
        20.0,
    ]
    assert evaluate_frame(definition(reset_daily=True), df, "minute")["outputs"][0]["values"] == [
        None,
        None,
        2.0,
        None,
        None,
        20.0,
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    preferences.save({"other_setting": "kept"})
    preferences.save(
        {
            "chart_indicators": {
                "version": 1,
                "presets_version": 1,
                "definitions": [],
                "views": {"daily": [], "minute": []},
            }
        }
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = None
    with TestClient(app) as client:
        yield client
    preferences._invalidate_cache()


def test_library_round_trip_version_conflict_and_unrelated_preferences(client):
    original = client.get("/api/chart-indicators/library").json()
    lib = original["library"]
    lib["definitions"] = [definition().model_dump()]
    lib["views"]["daily"] = [{"id": "test_ma", "parameters": {"N": 4}}]
    response = client.put(
        "/api/chart-indicators/library", json={"library": lib, "revision": original["revision"]}
    )
    assert response.status_code == 200
    lib["views"]["daily"][0]["visible"] = True
    lib["views"]["daily"][0]["volume_compare"] = None
    assert client.get("/api/chart-indicators/library").json()["library"] == lib
    assert preferences.load()["other_setting"] == "kept"
    assert (
        client.put(
            "/api/chart-indicators/library", json={"library": lib, "revision": original["revision"]}
        ).status_code
        == 409
    )


def test_visibility_api_keeps_toolbar_membership_and_rejects_non_boolean_state(client):
    initial = client.get("/api/chart-indicators/library").json()
    library = copy.deepcopy(initial["library"])
    library["definitions"] = [definition().model_dump()]
    library["views"]["daily"] = [
        {"id": "test_ma", "parameters": {"N": 4}, "visible": False, "volume_compare": None}
    ]
    body = {"library": library, "revision": initial["revision"]}
    response = client.put("/api/chart-indicators/library", json=body)
    assert response.status_code == 200
    saved = client.get("/api/chart-indicators/library").json()
    assert saved["library"] == library
    assert len(saved["library"]["views"]["daily"]) == 1
    body["revision"] = saved["revision"]
    body["library"]["views"]["daily"][0]["visible"] = "false"
    assert client.put("/api/chart-indicators/library", json=body).status_code == 422
    assert client.get("/api/chart-indicators/library").json() == saved


def test_invalid_formula_cannot_replace_saved_library(client):
    original = client.get("/api/chart-indicators/library").json()
    lib = copy.deepcopy(original["library"])
    lib["definitions"] = [definition("REF(C,-1)").model_dump()]
    assert (
        client.put(
            "/api/chart-indicators/library", json={"library": lib, "revision": original["revision"]}
        ).status_code
        == 400
    )
    assert client.get("/api/chart-indicators/library").json() == original


def test_evaluation_cache_invalidates_for_parameters_and_earlier_bar_correction(
    client, monkeypatch
):
    calls = []
    evaluate = chart_indicators.evaluate_frame

    def spy(*args):
        calls.append(1)
        return evaluate(*args)

    monkeypatch.setattr(chart_indicators, "evaluate_frame", spy)
    chart_indicators.clear_cache()
    body = {
        "symbol": "000001.SZ",
        "timeframe": "daily",
        "definitions": [definition().model_dump()],
        "rows": [{"time": r["time"], "close": r["close"]} for r in frame().to_dicts()],
    }
    first = client.post("/api/chart-indicators/evaluate", json=body)
    assert first.status_code == 200
    assert first.json()["results"][0]["outputs"][0]["values"][-1] == 11
    assert client.post("/api/chart-indicators/evaluate", json=body).json()["cache_hit"]
    assert len(calls) == 1
    body["rows"][1]["close"] = 100
    corrected = client.post("/api/chart-indicators/evaluate", json=body).json()
    assert not corrected["cache_hit"]
    assert corrected["results"][0]["outputs"][0]["values"][2] == pytest.approx(104 / 3)
    body["definitions"][0]["parameters"][0]["value"] = 4
    assert (
        client.post("/api/chart-indicators/evaluate", json=body).json()["results"][0]["outputs"][0][
            "values"
        ][-1]
        == 10.5
    )
    assert len(calls) == 3


def test_one_bad_indicator_does_not_remove_good_result(client):
    good = definition()
    bad = definition("amount/volume", id="missing_amount", pane="sub", format="number")
    body = {
        "symbol": "x",
        "timeframe": "daily",
        "definitions": [good.model_dump(), bad.model_dump()],
        "rows": [{"time": r["time"], "close": r["close"]} for r in frame().to_dicts()],
    }
    response = client.post("/api/chart-indicators/evaluate", json=body).json()
    assert [r["id"] for r in response["results"]] == [good.id]
    assert response["errors"][0]["id"] == bad.id


def test_minute_price_indicators_tolerate_invalid_optional_trade_fields(client):
    start = datetime(2026, 1, 6, 9, 30)
    rows = [
        {
            "time": (start + timedelta(minutes=i)).isoformat(sep=" "),
            "open": 10.0 + i,
            "high": 11.0 + i,
            "low": 9.0 + i,
            "close": 10.0 + i,
            "volume": 100,
            "amount": (10.0 + i) * 100,
        }
        for i in range(40)
    ]
    rows[1]["volume"] = -1
    rows[2]["amount"] = -1
    rows[3].update(volume=0, amount=0)
    price_ids = {"builtin_macd", "builtin_rsi", "builtin_kdj", "builtin_boll"}
    definitions = [d for d in BUILTIN_CHART_INDICATORS if d.id in price_ids]
    definitions.extend([definition("VOL", id="volume"), definition("AMOUNT", id="amount")])
    body = {
        "symbol": "000001.SZ",
        "timeframe": "minute",
        "definitions": [d.model_dump() for d in definitions],
        "rows": rows,
    }

    response = client.post("/api/chart-indicators/evaluate", json=body)
    assert response.status_code == 200, response.text
    assert not response.json()["errors"]
    results = {r["id"]: r["outputs"] for r in response.json()["results"]}
    assert all(results[id][0]["values"][-1] is not None for id in price_ids)
    assert results["builtin_boll"][0]["values"][:20] == [None] * 19 + [19.5]
    assert results["builtin_boll"][0]["values"][-1] == 39.5
    assert results["volume"][0]["values"][:4] == [100, None, 100, 0]
    assert results["amount"][0]["values"][:4] == [1000, 1100, None, 0]

    rows[0]["close"] = 0
    invalid_price = client.post("/api/chart-indicators/evaluate", json=body)
    assert invalid_price.status_code == 422
    assert invalid_price.json()["detail"][0]["loc"] == ["body", "rows", 0, "close"]


def test_minute_evaluation_warms_up_during_daily_publication(client, tmp_path):
    from app.enriched_generation import EnrichedPublication
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    client.app.state.repo = repo
    start = datetime(2026, 1, 5, 9, 30)
    part = tmp_path / "kline_minute/date=2026-01-05/part.parquet"
    part.parent.mkdir()
    pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * 20,
            "datetime": [start + timedelta(minutes=i) for i in range(20)],
            "close": [30.0] * 20,
            "volume": [-1.0] * 20,
        }
    ).write_parquet(part)
    boll = next(d for d in BUILTIN_CHART_INDICATORS if d.id == "builtin_boll")
    previous_volume = definition("REF(VOL,1)", id="previous_volume")
    time = "2026-01-06 09:30:00"
    body = {
        "symbol": "000001.SZ",
        "timeframe": "minute",
        "definitions": [boll.model_dump(), previous_volume.model_dump()],
        "rows": [{"time": time, "close": 10, "volume": 100}],
    }
    publication = EnrichedPublication(tmp_path)
    publication.begin()
    try:
        response = client.post("/api/chart-indicators/evaluate", json=body)
    finally:
        publication.abandon()

    assert response.status_code == 200
    assert not response.json()["errors"]
    assert response.json()["times"] == [time]
    results = {r["id"]: r["outputs"] for r in response.json()["results"]}
    assert results["builtin_boll"][0]["values"] == [29.0]
    assert results["previous_volume"][0]["values"] == [None]


class HistoryRepo:
    def __init__(self):
        self.version = 0
        self.calls = 0
        self.rows = frame().with_columns(
            pl.col("close").alias("open"),
            pl.col("close").alias("high"),
            pl.col("close").alias("low"),
        )

    def resolve_asset_type(self, symbol):
        return "stock"

    def chart_history_version(self, *args):
        return "test", id(self), self.version

    def get_daily_asset(self, *args, **kwargs):
        self.calls += 1
        return self.rows


def test_prewarm_is_before_visible_data_cached_and_invalidated_on_history_repair(client):
    repo = HistoryRepo()
    client.app.state.repo = repo
    chart_indicators.clear_cache()
    body = {
        "symbol": "x",
        "timeframe": "daily",
        "definitions": [definition().model_dump()],
        "rows": [{"time": r["time"], "close": r["close"]} for r in frame().to_dicts()[8:]],
    }
    first = client.post("/api/chart-indicators/evaluate", json=body).json()
    assert first["results"][0]["outputs"][0]["values"][0] == 8
    assert not first["warnings"]
    assert repo.calls == 1
    assert client.post("/api/chart-indicators/evaluate", json=body).json()["cache_hit"]
    assert repo.calls == 1
    repo.rows = repo.rows.with_columns(
        pl.when(pl.col("close") == 8).then(80).otherwise(pl.col("close")).alias("close")
    )
    repo.version += 1
    repaired = client.post("/api/chart-indicators/evaluate", json=body).json()
    assert repaired["results"][0]["outputs"][0]["values"][0] == 32
    assert repo.calls == 2


def test_display_slice_is_part_of_cache_identity(client):
    repo = HistoryRepo()
    client.app.state.repo = repo
    chart_indicators.clear_cache()
    bars = [{"time": r["time"], "close": r["close"]} for r in frame().to_dicts()]
    body = {
        "symbol": "x",
        "timeframe": "daily",
        "definitions": [definition().model_dump()],
        "rows": bars,
    }
    assert len(client.post("/api/chart-indicators/evaluate", json=body).json()["times"]) == 12
    body["rows"] = bars[1:]
    clipped = client.post("/api/chart-indicators/evaluate", json=body).json()
    assert clipped["times"][0] == bars[1]["time"]
    assert len(clipped["results"][0]["outputs"][0]["values"]) == 11


def test_disabled_evaluation_does_not_read_history(client):
    repo = HistoryRepo()
    client.app.state.repo = repo
    response = client.post(
        "/api/chart-indicators/evaluate",
        json={
            "symbol": "x",
            "timeframe": "daily",
            "definitions": [],
            "rows": [{"time": "2026-01-01", "close": 10}],
        },
    ).json()
    assert response["results"] == []
    assert repo.calls == 0


def test_division_by_zero_is_a_gap_not_a_fake_zero_or_infinity(client):
    d = definition("amount/volume", pane="sub", format="number")
    body = {
        "symbol": "x",
        "timeframe": "daily",
        "definitions": [d.model_dump()],
        "rows": [
            {"time": "2026-01-01", "close": 10, "volume": 0, "amount": 0},
            {"time": "2026-01-02", "close": 10, "volume": 1, "amount": 10},
        ],
    }
    assert client.post("/api/chart-indicators/evaluate", json=body).json()["results"][0]["outputs"][
        0
    ]["values"] == [None, 10]


@pytest.mark.parametrize(
    "raw", [[], False, {"version": 2}, {"definitions": [{"id": "incomplete"}]}]
)
def test_unknown_or_corrupt_config_is_not_silently_replaced(client, raw):
    preferences.save({"chart_indicators": raw})
    assert client.get("/api/chart-indicators/library").status_code == 400
    assert preferences.load()["chart_indicators"] == raw


def test_chart_preferences_do_not_change_the_data_source_stamp(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    before = preferences.data_source_stamp()
    original = client.get("/api/chart-indicators/library").json()
    lib = original["library"]
    lib["definitions"] = [definition().model_dump()]
    assert (
        client.put(
            "/api/chart-indicators/library", json={"library": lib, "revision": original["revision"]}
        ).status_code
        == 200
    )
    assert preferences.data_source_stamp() == before


def test_concurrent_windows_cannot_overwrite_each_others_library(client):
    original = client.get("/api/chart-indicators/library").json()
    bodies = []
    for name in ("窗口一", "窗口二"):
        lib = copy.deepcopy(original["library"])
        lib["definitions"] = [definition(name=name).model_dump()]
        bodies.append({"library": lib, "revision": original["revision"]})
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(lambda body: client.put("/api/chart-indicators/library", json=body), bodies)
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert client.get("/api/chart-indicators/library").json() == winner


@pytest.mark.parametrize(
    ("asset_type", "minute_table", "factor_table"),
    [
        ("stock", "kline_minute", "adj_factor"),
        ("etf", "kline_etf_minute", "adj_factor_etf"),
    ],
)
def test_minute_history_version_tracks_repairs_factors_and_basis(
    tmp_path, monkeypatch, asset_type, minute_table, factor_table
):
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    # A glob may contain Windows separators even when the test runs on Unix.
    windows_glob = str(PureWindowsPath(tmp_path, minute_table, "**", "*.parquet"))
    monkeypatch.setattr(repo, "_minute_glob_for", lambda asset: windows_glob)
    start = end = date(2026, 1, 2)
    base = tmp_path / minute_table
    part = base / "date=2026-01-02" / "part.parquet"
    part.parent.mkdir()
    part.write_bytes(b"one")
    first = repo.chart_history_version("minute", asset_type, "x", start, end)
    part.write_bytes(b"repaired")
    second = repo.chart_history_version("minute", asset_type, "x", start, end)
    assert second != first
    factor = tmp_path / factor_table / "all.parquet"
    factor.write_bytes(b"factor")
    third = repo.chart_history_version("minute", asset_type, "x", start, end)
    assert third != second
    (tmp_path / "kline_minute/.raw_basis").touch()
    assert repo.chart_history_version("minute", asset_type, "x", start, end) != third
    (base / "symbol=x").mkdir()
    assert repo.chart_history_version("minute", asset_type, "x", start, end) is None


def test_minute_history_version_skips_cache_when_stat_fails(tmp_path, monkeypatch):
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    day = date(2026, 1, 2)
    part = tmp_path / "kline_minute/date=2026-01-02/part.parquet"
    original_stat = Path.stat

    def blocked_stat(path, *args, **kwargs):
        if path == part:
            raise PermissionError("cannot stat minute partition")
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "stat", blocked_stat)
        assert repo.chart_history_version("minute", "stock", "x", day, day) is None
    assert repo.chart_history_version("minute", "stock", "x", day, day) is not None


def test_source_compiles_without_quotes_and_rejects_line_errors_before_save(client):
    source = definition().model_dump()
    source.update(syntax="tdx", source="均线:MA(C,N),COLORYELLOW;", outputs=[])
    response = client.post("/api/chart-indicators/compile", json=source)
    assert response.status_code == 200
    assert response.json()["dependencies"] == ["close"]
    original = client.get("/api/chart-indicators/library").json()
    source["source"] = "均线:MA(C,N);\n坏:UNKNOWN(C);"
    response = client.post("/api/chart-indicators/compile", json=source)
    assert response.status_code == 400 and "第2行" in response.json()["detail"]
    raw = {**original["library"], "definitions": [source]}
    response = client.put(
        "/api/chart-indicators/library", json={"library": raw, "revision": original["revision"]}
    )
    assert response.status_code == 400
    assert client.get("/api/chart-indicators/library").json() == original
