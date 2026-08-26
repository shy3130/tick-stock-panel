from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.mining import (
    MiningCandidate,
    NestedValidationConfig,
    generate_nested_folds,
)
from app.backtest.mining_runtime import (
    TrainingMetricProvider,
    _decode_runtime_request,
    _load_compact_factor_panel,
    _prepare_base_market,
    _rank_artifact_candidates,
    _regime_date_count,
    _validate_regime_availability,
    attach_single_forward_return,
)
from app.services import regime_builder


def test_runtime_rejects_insufficient_balanced_range_before_loading_panel(
    tmp_path,
) -> None:
    first = date(2023, 10, 13)
    dates = [first + timedelta(days=offset) for offset in range(690)]
    for value in dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={value.isoformat()}"
        partition.mkdir(parents=True)
        (partition / "part.parquet").touch()

    payload = {
        "run_id": "insufficient-balanced",
        "request": {
            "factor_names": ["turnover_rate"],
            "strategy_ids": [],
            "asset_type": "stock",
            "budget_profile": "balanced",
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
        },
    }

    with pytest.raises(
        ValueError,
        match=(
            r"balanced mining requires at least 786 enriched trading bars for "
            r"3 outer folds; effective range .* has 690"
        ),
    ):
        _decode_runtime_request(payload, tmp_path, SimpleNamespace())


def test_single_forward_label_uses_global_trading_axis_without_jump() -> None:
    first = date(2024, 1, 2)
    missing = first + timedelta(days=1)
    resumed = first + timedelta(days=2)
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [first, resumed],
        "close": [10.0, 12.0],
        "turnover_rate": [1.0, 2.0],
        "unused_factor": [9.0, 10.0],
    })

    result = attach_single_forward_return(
        panel,
        start=first,
        end=resumed,
        horizon=1,
        trading_dates=[first, missing, resumed],
        factor_names=["turnover_rate"],
    )

    first_row = result.filter(pl.col("date") == first).row(0, named=True)
    assert first_row["_target_date"] == missing
    assert first_row["_next_return"] is None
    assert "_forward_return_1d" not in result.columns
    assert "close" not in result.columns
    assert "unused_factor" not in result.columns
    assert result.columns == [
        "symbol",
        "date",
        "turnover_rate",
        "_next_return",
        "_target_date",
    ]
    assert result.schema["_next_return"] == pl.Float32
    assert result.schema["turnover_rate"] == pl.Float32

    fast = attach_single_forward_return(
        panel.sort(["date", "symbol"]),
        start=first,
        end=resumed,
        horizon=1,
        trading_dates=[first, missing, resumed],
        factor_names=["turnover_rate"],
        assume_unique_symbol_date=True,
    )
    assert fast.equals(result)


def test_compact_factor_panel_matches_full_symbol_independent_calculation(
    monkeypatch,
) -> None:
    first = date(2024, 1, 2)
    rows = []
    for symbol, offset in (("a", 0.0), ("b", 2.0), ("c", 4.0)):
        for day in range(70):
            close = 10.0 + offset + day * 0.1
            rows.append({
                "symbol": symbol,
                "date": first + timedelta(days=day),
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000.0 + day,
                "amount": close * (1000.0 + day),
                "turnover_rate": 1.0 + day / 100.0,
            })
    raw = pl.DataFrame(rows).sort(["symbol", "date"])

    class Engine:
        def load_panel(self, *_args, **_kwargs):
            return raw

    engine = Engine()
    from app.backtest.factor import FactorBacktestService

    factor_service = FactorBacktestService(engine)
    config = SimpleNamespace(
        symbols=None,
        start=first,
        end=first + timedelta(days=69),
        asset_type="stock",
    )
    names = ("momentum_20d", "rsi_14", "ma20_bias")
    full = factor_service._compute_missing_factors(
        raw,
        set(names),
        assume_sorted=True,
    ).select(["symbol", "date", "close", *names]).with_columns([
        pl.col(name).cast(pl.Float32) for name in names
    ]).sort(["date", "symbol"])

    monkeypatch.setattr("app.backtest.mining_runtime._SYMBOL_BATCH_SIZE", 1)
    compact = _load_compact_factor_panel(
        factor_service,
        config,
        names,
        expected_generation="generation",
        cancel_check=None,
    )

    assert compact.equals(full)


def test_compact_factor_panel_rejects_noncanonical_symbol_date_keys() -> None:
    first = date(2024, 1, 2)
    canonical = pl.DataFrame({
        "symbol": ["a", "a", "b"],
        "date": [first, first + timedelta(days=1), first],
        "open": [1.0, 1.0, 1.0],
        "high": [1.0, 1.0, 1.0],
        "low": [1.0, 1.0, 1.0],
        "close": [1.0, 1.0, 1.0],
        "volume": [1.0, 1.0, 1.0],
        "amount": [1.0, 1.0, 1.0],
        "turnover_rate": [1.0, 1.0, 1.0],
    })
    config = SimpleNamespace(
        symbols=None,
        start=first,
        end=first + timedelta(days=1),
        asset_type="stock",
    )

    class Engine:
        def __init__(self, panel):
            self.panel = panel

        def load_panel(self, *_args, **_kwargs):
            return self.panel

    from app.backtest.factor import FactorBacktestService

    for invalid in (
        canonical.with_columns(pl.Series(
            "date",
            [first + timedelta(days=1), first, first],
        )),
        pl.concat([canonical.slice(0, 1), canonical]),
    ):
        with pytest.raises(ValueError, match="unique symbol/date"):
            _load_compact_factor_panel(
                FactorBacktestService(Engine(invalid)),
                config,
                ("turnover_rate",),
                expected_generation="generation",
                cancel_check=None,
            )


def test_artifact_finalists_are_truncated_by_oos_sharpe_before_signature() -> None:
    low = MiningCandidate(candidate_id="a-low", kind="existing_strategy", strategy_id="low")
    high = MiningCandidate(candidate_id="z-high", kind="existing_strategy", strategy_id="high")
    rows = [
        {"candidate_signature": "a-low", "sharpe": 0.2, "skipped": False},
        {"candidate_signature": "z-high", "sharpe": 1.4, "skipped": False},
    ]

    assert _rank_artifact_candidates([low, high], rows, limit=1) == [high]


def test_prepare_base_market_forwards_cancel_event(monkeypatch, tmp_path) -> None:
    cancel_event = object()
    captured = {}
    plan = SimpleNamespace(
        base_columns=frozenset(),
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset(),
        matrix_columns=frozenset(),
        instrument_columns=frozenset(),
        warmup_bars=1,
        full_feature_fallback=False,
        execution_backend="matrix_native",
        fundamental_columns=frozenset(),
    )
    research = SimpleNamespace(entry_signals=[], exit_signals=[])
    strategy_engine = SimpleNamespace(get=lambda _strategy_id: research)
    service = SimpleNamespace(
        _effective_basic_filter=lambda *_args: {},
        engine=SimpleNamespace(),
    )
    request = SimpleNamespace(
        factor_names=("turnover_rate",),
        strategy_ids=(),
        asset_type="stock",
        forward_horizon=1,
        start=date(2024, 1, 2),
        end=date(2024, 1, 3),
        symbols=None,
    )

    monkeypatch.setattr(
        "app.backtest.mining_runtime.StrategyDependencyResolver.resolve",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        "app.backtest.mining_runtime.build_matrix_cache_profile",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    def load_matrix(*_args, **kwargs):
        captured.update(kwargs)
        return "market"

    service.engine.load_market_data_matrix_for_backtest = load_matrix

    result = _prepare_base_market(
        service,
        strategy_engine,
        tmp_path,
        request,
        expected_generation="generation",
        cancel_check=cancel_event,
    )

    assert result == "market"
    assert captured["cancel_event"] is cancel_event


def test_training_metric_provider_uses_only_supplied_fold() -> None:
    start = date(2024, 1, 2)
    rows = []
    for day_offset in range(3):
        for asset_id in range(4):
            rows.append({
                "symbol": f"{asset_id:06d}.SZ",
                "date": start + timedelta(days=day_offset),
                "factor": float(asset_id),
                "_next_return": (
                    float(asset_id) if day_offset < 2 else float(-asset_id)
                ),
            })
    panel = pl.DataFrame(rows)
    train = panel.filter(pl.col("date") < start + timedelta(days=2))

    provider = TrainingMetricProvider("_next_return")
    metric = provider(train, ["factor"])[0]

    assert metric.rank_ic == pytest.approx(1.0)
    assert metric.coverage == pytest.approx(1.0)
    assert provider.calls[0]["end"] == (start + timedelta(days=1)).isoformat()
    assert provider.calls[0]["rows"] == 8


def test_regime_date_count_uses_t_minus_one_market_labels(tmp_path) -> None:
    labels = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(4)]
    panel = pl.DataFrame({"date": labels})
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": labels[:3],
        "state": ["weak", "strong", "lean_strong"],
        "score": [20, 80, 70],
    }))
    fold = SimpleNamespace(
        test_start=labels[1].isoformat(),
        test_end=labels[3].isoformat(),
    )

    assert _regime_date_count(panel, fold, "strong", tmp_path) == 2
    assert _regime_date_count(panel, fold, "weak", tmp_path) == 1


def _small_validation() -> NestedValidationConfig:
    return NestedValidationConfig(
        outer_train_bars=10,
        outer_test_bars=3,
        outer_step_bars=5,
        inner_train_bars=5,
        inner_test_bars=2,
        inner_step_bars=3,
        purge_bars=1,
        embargo_bars=1,
        min_train_bars=3,
    )


def _regime_panel(n: int = 20) -> tuple[list[date], pl.DataFrame]:
    labels = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(n)]
    return labels, pl.DataFrame({"date": labels})


def _upsert_regime(tmp_path, labels, states: list[str]) -> None:
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": labels,
        "state": states,
        "score": [50] * len(labels),
    }))


def test_validate_regime_availability_fails_fast_when_regime_data_missing(
    tmp_path,
) -> None:
    labels, panel = _regime_panel()
    request = SimpleNamespace(
        mining_request=SimpleNamespace(validation=_small_validation()),
    )

    with pytest.raises(ValueError, match="市场环境数据为空"):
        _validate_regime_availability(panel, request, tmp_path)


def test_validate_regime_availability_passes_when_regime_covers_fold_windows(
    tmp_path,
) -> None:
    labels, panel = _regime_panel()
    _upsert_regime(
        tmp_path,
        labels[:-1],
        ["range"] * (len(labels) - 1),
    )
    request = SimpleNamespace(
        mining_request=SimpleNamespace(validation=_small_validation()),
    )

    _validate_regime_availability(panel, request, tmp_path)


def test_validate_regime_availability_reports_coverage_gaps_in_fold_windows(
    tmp_path,
) -> None:
    labels, panel = _regime_panel()
    nested = generate_nested_folds(
        [value.isoformat() for value in labels], _small_validation()
    )
    gap_date = date.fromisoformat(nested[0].outer.test_start) + timedelta(days=1)
    covered = [value for value in labels if value != gap_date]
    _upsert_regime(tmp_path, covered, ["range"] * len(covered))
    request = SimpleNamespace(
        mining_request=SimpleNamespace(validation=_small_validation()),
    )

    with pytest.raises(ValueError, match="市场环境数据覆盖不完整"):
        _validate_regime_availability(panel, request, tmp_path)
