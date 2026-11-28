"""Preset migration, sharing and backup validation without touching user data."""

import copy
import io
import json
import zipfile
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.indicators.chart import ChartIndicator, ChartLibrary, evaluate_frame
from app.services import chart_indicators as charts
from app.services import config_backup, import_center, preferences
from tests.legacy_chart_indicators import migrate_library as legacy_library
from tests.legacy_chart_indicators import presets


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    preferences.save({"chart_indicators": legacy_library(None).model_dump()})
    yield
    preferences._invalidate_cache()


def test_missing_scheme_displays_ma_and_volume_without_writing_preferences():
    preferences._path().unlink()
    preferences.save({"unrelated": "keep"})
    before = preferences._path().read_bytes()
    initial = charts.get_library()
    library = initial["library"]
    assert [d["id"] for d in library["definitions"]] == ["builtin_ma", "builtin_volume"]
    assert [s["id"] for s in library["views"]["daily"]] == ["builtin_ma", "builtin_volume"]
    assert all(s["visible"] for s in library["views"]["daily"])
    assert library["views"]["daily"][1]["volume_compare"] == {
        "days": 1, "show_labels": True, "show_readout": True,
    }
    assert library["views"]["minute"] == []
    assert {d["id"]: d["syntax"] for d in initial["presets"]} == dict.fromkeys(
        ("builtin_ma", "builtin_macd", "builtin_volume", "builtin_rsi", "builtin_kdj", "builtin_boll"),
        "expression",
    )
    exported = charts.export_definitions(["builtin_ma", "builtin_volume"], initial["revision"])
    assert exported["definitions"] == library["definitions"]
    assert preferences._path().read_bytes() == before

    empty = ChartLibrary()
    charts.save_library(charts.LibrarySave(library=empty, revision=initial["revision"]))
    assert charts.get_library()["library"] == empty.model_dump()
    assert preferences.load()["unrelated"] == "keep"


def test_native_presets_and_saved_tdx_formula_evaluate_together():
    source = {
        "id": "saved_tdx",
        "name": "旧源码",
        "syntax": "tdx",
        "source": "均线:MA(C,5);DRAWICON(C>75,L,1);",
    }
    preferences.save({"chart_indicators": {"version": 1, "definitions": [source]}})
    library = charts.get_library()
    presets = library["presets"]
    saved = library["library"]["definitions"]
    assert saved[0]["source"] == source["source"]
    start = date(2026, 1, 1)
    rows = [
        {
            "time": (start + timedelta(days=i - 1)).isoformat(),
            "open": i + (1 if i % 2 else -1),
            "high": i + 2,
            "low": i - 0.5,
            "close": i,
            "volume": 100 + i,
        }
        for i in range(1, 81)
    ]
    result = charts.evaluate(charts.EvaluateRequest.model_validate({
        "symbol": "000001.SZ",
        "timeframe": "daily",
        "rows": rows,
        "definitions": [*presets, *saved],
    }))

    assert result["errors"] == []
    indicators = {item["id"]: item for item in result["results"]}
    assert len(indicators) == 7
    for preset in presets:
        indicator = indicators[preset["id"]]
        assert indicator["syntax"] == "expression"
        assert all(output["values"][-1] is not None for output in indicator["outputs"])
    assert indicators["builtin_ma"]["outputs"][0]["values"][:5] == [None] * 4 + [3.0]
    assert indicators["builtin_boll"]["outputs"][0]["values"][-1] == 70.5
    volume = indicators["builtin_volume"]["outputs"][0]
    assert volume["style"] == "volume"
    assert volume["colors"][:2] == ["#2D9B65", "#C74040"]
    assert indicators["saved_tdx"]["syntax"] == "tdx"
    assert indicators["saved_tdx"]["outputs"][0]["values"][-1] == 78.0
    assert len(indicators["saved_tdx"]["drawings"][0]["points"]) == 5


def test_saved_null_and_empty_schemes_do_not_receive_defaults():
    for saved in (None, {}, ChartLibrary().model_dump()):
        preferences.save({"chart_indicators": saved})
        assert charts.get_library()["library"] == ChartLibrary().model_dump()
        assert preferences.load()["chart_indicators"] == saved


def test_saving_chart_library_does_not_invalidate_data_source_stamp():
    before = preferences.data_source_stamp()
    stored = charts.get_library()
    stored["library"]["definitions"][0]["name"] = "已编辑指标"
    charts.save_library(charts.LibrarySave(library=stored["library"], revision=stored["revision"]))
    assert preferences.data_source_stamp() == before


def test_old_definitions_and_view_parameters_are_preserved_without_reseeding():
    presets = charts.get_library()["library"]
    assert {d["id"] for d in presets["definitions"]} >= {
        "preset_ma",
        "preset_macd",
        "preset_kdj",
        "preset_volume",
    }
    old = {"version": 1, "definitions": [], "views": {"daily": [], "minute": []}}
    custom = copy.deepcopy(presets["definitions"][0])
    custom["id"] = "user_ma"
    old["definitions"] = [custom]
    old["views"]["daily"] = [{"id": "user_ma", "parameters": {"N1": 15}}]
    preferences.save({"chart_indicators": old, "unrelated": "keep"})
    migrated = charts.get_library()
    assert {**old["views"]["daily"][0], "visible": True, "volume_compare": None} in migrated[
        "library"
    ]["views"]["daily"]
    assert next(d for d in migrated["library"]["definitions"] if d["id"] == "user_ma") == custom
    migrated["library"]["definitions"] = [
        d for d in migrated["library"]["definitions"] if d["id"] != "preset_macd"
    ]
    charts.save_library(
        charts.LibrarySave(library=migrated["library"], revision=migrated["revision"])
    )
    assert not any(d["id"] == "preset_macd" for d in charts.get_library()["library"]["definitions"])
    assert preferences.load()["unrelated"] == "keep"
    assert [d["id"] for d in migrated["library"]["definitions"]] == ["user_ma"]
    definition = next(d for d in migrated["library"]["definitions"] if d["id"] == "user_ma")
    definition["parameters"][0]["value"] = 30
    assert (
        next(s for s in migrated["library"]["views"]["daily"] if s["id"] == "user_ma")[
            "parameters"
        ]["N1"]
        == 15
    )


def test_migration_preserves_full_legacy_library():
    seed = charts.get_library()["library"]["definitions"][0]
    old = {
        "version": 1,
        "definitions": [{**seed, "id": f"user_{i}"} for i in range(40)],
        "views": {
            "daily": [{"id": f"user_{i}", "parameters": {}} for i in range(8)],
            "minute": [],
        },
    }
    preferences.save({"chart_indicators": old})
    migrated = charts.get_library()["library"]
    assert len(migrated["definitions"]) == 40
    assert len(migrated["views"]["daily"]) == 8
    ChartLibrary.model_validate(migrated)


@pytest.mark.parametrize("flat", [False, True])
def test_presets_match_legacy_pipeline_on_fixed_history(flat):
    from app.indicators.pipeline import compute_indicators

    close = 10 + np.sin(np.arange(300) / 4) + np.arange(300) / 30
    if flat:
        close = np.full(300, 10.0)
    frame = pl.DataFrame(
        {
            "symbol": ["x"] * 300,
            "date": [f"{i:04}" for i in range(300)],
            "time": [f"{i:04}" for i in range(300)],
            "close": close,
            "open": close - 0.1,
            "high": close + (0 if flat else 0.3),
            "low": close - (0 if flat else 0.4),
            "volume": np.arange(300, dtype=float) + 1,
        }
    )
    legacy = compute_indicators(
        frame,
        needed={
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "ema5",
            "ema10",
            "ema20",
            "ema60",
            "boll_upper",
            "boll_lower",
            "macd_dif",
            "macd_dea",
            "macd_hist",
            "rsi_6",
            "rsi_14",
            "rsi_24",
            "kdj_k",
            "kdj_d",
            "kdj_j",
        },
    )
    expected = {
        "preset_ma": {"MA5": "ma5", "MA10": "ma10", "MA20": "ma20", "MA60": "ma60"},
        "preset_ema": {"EMA5": "ema5", "EMA10": "ema10", "EMA20": "ema20", "EMA60": "ema60"},
        "preset_boll": {"UPPER": "boll_upper", "LOWER": "boll_lower"},
        "preset_macd": {"DIF": "macd_dif", "DEA": "macd_dea", "HIST": "macd_hist"},
        "preset_rsi": {"RSI6": "rsi_6", "RSI14": "rsi_14", "RSI24": "rsi_24"},
        "preset_kdj": {"KVAL": "kdj_k", "DVAL": "kdj_d", "JVAL": "kdj_j"},
    }
    for raw in charts.get_library()["library"]["definitions"]:
        result = evaluate_frame(ChartIndicator.model_validate(raw), frame, "daily")
        for output in result["outputs"]:
            column = expected.get(raw["id"], {}).get(output["key"])
            if column:
                np.testing.assert_allclose(
                    np.array(output["values"], dtype=float),
                    legacy[column].to_numpy(),
                    rtol=1e-10,
                    atol=1e-10,
                    equal_nan=True,
                )


@pytest.mark.parametrize("bad", ["version", "formula", "selection", "visibility", "volume_compare"])
def test_invalid_backup_preserves_all_preferences(bad):
    original = charts.get_library()["library"]
    preferences.save({"chart_indicators": original, "unrelated": "keep"})
    invalid = copy.deepcopy(original)
    if bad == "version":
        invalid["version"] = 99
    elif bad == "formula":
        invalid["definitions"][0]["outputs"][0]["formula"] = "REF(C,-1)"
    elif bad == "visibility":
        invalid["views"]["daily"][0]["visible"] = "false"
    elif bad == "volume_compare":
        invalid["views"]["daily"][0]["volume_compare"] = {"days": 0}
    else:
        invalid["views"]["daily"][0]["parameters"] = {"N1": -1}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"format": config_backup.BACKUP_FORMAT, "version": 1})
        )
        zf.writestr(
            "user_data/preferences.json",
            json.dumps({"chart_indicators": invalid, "unrelated": "lost"}),
        )
    with pytest.raises(config_backup.ConfigBackupError):
        config_backup.inspect_archive(archive.getvalue())
    with pytest.raises(config_backup.ConfigBackupError):
        config_backup.apply_archive(archive.getvalue(), categories=["preferences"])
    assert preferences.load() == {"chart_indicators": original, "unrelated": "keep"}


def test_definition_package_conflicts_revision_and_atomic_validation():
    initial = charts.get_library()
    initial["library"]["views"]["daily"][0]["visible"] = False
    initial = charts.save_library(
        charts.LibrarySave(library=initial["library"], revision=initial["revision"])
    )
    package = charts.export_definitions(["preset_ma"], initial["revision"])
    raw = json.dumps(package).encode()
    assert (
        import_center.inspect_uploads(
            [("indicators.json", raw)], preferences._path().parent.parent
        )["kind"]
        == "chart_indicators"
    )
    assert charts.inspect_definitions(raw)["entries"][0]["action"] == "reuse"
    changed = copy.deepcopy(package)
    changed["definitions"][0]["name"] = "我的均线"
    preview = charts.inspect_definitions(json.dumps(changed).encode())
    assert preview["entries"][0]["action"] == "conflict"
    body = {
        "package": changed,
        "selected": ["preset_ma"],
        "overwrite": [],
        "revision": preview["revision"],
    }
    with pytest.raises(charts.LibraryConflictError):
        charts.import_definitions(charts.DefinitionsImport.model_validate(body))
    body["overwrite"] = ["preset_ma"]
    charts.import_definitions(charts.DefinitionsImport.model_validate(body))
    after = charts.get_library()
    assert after["library"]["views"] == initial["library"]["views"]
    assert after["library"]["definitions"][0]["name"] == "我的均线"
    with pytest.raises(charts.LibraryConflictError):
        charts.import_definitions(charts.DefinitionsImport.model_validate(body))
    changed["definitions"][0]["outputs"][0]["formula"] = "REF(C,-1)"
    with pytest.raises(ValueError):
        charts.inspect_definitions(json.dumps(changed).encode())
    assert charts.get_library() == after


def test_backup_roundtrip_old_backup_and_selected_categories():
    library = charts.get_library()["library"]
    library["views"]["daily"][0]["parameters"] = {"N1": 12}
    library["views"]["daily"][0]["visible"] = False
    library["views"]["daily"][-1]["volume_compare"] = {
        "days": 7,
        "show_labels": False,
        "show_readout": True,
    }
    library["views"]["minute"] = []
    preferences.save({"chart_indicators": library})
    archive, _ = config_backup.build_backup(categories=["preferences"])
    preferences.save(
        {"chart_indicators": {**library, "definitions": [], "views": {"daily": [], "minute": []}}}
    )
    config_backup.apply_archive(archive, categories=[])
    assert not preferences.load()["chart_indicators"]["definitions"]
    config_backup.apply_archive(archive, categories=["preferences"])
    assert charts.get_library()["library"] == library
    old = io.BytesIO()
    with zipfile.ZipFile(old, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"format": config_backup.BACKUP_FORMAT, "version": 1})
        )
        zf.writestr("user_data/preferences.json", json.dumps({"old_setting": True}))
    config_backup.apply_archive(old.getvalue())
    assert charts.get_library()["library"] == library


def test_visibility_defaults_for_old_config_and_saves_per_view_without_changing_parameters():
    initial = charts.get_library()
    raw = copy.deepcopy(initial["library"])
    for choices in raw["views"].values():
        for choice in choices:
            choice.pop("visible", None)
    old = ChartLibrary.model_validate(raw)
    assert all(choice.visible for choices in old.views.values() for choice in choices)
    volume = next(choice for choice in raw["views"]["daily"] if choice["id"] == "preset_volume")
    volume["visible"] = False
    saved = charts.save_library(charts.LibrarySave(library=raw, revision=initial["revision"]))
    assert saved["revision"] != initial["revision"]
    assert saved["library"]["views"]["daily"][-1] == {**volume, "volume_compare": None}
    assert saved["library"]["views"]["minute"][0]["visible"] is True
    with pytest.raises(charts.LibraryConflictError):
        charts.save_library(charts.LibrarySave(library=raw, revision=initial["revision"]))
    assert charts.get_library() == saved


@pytest.mark.parametrize(
    "settings",
    [{"days": 0}, {"days": 21}, {"days": 1.5}, {"show_labels": 1}, {"show_readout": "false"}],
)
def test_invalid_volume_comparison_cannot_replace_saved_library(settings):
    initial = charts.get_library()
    raw = copy.deepcopy(initial["library"])
    raw["views"]["daily"][-1]["volume_compare"] = settings
    with pytest.raises(ValueError):
        charts.LibrarySave(library=raw, revision=initial["revision"])
    assert charts.get_library() == initial


def test_import_api_export_conflicts_and_invalid_packages():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.chart_indicators import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        initial = client.get("/api/chart-indicators/library").json()
        response = client.post(
            "/api/chart-indicators/export",
            json={"ids": ["preset_ma"], "revision": initial["revision"]},
        )
        assert response.status_code == 200
        package = response.json()
        assert (
            client.post(
                "/api/chart-indicators/export",
                json={"ids": ["unknown"], "revision": initial["revision"]},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/chart-indicators/export", json={"ids": ["preset_ma"], "revision": "old"}
            ).status_code
            == 409
        )
        package["definitions"][0]["id"] = "shared_ma"
        body = {"package": package, "selected": ["shared_ma"], "revision": initial["revision"]}
        result = client.post("/api/chart-indicators/import", json=body)
        assert result.status_code == 200
        assert result.json()["library"]["views"] == initial["library"]["views"]
        assert client.post("/api/chart-indicators/import", json=body).status_code == 409
        body["revision"] = result.json()["revision"]
        package["definitions"][0]["outputs"][0]["formula"] = "REF(C,-1)"
        assert client.post("/api/chart-indicators/import", json=body).status_code == 422
        assert client.get("/api/chart-indicators/library").json() == result.json()


def test_candle_colors_and_hidden_outputs_survive_evaluation():

    volume = next(d for d in presets() if d.id == "preset_volume")
    frame = pl.DataFrame(
        {
            "symbol": ["x"] * 3,
            "date": ["1", "2", "3"],
            "time": ["1", "2", "3"],
            "close": [10.0, 9.0, 10.0],
            "open": [9.0, 10.0, 10.0],
            "volume": [20.0, 30.0, 40.0],
        }
    )
    result = evaluate_frame(volume, frame, "daily")
    assert result["outputs"][0]["values"] == [20.0, 30.0, 40.0]
    assert result["outputs"][0]["colors"] == ["#C74040", "#2D9B65", "#C74040"]


def test_parameter_labels_follow_resolved_view_values():
    from app.indicators.chart import resolve_indicator

    ma = next(d for d in presets() if d.id == "preset_ma")
    changed = resolve_indicator(ma, {"N1": 2})
    frame = pl.DataFrame(
        {
            "symbol": ["x"] * 3,
            "date": ["1", "2", "3"],
            "time": ["1", "2", "3"],
            "close": [1.0, 2.0, 3.0],
        }
    )
    assert evaluate_frame(changed, frame, "daily")["outputs"][0]["label"] == "MA2"


def test_legacy_variables_can_share_new_function_names():
    definition = ChartIndicator.model_validate(
        {
            "id": "legacy",
            "name": "旧定义",
            "parameters": [{"name": "SMA_FIRST", "value": 2, "min": 2, "max": 20}],
            "outputs": [
                {"key": "EMA_FIRST", "label": "变量", "formula": "MA(C,SMA_FIRST)"},
                {"key": "LINE1", "label": "曲线", "formula": "EMA_FIRST(EMA_FIRST,2)"},
            ],
        }
    )
    frame = pl.DataFrame(
        {
            "symbol": ["x"] * 3,
            "date": ["1", "2", "3"],
            "time": ["1", "2", "3"],
            "close": [1.0, 2.0, 3.0],
        }
    )
    result = evaluate_frame(definition, frame, "daily")
    assert result["outputs"][0]["values"] == [None, 1.5, 2.5]
    assert result["outputs"][1]["values"][-1] == pytest.approx(13 / 6)


def test_invalid_legacy_library_cannot_replace_saved_preferences():
    original = charts.get_library()
    raw = {
        "version": 1,
        "definitions": [
            {**original["library"]["definitions"][0], "id": f"user_{i}"} for i in range(49)
        ],
        "views": {"daily": [], "minute": []},
    }
    with pytest.raises(ValueError):
        charts.save_library(charts.LibrarySave(library=raw, revision=original["revision"]))
    assert charts.get_library() == original


def test_source_library_package_backup_roundtrip_and_atomic_validation():
    initial = charts.get_library()
    raw = {
        "id": "tdx_user",
        "name": "我的源码",
        "syntax": "tdx",
        "source": "{原样保存中文源码}\n周期:=N; 均线:MA(C,周期),COLORYELLOW; DRAWICON(CROSS(C,均线),L,1);",
        "parameters": [{"name": "N", "value": 5, "min": 2, "max": 60, "integer": True}],
        "outputs": [],
        "pane": "main",
        "format": "price",
    }
    initial["library"]["definitions"].append(raw)
    initial["library"]["views"]["daily"].append(
        {"id": "tdx_user", "parameters": {"N": 9}, "visible": False}
    )
    saved = charts.save_library(
        charts.LibrarySave(library=initial["library"], revision=initial["revision"])
    )
    package = charts.export_definitions(["tdx_user"], saved["revision"])
    assert package["definitions"][0]["source"] == raw["source"]
    assert (
        charts.inspect_definitions(json.dumps(package).encode())["entries"][0]["action"] == "reuse"
    )
    archive, _ = config_backup.build_backup(categories=["preferences"])
    preferences.save(
        {
            "chart_indicators": {
                "version": 1,
                "definitions": [],
                "views": {"daily": [], "minute": []},
            }
        }
    )
    config_backup.apply_archive(archive, categories=["preferences"])
    assert charts.get_library()["library"] == saved["library"]
    invalid = copy.deepcopy(package)
    invalid["definitions"][0]["source"] = "A:MA(C,5);\nB:BACKSET(C>O,3);"
    with pytest.raises(ValueError, match="第2行"):
        charts.inspect_definitions(json.dumps(invalid).encode())
    assert charts.get_library()["library"] == saved["library"]
    bad_library = copy.deepcopy(saved["library"])
    next(d for d in bad_library["definitions"] if d["id"] == "tdx_user")["source"] = "A:UNKNOWN(C);"
    with pytest.raises(ValueError):
        charts.save_library(charts.LibrarySave(library=bad_library, revision=saved["revision"]))
    assert charts.get_library()["library"] == saved["library"]
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"format": config_backup.BACKUP_FORMAT, "version": 1})
        )
        zf.writestr("user_data/preferences.json", json.dumps({"chart_indicators": bad_library}))
    with pytest.raises(config_backup.ConfigBackupError):
        config_backup.apply_archive(bad.getvalue(), categories=["preferences"])
    assert charts.get_library()["library"] == saved["library"]


def test_exported_source_packages_larger_than_old_limit_can_be_reimported():
    initial = charts.get_library()
    source = "{" + "中" * 39980 + "};\n均线:MA(C,5);"
    initial["library"]["definitions"].extend(
        [
            {
                "id": f"large_{i}",
                "name": f"源码{i}",
                "syntax": "tdx",
                "source": source,
                "outputs": [],
            }
            for i in range(10)
        ]
    )
    saved = charts.save_library(
        charts.LibrarySave(library=initial["library"], revision=initial["revision"])
    )
    package = charts.export_definitions([f"large_{i}" for i in range(10)], saved["revision"])
    payload = json.dumps(package, ensure_ascii=False).encode()
    assert len(payload) > 1024 * 1024
    assert len(charts.inspect_definitions(payload)["entries"]) == 10
