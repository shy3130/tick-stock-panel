from __future__ import annotations

import json

import numpy as np

from research.alphagpt.pool import FactorPool, formula_hash


def _add(pool: FactorPool, candidate_id: str, formula: str, signal: np.ndarray):
    return pool.add_candidate(
        candidate_id=candidate_id,
        formula=formula,
        parent_formulas=[],
        generation_method="random",
        complexity=1,
        fold_metrics=[{"fold_id": "T1", "icir": 1.0}],
        reward={"total": 1.0},
        signal=signal,
    )


def test_normalized_hash_deduplicates_and_records_failure() -> None:
    pool = FactorPool()
    first = _add(pool, "c1", "MOM20", np.arange(8, dtype=float))
    duplicate = _add(pool, "c2", "  MOM20   ", np.arange(8, dtype=float))

    assert first.accepted
    assert not duplicate.accepted
    assert duplicate.reason == "duplicate_formula"
    assert len(pool.candidates) == 1
    assert pool.failures[-1].reason == "duplicate_formula"
    assert formula_hash(" MOM20 ") == formula_hash(["MOM20"])


def test_highly_correlated_factor_is_rejected_and_audited() -> None:
    pool = FactorPool(correlation_threshold=0.90)
    assert _add(pool, "c1", "MOM20", np.arange(20, dtype=float)).accepted
    result = _add(
        pool,
        "c2",
        "MA20_DEV",
        np.arange(20, dtype=float) * 2.0 + 1.0,
    )

    assert not result.accepted
    assert result.reason == "high_correlation"
    assert result.candidate is not None
    assert result.candidate.status == "rejected"
    assert result.candidate.max_abs_correlation > 0.99
    assert pool.failures[-1].details["correlated_with"] == formula_hash("MOM20")


def test_pool_json_round_trip_preserves_candidates_and_failures(tmp_path) -> None:
    pool = FactorPool(correlation_threshold=0.95)
    _add(pool, "c1", "MOM20", np.linspace(-1, 1, 12))
    _add(pool, "c2", "MOM20", np.linspace(-1, 1, 12))

    restored = FactorPool.from_dict(pool.to_dict(include_signals=True))
    assert restored.to_dict(include_signals=True) == pool.to_dict(include_signals=True)

    output = tmp_path / "pool.json"
    pool.write_json(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["n_accepted"] == 1
    assert payload["failures"][0]["reason"] == "duplicate_formula"
