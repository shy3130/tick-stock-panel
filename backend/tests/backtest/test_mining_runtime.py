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


# ── P2a 稳健选冠 (mining-v3) ──────────────────────────────────────


def _candidate(cid: str, factors: tuple[str, ...] = ("f1", "f2")) -> MiningCandidate:
    return MiningCandidate(
        candidate_id=cid,
        kind="factor_rank",
        factor_names=factors,
        weights=(1.0,) * len(factors),
        directions=(1,) * len(factors),
    )


def test_rank_prefers_robust_over_mean_champion() -> None:
    """均值口径 A(1.67) > B(1.5); 稳健口径 A(1.0) < B(1.5) — v3 必须选 B。"""
    rows = [
        {"candidate_signature": "a", "sharpe": 5.0, "skipped": False},
        {"candidate_signature": "a", "sharpe": 5.0, "skipped": False},
        {"candidate_signature": "a", "sharpe": -5.0, "skipped": False},
        {"candidate_signature": "b", "sharpe": 1.5, "skipped": False},
        {"candidate_signature": "b", "sharpe": 1.5, "skipped": False},
        {"candidate_signature": "b", "sharpe": 1.5, "skipped": False},
    ]
    ranked = _rank_artifact_candidates(
        [_candidate("a"), _candidate("b")], rows, limit=2
    )
    assert [item.candidate_id for item in ranked] == ["b", "a"]


def test_rank_skips_failed_folds_like_before() -> None:
    rows = [
        {"candidate_signature": "a", "sharpe": 2.0, "skipped": True},
        {"candidate_signature": "a", "sharpe": 1.0, "skipped": False},
        {"candidate_signature": "b", "sharpe": 0.5, "skipped": False},
    ]
    ranked = _rank_artifact_candidates(
        [_candidate("a"), _candidate("b")], rows, limit=2
    )
    assert ranked[0].candidate_id == "a"


# ── P2b 冠军权重精调 ──────────────────────────────────────────────


class _RefineEvaluator:
    """按 scoring 比率查表返回分数; 传入面板行数 == 全面板行数视为 stage2 全窗。"""

    FULL_ROWS = 8

    def __init__(self, fold_scores, full_scores) -> None:
        self.fold_scores = fold_scores  # {ratios: [每折 sharpe]}
        self.full_scores = full_scores  # {ratios: sharpe}
        self.fold_cursor: dict[tuple[float, ...], int] = {}
        self.backtest_count = 0
        self.data_dir = None

    def evaluate_test(self, test, definition, *, regime_state="overall"):
        from app.backtest.mining import CandidateEvaluation

        del regime_state
        self.backtest_count += 1
        names = list(definition["factor_names"])
        base = definition["scoring"][names[0]]
        ratios = tuple(round(definition["scoring"][n] / base, 6) for n in names)
        if test.height >= self.FULL_ROWS:
            sharpe = self.full_scores.get(ratios)
        else:
            scores = self.fold_scores.get(ratios) or []
            index = self.fold_cursor.get(ratios, 0)
            self.fold_cursor[ratios] = index + 1
            sharpe = scores[index] if index < len(scores) else None
        if sharpe is None:
            return CandidateEvaluation(score=None, error="no scores for variant")
        return CandidateEvaluation(
            score=sharpe,
            metrics={
                "sharpe": sharpe,
                "total_return": 0.1 * sharpe,
                "max_drawdown": -0.05,
                "n_trades": 50,
            },
        )


class _Fold:
    def __init__(self, labels) -> None:
        self.outer = SimpleNamespace(test_labels=labels)


def _refine_request(profile="balanced"):
    from app.backtest.mining import MiningBudget, MiningRequest

    budget = (
        MiningBudget.balanced() if profile == "balanced" else MiningBudget.strict()
    )
    mining = MiningRequest(factor_names=("f1", "f2"), budget=budget, profile=profile)
    return SimpleNamespace(
        profile=profile,
        mining_request=mining,
        run_id="run",
        max_finalists=8,
        end=date(2026, 10, 23),
    )


def _run_refine(champion, evaluator, folds=3, trials_used=10, profile="balanced"):
    from app.backtest.mining_runtime import _refine_champion_weights

    dates = [f"2026-01-0{i}" for i in range(1, 9)]
    panel = pl.DataFrame({"date": dates})
    nested = tuple(
        _Fold([dates[i], dates[i + 1]]) for i in range(0, folds * 2, 2)
    )  # 每折两个标签, 与全窗 (8 行) 可区分
    result = SimpleNamespace(trials_used=trials_used)
    return _refine_champion_weights(
        panel, _refine_request(profile), result, [champion], evaluator, nested, None
    )


def _two_factor_champion(weights=(1.0, 1.0)):
    scoring = {"f1": weights[0], "f2": weights[1]}

    class _C:
        candidate_id = "sig-original"
        kind = "factor_rank"
        factor_names = ("f1", "f2")

        @staticmethod
        def definition():
            return {
                "kind": "factor_rank",
                "factor_names": ["f1", "f2"],
                "scoring": dict(scoring),
                "directions": {"f1": "high", "f2": "high"},
            }

    instance = _C()
    instance.weights = weights
    instance.directions = (1, 1)
    return instance


def test_refine_produces_refined_row_when_variant_wins():
    evaluator = _RefineEvaluator(
        fold_scores={
            (1.0, 1.0): [1.0, 1.0, 1.0],
            (1.0, 2.0): [2.0, 2.0, 2.0],
            (1.0, 0.5): [0.5, 0.5, 0.5],
            (1.0, 1.5): [1.5, 1.5, 1.5],
        },
        full_scores={
            (1.0, 1.0): 1.0,
            (1.0, 2.0): 1.8,
            (1.0, 0.5): 0.4,
            (1.0, 1.5): 1.4,
        },
    )
    rows, summary = _run_refine(_two_factor_champion(), evaluator)
    assert summary is not None and summary["applied"] is True
    assert summary["refined_weights"] == (1.0, 2.0)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"].endswith("· 权重精调")
    assert row["score"] == 2.0            # 稳健分 = 各折一致 2.0
    assert row["oos_worst_sharpe"] == 2.0
    assert row["valid_folds"] == 3
    import json as _json

    definition = _json.loads(row["definition_json"])
    assert definition["scoring"] == {"f1": 1.0, "f2": 2.0}
    # 调用预算: 4 变体 (原权重 + 3 个非重复档位) × 3 折 + 2 全窗 = 14
    assert evaluator.backtest_count == 14


def test_refine_abandoned_on_full_window_degradation():
    evaluator = _RefineEvaluator(
        fold_scores={
            (1.0, 1.0): [1.0, 1.0, 1.0],
            (1.0, 2.0): [2.0, 2.0, 2.0],
            (1.0, 0.5): [0.5, 0.5, 0.5],
            (1.0, 1.5): [1.5, 1.5, 1.5],
        },
        full_scores={(1.0, 1.0): 2.0, (1.0, 2.0): 0.5, (1.0, 0.5): 0.4, (1.0, 1.5): 1.4},
    )
    rows, summary = _run_refine(_two_factor_champion(), evaluator)
    assert rows == []
    assert summary is not None and summary["applied"] is False
    assert "退化" in summary["reason"]


def test_refine_no_improvement_returns_nothing():
    evaluator = _RefineEvaluator(
        fold_scores={
            (1.0, 1.0): [1.0, 1.0, 1.0],
            (1.0, 2.0): [0.9, 1.0, 0.8],
            (1.0, 0.5): [0.5, 0.5, 0.5],
            (1.0, 1.5): [0.8, 0.7, 0.9],
        },
        full_scores={(1.0, 1.0): 1.0},
    )
    rows, summary = _run_refine(_two_factor_champion(), evaluator)
    assert rows == [] and summary is None


def test_refine_skips_single_factor_champion():
    original = SimpleNamespace(
        candidate_id="sig-1f", kind="factor_rank",
        factor_names=("f1",), weights=(1.0,), directions=(1,),
        definition=lambda: {},
    )
    rows, summary = _run_refine(original, _RefineEvaluator({}, {}))
    assert rows == [] and summary is None


def test_refine_skips_when_budget_remaining_insufficient():
    original = SimpleNamespace(
        candidate_id="sig-4f", kind="factor_rank",
        factor_names=("f1", "f2", "f3", "f4"),
        weights=(1.0, 1.0, 1.0, 1.0), directions=(1, 1, 1, 1),
        definition=lambda: {},
    )
    # balanced 剩余额度 96-90=6 < 64 变体×3 折+2 → 跳过
    rows, summary = _run_refine(original, _RefineEvaluator({}, {}), trials_used=90)
    assert rows == [] and summary is None
