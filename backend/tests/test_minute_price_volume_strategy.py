from __future__ import annotations

import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import definitions as definitions_api
from app.api.strategy import StrategyCodeSaveRequest, save_strategy_code_core
from app.config import settings
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyDataContext, StrategyEngine

STRATEGY_PATH = (
    Path(__file__).parent / "fixtures" / "strategies_minute"
    / "custom_minute_price_volume.py"
)
START = datetime(2026, 10, 20, 9, 31)


@pytest.fixture
def strategy():
    spec = importlib.util.spec_from_file_location("minute_price_volume", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(
    *,
    symbol="600001.SH",
    start=START,
    count=21,
    last_close=10.2,
    last_volume=300.0,
):
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "datetime": [start + timedelta(minutes=i) for i in range(count)],
        "open": [10.0] * count,
        "high": [max(10.0, last_close)] * count,
        "low": [min(10.0, last_close)] * count,
        "close": [10.0] * (count - 1) + [last_close],
        "volume": [100.0] * (count - 1) + [last_volume],
        "amount": [100_000.0] * count,
    })


def test_exact_thresholds_and_previous_twenty_bar_baseline(strategy):
    result = strategy.filter_minute_history(_bars(), {})

    assert result["symbol"].to_list() == ["600001.SH"]
    row = result.row(0, named=True)
    assert row["minute_change_pct"] == pytest.approx(0.02)
    assert row["minute_volume_ratio"] == pytest.approx(3.0)
    assert row["previous_mean_volume"] == 100.0
    assert row["last_datetime"] == START + timedelta(minutes=20)


@pytest.mark.parametrize(
    "last_close,last_volume", [(10.19, 300.0), (10.2, 299.0), (10.19, 299.0)],
)
def test_both_conditions_are_required(strategy, last_close, last_volume):
    bars = _bars(last_close=last_close, last_volume=last_volume)
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_baseline_is_twenty_bar_average_not_previous_minute(strategy):
    bars = _bars(last_close=10.25, last_volume=600.0).with_columns(
        pl.when(pl.col("datetime") == START + timedelta(minutes=19))
        .then(300.0).otherwise(pl.col("volume")).alias("volume")
    )
    row = strategy.filter_minute_history(bars, {}).row(0, named=True)

    assert row["previous_mean_volume"] == 110.0
    assert row["minute_volume_ratio"] == pytest.approx(600.0 / 110.0)


def test_parameters_can_be_adjusted(strategy):
    params = {"min_change_pct": 1.0, "volume_window": 5, "volume_multiple": 2.0}
    bars = _bars(count=6, last_close=10.1, last_volume=200.0)
    assert strategy.filter_minute_history(bars, params).height == 1
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_only_latest_bar_can_match(strategy):
    bars = pl.concat([
        _bars(),
        _bars(start=START + timedelta(minutes=21), count=1, last_close=10.2, last_volume=100.0),
    ])
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_latest_day_and_symbols_are_isolated(strategy):
    bars = pl.concat([
        _bars(start=START - timedelta(days=1)),
        _bars(count=10),
        _bars(symbol="600002.SH"),
        _bars(symbol="600003.SH", last_volume=299.0),
    ]).reverse()
    result = strategy.filter_minute_history(bars, {})
    assert result["symbol"].to_list() == ["600002.SH"]


def test_window_restarts_after_lunch(strategy):
    bars = pl.concat([
        _bars(start=datetime(2026, 10, 20, 11, 10)),
        _bars(start=datetime(2026, 10, 20, 13, 0), count=10),
    ])
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_missing_minute_does_not_extend_the_baseline(strategy):
    bars = _bars(count=22).filter(pl.col("datetime") != START + timedelta(minutes=10))
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_duplicate_minutes_do_not_change_the_baseline(strategy):
    bars = _bars()
    result = strategy.filter_minute_history(pl.concat([bars, bars]), {})
    assert result.height == 1
    assert result["previous_mean_volume"][0] == 100.0


@pytest.mark.parametrize("bad_value", [None, -1.0, float("nan"), float("inf")])
def test_invalid_historical_volume_fails_closed(strategy, bad_value):
    bars = _bars().with_columns(
        pl.when(pl.col("datetime") == START + timedelta(minutes=10))
        .then(pl.lit(bad_value, dtype=pl.Float64))
        .otherwise(pl.col("volume")).alias("volume")
    )
    assert strategy.filter_minute_history(bars, {}).is_empty()


@pytest.mark.parametrize("column", ["close", "volume"])
@pytest.mark.parametrize("bad_value", [None, -1.0, float("nan"), float("inf")])
def test_invalid_latest_price_or_volume_fails_closed(strategy, column, bad_value):
    bars = _bars().with_columns(
        pl.when(pl.col("datetime") == START + timedelta(minutes=20))
        .then(pl.lit(bad_value, dtype=pl.Float64))
        .otherwise(pl.col(column)).alias(column)
    )
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_zero_baseline_fails_closed(strategy):
    bars = _bars().with_columns(
        pl.when(pl.col("datetime") < START + timedelta(minutes=20))
        .then(0.0).otherwise(pl.col("volume")).alias("volume")
    )
    assert strategy.filter_minute_history(bars, {}).is_empty()


def test_empty_input_preserves_output_schema(strategy):
    result = strategy.filter_minute_history(_bars().head(0), {})
    assert result.is_empty()
    assert {"symbol", "close", "last_datetime", "minute_change_pct"} <= set(result.columns)


def test_current_minute_is_excluded_and_timezone_is_beijing(strategy, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 1, 51, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(strategy, "datetime", FrozenDateTime)
    assert strategy.filter_minute_history(_bars(), {}).is_empty()

    # 09:51 这一根留待下一分钟评估，上一根完成线满足时仍可正常命中。
    earlier = _bars(start=START - timedelta(minutes=1))
    result = strategy.filter_minute_history(earlier, {})
    assert result.height == 1
    assert result["last_datetime"][0].hour == 9


def test_timezone_aware_data_is_normalized(strategy):
    bars = _bars().with_columns(
        pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC")
    )
    result = strategy.filter_minute_history(bars, {})
    assert result.height == 1
    assert result["last_datetime"][0] == START + timedelta(minutes=20)


@pytest.mark.parametrize(
    "params", [{"volume_window": 0}, {"volume_window": 121},
               {"min_change_pct": -1}, {"volume_multiple": 0}],
)
def test_invalid_parameters_are_rejected(strategy, params):
    with pytest.raises(ValueError):
        strategy.filter_minute_history(_bars(), params)


def test_import_save_and_engine_execution(tmp_path):
    code = STRATEGY_PATH.read_text(encoding="utf-8")
    AIStrategyGenerator._validate_safety(code)
    engine = StrategyEngine([tmp_path / "strategies" / "custom"])
    saved = save_strategy_code_core(
        StrategyCodeSaveRequest(
            strategy_id="custom_minute_price_volume", code=code,
            target_source="custom", mode="create",
        ), engine, tmp_path,
    )
    assert saved["ok"] is True
    context = StrategyDataContext(
        asset_type="stock", timeframe="1m", as_of=date(2026, 10, 20),
        current=pl.DataFrame({"symbol": ["600001.SH"], "name": ["测试标的"]}),
        history=_bars(),
    )
    result = engine.run("custom_minute_price_volume", context)
    assert result.total == 1
    assert result.rows[0]["name"] == "测试标的"
    assert result.rows[0]["minute_volume_ratio"] == 3.0


def test_py_file_import_through_resource_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = StrategyEngine([tmp_path / "strategies" / "custom"])
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = engine
    app.include_router(definitions_api.router)
    files = [("files", (STRATEGY_PATH.name, STRATEGY_PATH.read_bytes(), "text/x-python"))]

    with TestClient(app) as client:
        response = client.post("/api/definitions/resources/inspect", files=files)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["roots"] == ["strategy:custom_minute_price_volume"]
        assert preview["entries"][0]["issues"] == []

        response = client.post(
            "/api/definitions/resources/apply", files=files,
            data={"selected": json.dumps(preview["roots"]), "revision": preview["revision"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == 1

    imported = tmp_path / "strategies" / "custom" / STRATEGY_PATH.name
    assert imported.read_bytes() == STRATEGY_PATH.read_bytes()
    assert engine.get("custom_minute_price_volume").execution_backend == "minute_filter"
