"""The host must consume the independent implementation, not a copied matcher."""

import json
from dataclasses import asdict
from pathlib import Path

import polars as pl
from tivelo_backtest.legacy import position_sizing
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig, SimulationOptions
from tivelo_backtest.research.matrix import MarketMatrix, build_market_matrix

from app.backtest import engine, matrix
from app.backtest import position_sizing as host_position_sizing


def test_shared_types_and_position_contract_have_one_identity():
    assert engine.MatcherConfig is MatcherConfig
    assert engine.SimulationOptions is SimulationOptions
    assert matrix.MarketMatrix is MarketMatrix
    assert host_position_sizing is position_sizing


def test_host_delegates_execution_and_replays_fixed_numeric_baseline(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads(
        (root / "docs/engine-platform/evidence/legacy-numeric-baseline.json").read_text()
    )
    frame = pl.DataFrame(fixture["fixture"]["rows"]).with_columns(pl.col("date").str.to_date())
    prepared = build_market_matrix(
        frame,
        pl.Series(fixture["fixture"]["entries"]),
        pl.Series(fixture["fixture"]["exits"]),
        entry_delay_bars=1,
        exit_delay_bars=1,
    )
    calls = []
    original = LegacySimulator.simulate_market_matrix

    def execute(self, *args, **kwargs):
        calls.append(args[0])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LegacySimulator, "simulate_market_matrix", execute)
    result = engine.BacktestEngine(None).simulate_market_matrix(
        prepared,
        MatcherConfig(**fixture["config"]),
        options=SimulationOptions(**fixture["options"]),
    )
    assert calls == [prepared]
    payload = asdict(result)
    payload["stats"].pop("statistics_ms", None)
    assert json.loads(json.dumps(payload, default=str)) == fixture["result"]
