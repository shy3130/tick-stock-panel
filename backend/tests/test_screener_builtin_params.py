from __future__ import annotations

import random
import types
from datetime import date
from pathlib import Path
from typing import ClassVar

import polars as pl

from app.api import screener as screener_api
from app.services.screener import PRESET_STRATEGIES, ScreenerResult, ScreenerService
from app.strategy.engine import StrategyEngine


def _builtin_engine() -> StrategyEngine:
    builtin_dir = Path(__file__).parents[1] / "app" / "strategy" / "builtin"
    return StrategyEngine(
        enriched_loader=lambda _as_of: pl.DataFrame(),
        strategy_dirs=[builtin_dir],
    )


def _comparison_frame() -> pl.DataFrame:
    rng = random.Random(20260715)
    size = 200
    return pl.DataFrame({
        "symbol": [f"S{i:03d}" for i in range(size)],
        "close": [rng.uniform(5, 100) for _ in range(size)],
        "open": [rng.uniform(5, 100) for _ in range(size)],
        "ma5": [rng.uniform(5, 100) for _ in range(size)],
        "ma10": [rng.uniform(5, 100) for _ in range(size)],
        "ma20": [rng.uniform(5, 100) for _ in range(size)],
        "ma60": [rng.uniform(5, 100) for _ in range(size)],
        "vol_ratio_5d": [rng.uniform(0, 4) for _ in range(size)],
        "momentum_20d": [rng.uniform(-0.5, 0.5) for _ in range(size)],
        "momentum_60d": [rng.uniform(-0.5, 0.5) for _ in range(size)],
        "annual_vol_20d": [rng.uniform(0, 0.6) for _ in range(size)],
        "change_pct": [rng.uniform(-0.1, 0.1) for _ in range(size)],
        "rsi_14": [rng.uniform(0, 100) for _ in range(size)],
        "consecutive_limit_ups": [rng.randrange(0, 5) for _ in range(size)],
        "signal_n_day_high": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_ma_golden_5_20": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_macd_golden": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_ma20_breakout": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_limit_up": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_boll_breakout_upper": [rng.choice([True, False, None]) for _ in range(size)],
        "signal_n_day_low": [rng.choice([True, False, None]) for _ in range(size)],
    })


def test_builtin_default_filters_match_legacy_presets():
    engine = _builtin_engine()
    df = _comparison_frame()

    for strategy_id, preset in PRESET_STRATEGIES.items():
        strategy = engine.get(strategy_id)
        defaults = {item["id"]: item["default"] for item in strategy.meta["params"]}
        expected = df.filter(preset["filter"])["symbol"].to_list()
        actual = df.filter(strategy.filter_fn(df, defaults))["symbol"].to_list()
        assert actual == expected, strategy_id


def test_run_preset_applies_numeric_and_boolean_strategy_params():
    engine = _builtin_engine()
    strategy = engine.get("trend_breakout")
    df = pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "close": [10.0, 10.0, 10.0],
        "ma60": [9.0, 9.0, 9.0],
        "signal_n_day_high": [True, True, True],
        "vol_ratio_5d": [1.0, 2.5, 3.5],
        "momentum_60d": [0.1, 0.2, 0.3],
    })
    service = ScreenerService(types.SimpleNamespace())

    strict = service.run_preset(
        "trend_breakout",
        as_of=date(2026, 7, 15),
        precomputed=df,
        filter_fn=strategy.filter_fn,
        strategy_params={"vol_ratio_min": 3.0},
    )
    volume_disabled = service.run_preset(
        "trend_breakout",
        as_of=date(2026, 7, 15),
        precomputed=df,
        filter_fn=strategy.filter_fn,
        strategy_params={"use_volume_filter": False},
    )

    assert [row["symbol"] for row in strict.rows] == ["C"]
    assert [row["symbol"] for row in volume_disabled.rows] == ["C", "B", "A"]


class _CapturingScreenerService:
    calls: ClassVar[list[dict]] = []

    def __init__(self, repo, asset_type="stock"):
        self.repo = repo
        self.asset_type = asset_type

    def latest_date(self):
        return date(2026, 7, 15)

    def _load_enriched_for_date(self, _as_of):
        return pl.DataFrame({"symbol": ["A"]})

    def run_preset(self, strategy_id, as_of, **kwargs):
        self.calls.append({"strategy_id": strategy_id, **kwargs})
        return ScreenerResult(as_of=as_of, strategy=strategy_id)


def _api_request(tmp_path, engine):
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))
    state = types.SimpleNamespace(repo=repo, strategy_engine=engine)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_single_run_passes_saved_params_to_builtin_filter(monkeypatch, tmp_path):
    engine = _builtin_engine()
    request = _api_request(tmp_path, engine)
    _CapturingScreenerService.calls = []
    monkeypatch.setattr(screener_api, "ScreenerService", _CapturingScreenerService)
    monkeypatch.setattr(
        screener_api.strategy_config,
        "load_override",
        lambda *_args: {"params": {"vol_ratio_min": 3.0}},
    )
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *_args: {})
    monkeypatch.setattr(screener_api, "_update_cache_strategy", lambda *_args: None)

    screener_api.run_preset(
        screener_api.PresetRequest(
            strategy_id="trend_breakout",
            as_of=date(2026, 7, 15),
        ),
        request,
    )

    call = _CapturingScreenerService.calls[0]
    assert call["filter_fn"] is not None
    assert call["strategy_params"] == {"vol_ratio_min": 3.0}


def test_batch_run_passes_saved_params_to_builtin_filter(monkeypatch, tmp_path):
    engine = _builtin_engine()
    request = _api_request(tmp_path, engine)
    _CapturingScreenerService.calls = []
    monkeypatch.setattr(screener_api, "ScreenerService", _CapturingScreenerService)
    monkeypatch.setattr(
        screener_api.strategy_config,
        "list_overrides",
        lambda *_args: {"trend_breakout": {"params": {"vol_ratio_min": 3.0}}},
    )
    monkeypatch.setattr(screener_api.strategy_cache, "write_cache", lambda *_args: None)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *_args: {})

    screener_api.run_all(
        request,
        body={"as_of": "2026-07-15", "strategy_ids": ["trend_breakout"]},
    )

    call = _CapturingScreenerService.calls[0]
    assert call["filter_fn"] is not None
    assert call["strategy_params"] == {"vol_ratio_min": 3.0}
