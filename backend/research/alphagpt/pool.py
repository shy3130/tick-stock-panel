"""AlphaGPT v1 候选池、去重、相关性裁剪与失败审计。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from research.common.factor_dsl import FEATURE_NAMES, OPS


def normalize_formula(formula: str | Iterable[str]) -> tuple[str, ...]:
    tokens = tuple(formula.split()) if isinstance(formula, str) else tuple(formula)
    if not tokens:
        raise ValueError("formula must not be empty")
    unknown = [token for token in tokens if token not in FEATURE_NAMES and token not in OPS]
    if unknown:
        raise ValueError(f"unknown formula tokens: {unknown}")
    return tokens


def formula_hash(formula: str | Iterable[str]) -> str:
    normalized = " ".join(normalize_formula(formula))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class FailureRecord:
    reason: str
    formula: str | None
    formula_hash: str | None
    generation_method: str
    parent_formulas: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactorCandidate:
    candidate_id: str
    formula_hash: str
    formula: str
    tokens: list[str]
    parent_formulas: list[str]
    generation_method: str
    complexity: int
    fold_metrics: list[dict[str, Any]]
    reward: dict[str, Any]
    status: str
    rejection_reason: str | None = None
    max_abs_correlation: float = 0.0
    correlated_with: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoolAddResult:
    accepted: bool
    candidate: FactorCandidate | None
    reason: str | None = None


class FactorPool:
    """保存全部唯一候选，并显式记录重复、共线和评估失败。"""

    def __init__(self, correlation_threshold: float = 0.95) -> None:
        if not 0.0 <= correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be within [0, 1]")
        self.correlation_threshold = float(correlation_threshold)
        self.candidates: dict[str, FactorCandidate] = {}
        self.failures: list[FailureRecord] = []
        self._signals: dict[str, np.ndarray] = {}

    def has_formula(self, formula: str | Iterable[str]) -> bool:
        return formula_hash(formula) in self.candidates

    @staticmethod
    def _correlation(left: np.ndarray, right: np.ndarray) -> float:
        left = np.asarray(left, dtype=float).reshape(-1)
        right = np.asarray(right, dtype=float).reshape(-1)
        if left.shape != right.shape:
            raise ValueError("factor correlation signals must have identical shapes")
        finite = np.isfinite(left) & np.isfinite(right)
        if int(finite.sum()) < 3:
            return 0.0
        x = left[finite]
        y = right[finite]
        x_std = float(np.std(x))
        y_std = float(np.std(y))
        if x_std == 0.0 or y_std == 0.0:
            return 1.0 if np.allclose(x, y) else 0.0
        value = float(np.corrcoef(x, y)[0, 1])
        return value if math.isfinite(value) else 0.0

    def max_abs_correlation(self, signal: Sequence[float] | np.ndarray | None) -> tuple[float, str | None]:
        if signal is None or not self._signals:
            return 0.0, None
        vector = np.asarray(signal, dtype=float).reshape(-1)
        best = 0.0
        matched: str | None = None
        for candidate_hash, existing in self._signals.items():
            correlation = abs(self._correlation(vector, existing))
            if correlation > best:
                best = correlation
                matched = candidate_hash
        return best, matched

    def record_failure(
        self,
        *,
        reason: str,
        formula: str | Iterable[str] | None,
        generation_method: str,
        parent_formulas: Iterable[str] = (),
        details: dict[str, Any] | None = None,
    ) -> FailureRecord:
        normalized: tuple[str, ...] | None = None
        digest: str | None = None
        formula_text: str | None = None
        if formula is not None:
            try:
                normalized = normalize_formula(formula)
                digest = formula_hash(normalized)
                formula_text = " ".join(normalized)
            except ValueError:
                formula_text = formula if isinstance(formula, str) else " ".join(formula)
        failure = FailureRecord(
            reason=reason,
            formula=formula_text,
            formula_hash=digest,
            generation_method=generation_method,
            parent_formulas=list(parent_formulas),
            details=details or {},
        )
        self.failures.append(failure)
        return failure

    def add_candidate(
        self,
        *,
        candidate_id: str,
        formula: str | Iterable[str],
        parent_formulas: Iterable[str],
        generation_method: str,
        complexity: int,
        fold_metrics: list[dict[str, Any]],
        reward: dict[str, Any],
        signal: Sequence[float] | np.ndarray | None = None,
        correlation: tuple[float, str | None] | None = None,
    ) -> PoolAddResult:
        tokens = normalize_formula(formula)
        digest = formula_hash(tokens)
        formula_text = " ".join(tokens)
        parents = list(parent_formulas)
        if digest in self.candidates:
            self.record_failure(
                reason="duplicate_formula",
                formula=tokens,
                generation_method=generation_method,
                parent_formulas=parents,
                details={"existing_candidate_id": self.candidates[digest].candidate_id},
            )
            return PoolAddResult(False, None, "duplicate_formula")

        max_corr, matched = correlation or self.max_abs_correlation(signal)
        rejection_reason = (
            "high_correlation" if matched is not None and max_corr > self.correlation_threshold else None
        )
        candidate = FactorCandidate(
            candidate_id=candidate_id,
            formula_hash=digest,
            formula=formula_text,
            tokens=list(tokens),
            parent_formulas=parents,
            generation_method=generation_method,
            complexity=int(complexity),
            fold_metrics=fold_metrics,
            reward=reward,
            status="rejected" if rejection_reason else "accepted",
            rejection_reason=rejection_reason,
            max_abs_correlation=float(max_corr),
            correlated_with=matched,
        )
        self.candidates[digest] = candidate
        if rejection_reason:
            self.record_failure(
                reason=rejection_reason,
                formula=tokens,
                generation_method=generation_method,
                parent_formulas=parents,
                details={
                    "max_abs_correlation": float(max_corr),
                    "threshold": self.correlation_threshold,
                    "correlated_with": matched,
                },
            )
            return PoolAddResult(False, candidate, rejection_reason)

        if signal is not None:
            vector = np.asarray(signal, dtype=float).reshape(-1)
            if not np.all(np.isfinite(vector)):
                raise ValueError("accepted factor signal must be finite")
            self._signals[digest] = vector.copy()
        return PoolAddResult(True, candidate)

    def accepted_candidates(self) -> list[FactorCandidate]:
        return [candidate for candidate in self.candidates.values() if candidate.status == "accepted"]

    def ranked_candidates(self) -> list[FactorCandidate]:
        return sorted(
            self.accepted_candidates(),
            key=lambda candidate: (
                float(candidate.reward.get("total", float("-inf"))),
                candidate.formula_hash,
            ),
            reverse=True,
        )

    def to_dict(self, *, include_signals: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "correlation_threshold": self.correlation_threshold,
            "n_unique_candidates": len(self.candidates),
            "n_accepted": len(self.accepted_candidates()),
            "candidates": [candidate.to_dict() for candidate in self.candidates.values()],
            "failures": [failure.to_dict() for failure in self.failures],
        }
        if include_signals:
            data["signals"] = {
                candidate_hash: signal.tolist()
                for candidate_hash, signal in self._signals.items()
            }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FactorPool:
        pool = cls(correlation_threshold=float(data["correlation_threshold"]))
        for candidate_data in data.get("candidates", []):
            candidate = FactorCandidate(**candidate_data)
            pool.candidates[candidate.formula_hash] = candidate
        pool.failures = [FailureRecord(**item) for item in data.get("failures", [])]
        pool._signals = {
            candidate_hash: np.asarray(signal, dtype=float)
            for candidate_hash, signal in data.get("signals", {}).items()
        }
        return pool

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
