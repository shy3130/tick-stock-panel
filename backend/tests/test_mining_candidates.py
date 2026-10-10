from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.candidates import CandidateStore, CandidateValidationError
from app.backtest.mining import compute_candidate_signature
from app.services.mining_candidates import (
    MiningCandidateService,
    _published_strategy_id,
)
from app.services.mining_jobs import MiningRunStore
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine


class _StrategyEngine:
    def __init__(self) -> None:
        self.strategies = {
            "existing_daily": SimpleNamespace(
                id="existing_daily",
                execution_backend="matrix_native",
                meta={
                    "id": "existing_daily",
                    "research_only": False,
                    "timeframes": ["1d"],
                    "asset_types": ["stock"],
                },
            )
        }

    def get(self, strategy_id: str):
        if strategy_id not in self.strategies:
            raise KeyError(strategy_id)
        return self.strategies[strategy_id]

    def list_strategies(self):
        return [strategy.meta for strategy in self.strategies.values()]


def _factor_definition() -> dict:
    return {
        "kind": "factor_rank",
        "factor_names": ["turnover_rate", "rsi_14"],
        "scoring": {"turnover_rate": 1.0, "rsi_14": 2.0},
        "directions": {"turnover_rate": "high", "rsi_14": "low"},
    }


def _create_run(
    tmp_path,
    *,
    definition: dict | None = None,
    status: str = "succeeded",
    run_id: str = "mining-run",
) -> tuple[MiningRunStore, str, str]:
    definition = definition or _factor_definition()
    signature = compute_candidate_signature(definition)
    store = MiningRunStore(tmp_path)
    store.create(
        {
            "factor_names": ["turnover_rate", "rsi_14"],
            "strategy_ids": ["existing_daily", "ma_golden_cross"],
            "asset_type": "stock",
            "start": "2025-01-01",
            "end": "2026-01-09",
            "budget_profile": "exploratory",
            "commission_pct": 0.0002,
            "stamp_tax_pct": 0.0005,
            "slippage_bps": 5.0,
        },
        {"generation": "test"},
        run_id=run_id,
    )
    frame = pl.DataFrame({
        "signature": [signature],
        "name": ["因子组合候选"],
        "kind": [
            "existing_strategy"
            if definition["kind"] == "existing_strategy"
            else "factor_combination"
        ],
        "factor_names_json": [json.dumps(definition.get("factor_names", []))],
        "strategy_id": [definition.get("strategy_id")],
        "definition_json": [json.dumps(definition, sort_keys=True)],
        "regime_state": ["overall"],
        "score": [0.9],
        "oos_return": [0.08],
        "oos_sharpe": [0.9],
        "oos_max_drawdown": [-0.12],
        "oos_positive_fold_ratio": [1.0],
        "oos_n_trades": [70],
        "confidence": ["standard"],
        "valid_folds": [3],
        "skipped_folds": [0],
        "promoted_candidate_id": [None],
        "published_strategy_id": [None],
    }, schema_overrides={
        "strategy_id": pl.String,
        "promoted_candidate_id": pl.String,
        "published_strategy_id": pl.String,
    })
    frame.write_parquet(store.artifact_path(run_id, "candidates"))
    store.register_artifact(run_id, "candidates")
    store.write_summary(run_id, {
        "data_as_of": "2026-01-09",
        "algorithm_version": "mining-v1",
        "methodology_version": "factor_v2",
    })
    if status != "queued":
        store.transition_status(run_id, "running")
        store.transition_status(run_id, status)
    return store, run_id, signature


def _service(tmp_path, store: MiningRunStore) -> MiningCandidateService:
    return MiningCandidateService(
        tmp_path,
        store,
        CandidateStore(tmp_path),
        _StrategyEngine(),
        strategy_cache_invalidator=lambda _data_dir: None,
    )


def _real_service(
    tmp_path,
    store: MiningRunStore,
    *,
    cache_invalidator=None,
    monitor_invalidator=None,
) -> tuple[MiningCandidateService, StrategyEngine]:
    builtin_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "strategies"
    custom_dir = tmp_path / "strategies" / "custom"
    engine = StrategyEngine(strategy_dirs=[builtin_dir, custom_dir])
    service = MiningCandidateService(
        tmp_path,
        store,
        CandidateStore(tmp_path),
        engine,
        strategy_cache_invalidator=cache_invalidator or (lambda _data_dir: None),
        monitor_state_invalidator=monitor_invalidator,
    )
    return service, engine


def test_promote_rereads_artifact_and_repairs_backlink_idempotently(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service = _service(tmp_path, store)

    first = service.promote(run_id, signature)
    second = service.promote(run_id, signature)

    assert second == first
    assert first["kind"] == "strategy"
    assert first["source_id"].startswith("mined_factor_")
    assert first["status"] == "pending"
    assert first["config"]["origin_run_id"] == run_id
    assert first["config"]["candidate_signature"] == signature
    assert first["config"]["factor_names"] == ["turnover_rate", "rsi_14"]
    assert first["config"]["directions"] == ["high", "low"]
    assert first["config"]["weights"] == [1.0, 2.0]
    assert first["metrics"]["oos_sharpe"] == pytest.approx(0.9)
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["promoted_candidate_id"] == first["id"]
    assert len(CandidateStore(tmp_path).list()) == 1


def test_promote_repairs_backlink_after_partial_failure(tmp_path, monkeypatch) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service = _service(tmp_path, store)
    original = service._write_backlink
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected backlink failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_write_backlink", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        service.promote(run_id, signature)

    created = CandidateStore(tmp_path).list()
    assert len(created) == 1
    recovered = service.promote(run_id, signature)
    assert recovered["id"] == created[0]["id"]
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["promoted_candidate_id"] == created[0]["id"]
    assert len(CandidateStore(tmp_path).list()) == 1


def test_promote_rejects_non_successful_run(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path, status="queued")

    with pytest.raises(ValueError, match="successful"):
        _service(tmp_path, store).promote(run_id, signature)

    assert CandidateStore(tmp_path).list() == []


def test_promote_rejects_tampered_definition_before_candidate_write(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    frame = pl.read_parquet(path).with_columns(
        pl.lit(json.dumps({
            "kind": "factor_rank",
            "factor_names": ["turnover_rate"],
            "scoring": {"turnover_rate": 1.0},
            "directions": {"turnover_rate": "high"},
        })).alias("definition_json")
    )
    frame.write_parquet(path)

    with pytest.raises(ValueError, match=r"signature|definition"):
        _service(tmp_path, store).promote(run_id, signature)

    assert CandidateStore(tmp_path).list() == []


def test_promote_rejects_factor_not_selected_in_origin_request(tmp_path) -> None:
    definition = {
        "kind": "factor_rank",
        "factor_names": ["momentum_20d"],
        "scoring": {"momentum_20d": 1.0},
        "directions": {"momentum_20d": "high"},
    }
    store, run_id, signature = _create_run(tmp_path, definition=definition)

    with pytest.raises(ValueError, match="origin request"):
        _service(tmp_path, store).promote(run_id, signature)


def test_promote_existing_strategy_revalidates_current_engine_contract(tmp_path) -> None:
    definition = {"kind": "existing_strategy", "strategy_id": "existing_daily"}
    store, run_id, signature = _create_run(tmp_path, definition=definition)

    promoted = _service(tmp_path, store).promote(run_id, signature)

    assert promoted["source_id"] == "existing_daily"
    assert promoted["config"]["strategy_id"] == "existing_daily"


def test_publish_existing_strategy_returns_verified_id_and_repairs_backlink(tmp_path) -> None:
    definition = {"kind": "existing_strategy", "strategy_id": "existing_daily"}
    store, run_id, signature = _create_run(tmp_path, definition=definition)
    service = _service(tmp_path, store)

    first = service.publish(run_id, signature)
    second = service.publish(run_id, signature)
    with pytest.raises(TypeError):
        service.publish(run_id, signature, "existing_daily")

    assert first == second == {"ok": True, "strategy_id": "existing_daily"}
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["published_strategy_id"] == "existing_daily"
    assert CandidateStore(tmp_path).list() == []


def test_promote_requires_unique_artifact_signature(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    frame = pl.read_parquet(path)
    pl.concat([frame, frame]).write_parquet(path)

    with pytest.raises(ValueError, match="duplicate"):
        _service(tmp_path, store).promote(run_id, signature)


def test_promote_concurrent_calls_create_one_authoritative_candidate(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service = _service(tmp_path, store)

    with ThreadPoolExecutor(max_workers=8) as executor:
        items = list(executor.map(
            lambda _index: service.promote(run_id, signature),
            range(16),
        ))

    assert len({item["id"] for item in items}) == 1
    assert len(CandidateStore(tmp_path).list()) == 1


def test_promote_rejects_artifact_conflicting_with_existing_store_record(
    tmp_path,
) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service = _service(tmp_path, store)
    original = service.promote(run_id, signature)
    path = store.artifact_path(run_id, "candidates")
    pl.read_parquet(path).with_columns(
        pl.lit(1.1).alias("oos_sharpe"),
        pl.lit(None, dtype=pl.String).alias("promoted_candidate_id"),
    ).write_parquet(path)

    with pytest.raises(CandidateValidationError, match="冲突"):
        service.promote(run_id, signature)

    persisted = pl.read_parquet(path).row(0, named=True)
    assert persisted["promoted_candidate_id"] is None
    stored = CandidateStore(tmp_path).list()[0]
    assert stored == original
    assert stored["metrics"]["oos_sharpe"] == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        (
            {
                "kind": "factor_rank",
                "factor_names": ["turnover_rate"],
                "scoring": {"turnover_rate": 0.0},
                "directions": {"turnover_rate": "high"},
            },
            "positive",
        ),
        (
            {
                "kind": "factor_rank",
                "factor_names": ["turnover_rate"],
                "scoring": {"turnover_rate": float("inf")},
                "directions": {"turnover_rate": "high"},
            },
            "finite",
        ),
        (
            {
                "kind": "factor_rank",
                "factor_names": ["turnover_rate"],
                "scoring": {"turnover_rate": 1.0},
                "directions": {"turnover_rate": "sideways"},
            },
            "directions",
        ),
        (
            {
                "kind": "factor_rank",
                "factor_names": ["not_a_factor"],
                "scoring": {"not_a_factor": 1.0},
                "directions": {"not_a_factor": "high"},
            },
            "unknown factors",
        ),
        (
            {
                "kind": "factor_rank",
                "factor_names": [
                    "momentum_5d",
                    "momentum_10d",
                    "momentum_20d",
                    "momentum_30d",
                    "momentum_60d",
                ],
                "scoring": {
                    "momentum_5d": 1.0,
                    "momentum_10d": 1.0,
                    "momentum_20d": 1.0,
                    "momentum_30d": 1.0,
                    "momentum_60d": 1.0,
                },
                "directions": {
                    "momentum_5d": "high",
                    "momentum_10d": "high",
                    "momentum_20d": "high",
                    "momentum_30d": "high",
                    "momentum_60d": "high",
                },
            },
            "1 to 4",
        ),
    ],
)
def test_promote_rejects_invalid_factor_contract(tmp_path, definition, message) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    frame = pl.read_parquet(path).with_columns(
        pl.lit(signature).alias("signature"),
        pl.lit(json.dumps(definition)).alias("definition_json"),
        pl.lit(json.dumps(definition["factor_names"])).alias("factor_names_json"),
    )
    frame.write_parquet(path)

    with pytest.raises(ValueError, match=message):
        _service(tmp_path, store).promote(run_id, signature)


@pytest.mark.parametrize("column", ["oos_sharpe", "oos_return", "score"])
def test_promote_rejects_nonfinite_artifact_values(tmp_path, column) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    pl.read_parquet(path).with_columns(pl.lit(float("nan")).alias(column)).write_parquet(path)

    with pytest.raises(ValueError, match="finite"):
        _service(tmp_path, store).promote(run_id, signature)


def test_promote_rejects_corrupt_or_missing_schema_artifact(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    path.write_bytes(b"not parquet")
    with pytest.raises(RuntimeError, match="failed to read"):
        _service(tmp_path, store).promote(run_id, signature)

    path.unlink()
    pl.DataFrame({"signature": [signature]}).write_parquet(path)
    with pytest.raises(ValueError, match="schema"):
        _service(tmp_path, store).promote(run_id, signature)


def test_publish_factor_discovers_public_strategy_and_repairs_runtime_state(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    invalidations: list[object] = []
    service, engine = _real_service(
        tmp_path,
        store,
        cache_invalidator=lambda data_dir: invalidations.append(data_dir),
        monitor_invalidator=lambda: invalidations.append("monitor"),
    )
    strategy_config.save_override(
        tmp_path,
        "factor_rank_research",
        {"params": {"entry_score": 99.0}},
    )

    result = service.publish(run_id, signature)
    repeated = service.publish(run_id, signature)

    assert repeated == result
    strategy = engine.get(result["strategy_id"])
    assert strategy.source == "custom"
    assert strategy.execution_backend == "matrix_native"
    assert strategy.meta["origin_run_id"] == run_id
    assert strategy.meta["candidate_signature"] == signature
    assert strategy.meta["research_only"] is False
    assert strategy.meta["asset_types"] == ["stock"]
    assert strategy.matrix_strategy._scoring == {"turnover_rate": 1.0, "rsi_14": 2.0}
    assert strategy.matrix_strategy._directions == {
        "turnover_rate": "high",
        "rsi_14": "low",
    }
    assert result["strategy_id"] in {
        meta["id"] for meta in engine.list_strategies()
    }
    assert strategy_config.load_override(
        tmp_path, "factor_rank_research"
    ) == {"params": {"entry_score": 99.0}}
    assert not (tmp_path / "user_data" / "strategy_overrides" / f"{result['strategy_id']}.json").exists()
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["published_strategy_id"] == result["strategy_id"]
    assert invalidations == [tmp_path, "monitor", tmp_path, "monitor"]


def test_publish_factor_repairs_backlink_after_source_was_verified(
    tmp_path,
    monkeypatch,
) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service, _engine = _real_service(tmp_path, store)
    original = service._write_backlink
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected publication backlink failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_write_backlink", fail_once)
    with pytest.raises(RuntimeError, match="injected publication"):
        service.publish(run_id, signature)

    result = service.publish(run_id, signature)
    strategy_path = tmp_path / "strategies" / "custom" / f"{result['strategy_id']}.py"
    assert strategy_path.is_file()
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["published_strategy_id"] == result["strategy_id"]


def test_publish_factor_uses_server_run_scoped_id_and_refuses_collision(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    service, _engine = _real_service(tmp_path, store)
    expected_id = _published_strategy_id(run_id, signature)

    with pytest.raises(TypeError):
        service.publish(run_id, signature, "mined_factor_caller_selected")

    target = tmp_path / "strategies" / "custom" / f"{expected_id}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("user owned", encoding="utf-8")
    with pytest.raises(ValueError, match="collision"):
        service.publish(run_id, signature)
    assert target.read_text(encoding="utf-8") == "user owned"


def test_publish_factor_same_signature_in_different_runs_has_independent_ids(
    tmp_path,
) -> None:
    store, first_run_id, signature = _create_run(tmp_path, run_id="mining-run-one")
    _other_store, second_run_id, second_signature = _create_run(
        tmp_path,
        run_id="mining-run-two",
    )
    service, engine = _real_service(tmp_path, store)

    first = service.publish(first_run_id, signature)
    second = service.publish(second_run_id, second_signature)

    assert signature == second_signature
    assert first["strategy_id"] == _published_strategy_id(first_run_id, signature)
    assert second["strategy_id"] == _published_strategy_id(
        second_run_id,
        second_signature,
    )
    assert first["strategy_id"] != second["strategy_id"]
    assert engine.has(first["strategy_id"])
    assert engine.has(second["strategy_id"])


def test_publish_factor_rejects_inconsistent_backlink(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    path = store.artifact_path(run_id, "candidates")
    pl.read_parquet(path).with_columns(
        pl.lit("mined_factor_other").alias("published_strategy_id")
    ).write_parquet(path)
    service, engine = _real_service(tmp_path, store)

    with pytest.raises(ValueError, match="backlink is inconsistent"):
        service.publish(run_id, signature)

    expected_id = _published_strategy_id(run_id, signature)
    assert not engine.has(expected_id)
    assert not (tmp_path / "strategies" / "custom" / f"{expected_id}.py").exists()


def test_publish_factor_rolls_back_file_and_skips_invalidations_on_reload_failure(
    tmp_path,
    monkeypatch,
) -> None:
    store, run_id, signature = _create_run(tmp_path)
    invalidations: list[str] = []
    service, engine = _real_service(
        tmp_path,
        store,
        cache_invalidator=lambda _data_dir: invalidations.append("cache"),
        monitor_invalidator=lambda: invalidations.append("monitor"),
    )
    real_reload = engine.reload
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("injected reload failure")
        real_reload()

    monkeypatch.setattr(engine, "reload", fail_once)
    expected_id = _published_strategy_id(run_id, signature)
    with pytest.raises(RuntimeError, match="injected reload failure"):
        service.publish(run_id, signature)

    assert not (tmp_path / "strategies" / "custom" / f"{expected_id}.py").exists()
    assert not engine.has(expected_id)
    assert invalidations == []
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["published_strategy_id"] is None


def test_publish_factor_rolls_back_on_runtime_invalidation_failure(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    invalidations: list[str] = []

    def fail_monitor() -> None:
        invalidations.append("monitor")
        raise ValueError("injected monitor invalidation failure")

    service, engine = _real_service(
        tmp_path,
        store,
        cache_invalidator=lambda _data_dir: invalidations.append("cache"),
        monitor_invalidator=fail_monitor,
    )
    expected_id = _published_strategy_id(run_id, signature)

    with pytest.raises(RuntimeError, match="injected monitor invalidation failure"):
        service.publish(run_id, signature)

    assert invalidations == ["cache", "monitor"]
    assert not (tmp_path / "strategies" / "custom" / f"{expected_id}.py").exists()
    assert not engine.has(expected_id)
    persisted = pl.read_parquet(store.artifact_path(run_id, "candidates")).row(0, named=True)
    assert persisted["published_strategy_id"] is None


def test_publish_existing_does_not_reload_or_invalidate(tmp_path) -> None:
    definition = {"kind": "existing_strategy", "strategy_id": "ma_golden_cross"}
    store, run_id, signature = _create_run(tmp_path, definition=definition)
    invalidations: list[str] = []
    service, engine = _real_service(
        tmp_path,
        store,
        cache_invalidator=lambda _data_dir: invalidations.append("cache"),
        monitor_invalidator=lambda: invalidations.append("monitor"),
    )
    original = engine.get("ma_golden_cross")

    result = service.publish(run_id, signature)

    assert result == {"ok": True, "strategy_id": "ma_golden_cross"}
    assert engine.get("ma_golden_cross") is original
    assert invalidations == []

def _rewrite_candidate_metric(
    store: MiningRunStore,
    run_id: str,
    column: str,
    value,
) -> None:
    path = store.artifact_path(run_id, "candidates")
    frame = pl.read_parquet(path)
    frame.with_columns(
        pl.lit(value, dtype=frame.schema[column]).alias(column),
    ).write_parquet(path)


def test_publish_rejects_low_confidence_exploratory_candidate(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    _rewrite_candidate_metric(store, run_id, "confidence", "low")
    service = _service(tmp_path, store)

    with pytest.raises(ValueError, match="exploratory results can only be saved"):
        service.publish(run_id, signature)


def test_publish_rejects_candidate_below_evidence_gate(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    _rewrite_candidate_metric(store, run_id, "oos_sharpe", 0.2)
    service = _service(tmp_path, store)

    with pytest.raises(ValueError, match=r"OOS Sharpe of at least 0.5"):
        service.publish(run_id, signature)


def test_publish_rejects_candidate_with_insufficient_folds(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    _rewrite_candidate_metric(store, run_id, "valid_folds", 1)
    service = _service(tmp_path, store)

    with pytest.raises(ValueError, match="at least 2 valid outer folds"):
        service.publish(run_id, signature)


def test_publish_rejects_candidate_missing_required_metrics(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    _rewrite_candidate_metric(store, run_id, "oos_n_trades", None)
    service = _service(tmp_path, store)

    with pytest.raises(ValueError, match="does not meet the promotion gate"):
        service.publish(run_id, signature)


def test_promote_still_allowed_for_gated_candidates(tmp_path) -> None:
    store, run_id, signature = _create_run(tmp_path)
    _rewrite_candidate_metric(store, run_id, "confidence", "low")
    _rewrite_candidate_metric(store, run_id, "oos_sharpe", 0.1)
    service = _service(tmp_path, store)

    result = service.promote(run_id, signature)

    assert result["status"] == "pending"
