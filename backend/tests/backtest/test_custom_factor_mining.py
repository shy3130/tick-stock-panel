from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.factor import FactorBacktestService, FactorBatchConfig
from app.backtest.matrix import MatrixComputeCache, build_market_data_matrix, matrix_feature
from app.backtest.mining_runtime import _load_compact_factor_panel
from app.factors.registry import isolated_registry
from app.factors.store import register_definition


@pytest.fixture(autouse=True)
def factor_registry():
    with isolated_registry():
        yield


def _register(fid: str, formula: str, **extra):
    return register_definition(
        {
            "id": fid,
            "label": fid,
            "kind": "custom",
            "formula": formula,
            **extra,
        }
    )


def _panel(days: int = 280) -> pl.DataFrame:
    first = date(2024, 1, 2)
    dates = [first + timedelta(days=i) for i in range(days * 2)]
    dates = [day for day in dates if day.weekday() < 5][:days]
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "date": day,
                "open": 10.0 + asset + i * 0.1,
                "high": 11.0 + asset + i * 0.1,
                "low": 9.0 + asset + i * 0.1,
                "close": 10.5 + asset + i * (0.1 + asset * 0.01),
                "volume": 1000.0 + asset * 100 + i,
                "amount": 1_000_000.0 + i * 100,
                "turnover_rate": 1.0 + asset + i * 0.01,
                "consecutive_limit_ups": int(i % (asset + 2) == 0),
            }
            for asset, symbol in enumerate(("A", "B", "C"))
            for i, day in enumerate(dates)
        ]
    ).sort(["symbol", "date"])


class _Engine:
    def __init__(self, panel, data_dir=None):
        self.panel = panel
        self.calls = []
        if data_dir is not None:
            self.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))

    def load_panel(self, symbols, start, end, *, columns, **kwargs):
        self.calls.append({"start": start, "columns": columns})
        return self.panel.filter(pl.col("date").is_between(start, end)).select(columns)


@pytest.mark.parametrize("lang", ["dsl", "python"])
def test_custom_factor_resolves_virtual_and_nested_custom_inputs(lang):
    formula = "ma20_bias" if lang == "dsl" else 'ctx.col("ma20_bias")'
    _register("uf_child", formula, lang=lang)
    _register("uf_parent", "uf_child * 2")
    raw = _panel(30)
    actual = FactorBacktestService._compute_missing_factors(raw, {"uf_parent"})
    expected = raw.with_columns(
        ((pl.col("close") / pl.col("close").rolling_mean(20).over("symbol") - 1) * 2).alias(
            "expected"
        )
    )["expected"]
    assert actual["uf_parent"].equals(expected.rename("uf_parent"))


def test_composite_materializes_custom_members_before_combining():
    _register("uf_child", "close / open - 1")
    register_definition(
        {
            "id": "cf_parent",
            "label": "组合",
            "kind": "composite",
            "members": {"uf_child": 1.0, "turnover_rate": 0.5},
        }
    )
    raw = _panel(5)
    actual = FactorBacktestService._compute_missing_factors(raw, {"cf_parent"})
    child = pl.col("close") / pl.col("open") - 1
    turnover = pl.col("turnover_rate")
    expected = raw.select(
        (
            (child - child.mean().over("date")) / child.std().over("date")
            + 0.5 * (turnover - turnover.mean().over("date")) / turnover.std().over("date")
        ).alias("cf_parent")
    )["cf_parent"]
    assert np.allclose(actual["cf_parent"].to_numpy(), expected.to_numpy())


@pytest.mark.parametrize("compact", [False, True])
def test_long_window_loads_actual_trading_history_before_formal_start(tmp_path, compact):
    raw = _panel()
    dates = sorted(raw["date"].unique())
    # A long exchange closure makes a calendar-day multiplier insufficient.
    raw = raw.with_columns(
        pl.when(pl.col("date") >= dates[200])
        .then(pl.col("date") + timedelta(days=90))
        .otherwise(pl.col("date"))
        .alias("date")
    )
    dates = sorted(raw["date"].unique())
    for day in dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={day}"
        partition.mkdir(parents=True)
        (partition / "part.parquet").touch()
    _register("uf_long", "ts_mean(close, 200)")
    _register("uf_nested", "uf_long * 2")
    service = FactorBacktestService(_Engine(raw, tmp_path))
    config = FactorBatchConfig(
        factor_names=["uf_nested"],
        symbols=None,
        start=dates[240],
        end=dates[245],
    )
    if compact:
        actual = _load_compact_factor_panel(
            service, config, config.factor_names, expected_generation="test", cancel_check=None
        )
    else:
        actual = service._load_factor_panel(config, config.factor_names)
    actual = actual.filter(pl.col("date").is_between(config.start, config.end)).sort(
        ["symbol", "date"]
    )
    expected = raw.with_columns(
        (pl.col("close").rolling_mean(200).over("symbol") * 2).alias("expected")
    )
    expected = expected.filter(pl.col("date").is_between(config.start, config.end))["expected"]
    assert actual["uf_nested"].is_not_null().all()
    assert np.allclose(actual["uf_nested"].to_numpy(), expected.to_numpy(), rtol=1e-6)
    assert service.engine.calls[0]["start"] <= dates[40]


def test_compact_panel_keeps_full_cross_section_for_custom_rank(monkeypatch):
    _register("uf_rank", "rank(turnover_rate)")
    raw = _panel(3)
    dates = sorted(raw["date"].unique())
    service = FactorBacktestService(_Engine(raw))
    config = FactorBatchConfig(
        factor_names=["uf_rank"], symbols=None, start=dates[0], end=dates[-1]
    )
    monkeypatch.setattr("app.backtest.mining_runtime._SYMBOL_BATCH_SIZE", 1)
    actual = _load_compact_factor_panel(
        service, config, config.factor_names, expected_generation="test", cancel_check=None
    )
    expected = FactorBacktestService._compute_missing_factors(raw, {"uf_rank"})
    expected = expected.select("symbol", "date", pl.col("uf_rank").cast(pl.Float32)).sort(
        ["date", "symbol"]
    )
    assert actual.select(expected.columns).equals(expected)


def test_matrix_custom_factor_matches_panel_with_suspension_and_updates_cache():
    _register("uf_child", "rank(ts_mean(close, 3))")
    _register("uf_parent", "uf_child * 2")
    raw = _panel(8).filter(~((pl.col("symbol") == "B") & (pl.col("date") == date(2024, 1, 4))))
    market = build_market_data_matrix(raw, field_columns={"turnover_rate"})
    expected = FactorBacktestService._compute_missing_factors(raw, {"uf_parent"})
    expected_market = build_market_data_matrix(expected, field_columns={"uf_parent"})
    cache = MatrixComputeCache()
    with cache.activate(market):
        actual = matrix_feature(market, "uf_parent")
        assert np.allclose(actual, expected_market.field("uf_parent"), equal_nan=True)
        assert matrix_feature(market, "uf_parent") is actual
        _register("uf_child", "rank(ts_mean(close, 3)) * 3", version=2)
        updated = matrix_feature(market, "uf_parent")
        assert np.allclose(updated, actual * 3, equal_nan=True)
    cache.close()


def test_dependency_cycle_fails_with_readable_error():
    _register("uf_child", "close")
    _register("uf_parent", "uf_child * 2")
    with pytest.raises(ValueError, match="cycle"):
        _register("uf_child", "uf_parent * 2", version=2)
    panel = FactorBacktestService._compute_missing_factors(_panel(3), {"uf_parent"})
    assert panel["uf_parent"].equals((panel["close"] * 2).alias("uf_parent"))


def test_nested_dependency_plan_tracks_child_formula_edits():
    from app.strategy.scoring import scoring_dependencies

    _register("uf_child", "close")
    _register("uf_parent", "uf_child * 2")
    _register("uf_child", "roe_latest", version=2)
    assert "roe_latest" in scoring_dependencies({"uf_parent": 1.0})


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_custom_financial_dependency_keeps_announcement_gate(tmp_path, compact, available):
    from app.backtest.fundamentals import FUNDAMENTAL_FACTORS

    raw = _panel(5)
    dates = sorted(raw["date"].unique())
    if available:
        root = tmp_path / "financials" / "metrics"
        root.mkdir(parents=True)
        pl.DataFrame(
            {
                "symbol": ["A", "B", "C"],
                "announce_date": [str(dates[0])] * 3,
                **{item["column"]: [2.0] * 3 for item in FUNDAMENTAL_FACTORS.values()},
            }
        ).write_parquet(root / "part.parquet")
    _register("uf_fin", "1 / pb_latest")
    service = FactorBacktestService(_Engine(raw, tmp_path))
    config = FactorBatchConfig(factor_names=["uf_fin"], symbols=None, start=dates[0], end=dates[-1])
    if compact:
        result = _load_compact_factor_panel(
            service, config, config.factor_names, expected_generation="test", cancel_check=None
        )
    else:
        result = service._load_factor_panel(config, config.factor_names)
    assert result.filter(pl.col("date") == dates[0])["uf_fin"].is_null().all()
    later = result.filter(pl.col("date") > dates[0])
    if available:
        assert np.allclose(later["uf_fin"].to_numpy(), 2.0 / later["close"].to_numpy())
    else:
        assert later["uf_fin"].is_null().all()


def test_custom_limit_up_dependency_loads_persisted_state():
    raw = _panel(30)
    dates = sorted(raw["date"].unique())
    _register("uf_limit", "limit_up_count_20d * 2")
    service = FactorBacktestService(_Engine(raw))
    config = FactorBatchConfig(
        factor_names=["uf_limit"], symbols=None, start=dates[20], end=dates[-1]
    )
    result = _load_compact_factor_panel(
        service, config, config.factor_names, expected_generation="test", cancel_check=None
    )
    expected = raw.with_columns(
        (
            (pl.col("consecutive_limit_ups") > 0).cast(pl.Float64).rolling_sum(20).over("symbol")
            * 2
        ).alias("expected")
    )
    expected = expected.filter(pl.col("date") >= dates[20]).sort(["date", "symbol"])
    assert np.allclose(result["uf_limit"].to_numpy(), expected["expected"].to_numpy())


@pytest.mark.parametrize("lang", ["dsl", "python"])
def test_parent_tracks_replaced_child_in_scoring_and_matrix(lang):
    from app.factors.registry import factor_dependencies
    from app.strategy.scoring import materialize_scoring_columns, scoring_warmup_bars

    _register("uf_child", "pb_latest")
    formula = (
        "ts_mean(uf_child, 80)"
        if lang == "dsl"
        else 'ctx.col("uf_child").rolling_mean(80).over("symbol")'
    )
    _register("uf_parent", formula, lang=lang)
    _register("uf_child", "ts_mean(close, 200)", version=2)
    assert factor_dependencies(["uf_parent"]) == {"close"}
    assert scoring_warmup_bars({"uf_parent": 1}) == 281
    raw = _panel(336)
    expected = raw.with_columns(pl.col("close").rolling_mean(200).over("symbol").alias("inner"))
    expected = expected.with_columns(
        pl.col("inner").rolling_mean(80).over("symbol").alias("uf_parent")
    )
    actual = materialize_scoring_columns(raw, ["uf_parent"])
    assert set(actual.columns) == {*raw.columns, "uf_parent"}
    assert actual["uf_parent"].equals(expected["uf_parent"])

    matrix = build_market_data_matrix(raw)
    expected_matrix = build_market_data_matrix(expected, field_columns={"uf_parent"})
    assert np.allclose(
        matrix_feature(matrix, "uf_parent"), expected_matrix.field("uf_parent"), equal_nan=True
    )


def test_nested_inline_windows_match_full_history_from_first_formal_day(tmp_path):
    from app.strategy.engine import StrategyEngine
    from app.strategy.research.factor_rank_research import FactorRankResearchMatrixStrategy

    raw = _panel(345)
    dates = sorted(raw["date"].unique())
    raw = raw.with_columns(
        pl.when(pl.col("date") >= dates[200])
        .then(pl.col("date") + timedelta(days=120))
        .otherwise(pl.col("date"))
        .alias("date")
    )
    dates = sorted(raw["date"].unique())
    for day in dates:
        part = tmp_path / "kline_daily_enriched" / f"date={day}"
        part.mkdir(parents=True)
        (part / "part.parquet").touch()
    _register("uf_nested", "ts_mean(ts_mean(close, 200) + 1, 80)")
    service = FactorBacktestService(_Engine(raw, tmp_path))
    config = FactorBatchConfig(
        factor_names=["uf_nested"], symbols=None, start=dates[320], end=dates[-1]
    )
    result = _load_compact_factor_panel(
        service, config, config.factor_names, expected_generation="x", cancel_check=None
    )
    expected = raw.with_columns(
        (pl.col("close").rolling_mean(200).over("symbol") + 1).alias("inner")
    )
    expected = expected.with_columns(
        pl.col("inner").rolling_mean(80).over("symbol").alias("expected")
    )
    expected = expected.filter(pl.col("date") >= config.start).sort(["date", "symbol"])
    assert np.allclose(result["uf_nested"].to_numpy(), expected["expected"].to_numpy())
    strategy = SimpleNamespace(matrix_strategy=FactorRankResearchMatrixStrategy({"uf_nested": 1}))
    assert StrategyEngine._matrix_history_bars(strategy, {}) >= 279


def test_shared_composite_member_is_valid():
    from app.strategy.scoring import materialize_scoring_columns

    def combo(fid, members):
        register_definition({"id": fid, "label": fid, "kind": "composite", "members": members})

    combo("cf_first", {"open": 1, "close": 1})
    combo("cf_second", {"cf_first": 1, "close": 1})
    raw = _panel(3)
    scored = materialize_scoring_columns(raw, ["cf_second"])
    assert scored["cf_second"].is_finite().all()
    expected = build_market_data_matrix(scored, field_columns={"cf_second"}).field("cf_second")
    assert np.allclose(
        matrix_feature(build_market_data_matrix(raw), "cf_second"), expected, atol=1e-6
    )


def test_outer_window_reserves_history_to_build_underlying_indicator():
    from app.factors.registry import factor_warmup_bars

    _register("uf_ma_outer", "ts_mean(ma60_bias, 200)")
    bars = factor_warmup_bars(["uf_ma_outer"])
    assert bars >= 259
    frame = _panel(320)
    dates = frame["date"].unique().sort()
    truncated = frame.filter(pl.col("date") >= dates[-bars])
    actual = FactorBacktestService._compute_missing_factors(truncated, {"uf_ma_outer"})
    golden = frame.with_columns(
        (pl.col("close") / pl.col("close").rolling_mean(60).over("symbol") - 1).alias("bias")
    )
    golden = golden.with_columns(pl.col("bias").rolling_mean(200).over("symbol").alias("expected"))
    np.testing.assert_allclose(
        actual.filter(pl.col("date") == dates[-1])["uf_ma_outer"],
        golden.filter(pl.col("date") == dates[-1])["expected"],
    )


def test_dangling_child_is_rejected_by_mining_and_backtest_entry_points(tmp_path):
    from fastapi import HTTPException

    from app.api.backtest import FactorBacktestRequest, FactorBatchRequest, factor_batch, factor_run
    from app.api.mining import MiningStartRequest
    from app.backtest.mining_runtime import _decode_runtime_request
    from app.factors.registry import unregister_factor

    _register("uf_child", "close")
    _register("uf_parent", "uf_child * 2")
    unregister_factor("uf_child")
    with pytest.raises(ValueError, match="uf_child"):
        MiningStartRequest(factor_names=["uf_parent"])
    with pytest.raises(ValueError, match="uf_child"):
        _decode_runtime_request(
            {"run_id": "run", "request": {"factor_names": ["uf_parent"]}}, tmp_path, None
        )
    for handler, request in [
        (factor_run, FactorBacktestRequest(factor_name="uf_parent")),
        (factor_batch, FactorBatchRequest(factor_names=["uf_parent"])),
    ]:
        with pytest.raises(HTTPException) as exc:
            handler(request, None)
        assert exc.value.status_code == 400 and "uf_child" in exc.value.detail


def test_queued_mining_rejects_edited_child_before_loading_market_data(tmp_path, monkeypatch):
    from app.backtest import mining_runtime
    from app.services.mining_schedule import mining_factor_snapshot

    _register("uf_child", "close")
    _register("uf_parent", "uf_child * 2")
    fingerprint = {"generation": "test", "factors": mining_factor_snapshot(["uf_parent"])}
    _register("uf_child", "open", version=2)
    dates = sorted(_panel()["date"].unique())
    monkeypatch.setattr(mining_runtime, "enriched_partition_dates", lambda *_: dates)
    payload = {
        "run_id": "queued",
        "request": {"factor_names": ["uf_parent"], "budget_profile": "exploratory"},
        "data_fingerprint": fingerprint,
    }
    with pytest.raises(ValueError, match="uf_child"):
        mining_runtime.run_mining_runtime(
            payload, data_dir=tmp_path, service=None, strategy_engine=None
        )
