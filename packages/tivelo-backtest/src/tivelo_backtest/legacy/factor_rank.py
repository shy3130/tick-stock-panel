"""The legacy factor-rank strategy, evaluated over explicitly prepared market fields."""
from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ..research.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from ..research.scoring import build_matrix_score

_MAX_FACTORS = 4
_VALID_DIRECTIONS = {"high", "low"}


class FactorRankResearchMatrixStrategy:
    def __init__(
        self,
        scoring: dict[str, float] | None = None,
        directions: dict[str, str] | None = None,
        *,
        resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray] = MarketDataMatrix.field,
    ) -> None:
        self._resolve_feature = resolve_feature
        self._scoring = _validated_scoring(scoring) if scoring is not None else None
        self._directions = (
            _validated_directions(directions, self._scoring)
            if directions is not None and self._scoring is not None
            else None
        )

    def required_fields(self) -> frozenset[str]:
        return frozenset({
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover_rate",
        })

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def required_fields_for_params(self, params: dict) -> frozenset[str]:
        if self._scoring is not None:
            return frozenset(self._scoring)
        raw = params.get("scoring")
        if raw is None:
            return frozenset()
        return frozenset(_validated_scoring(raw))

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        scoring = self._scoring or _validated_scoring(params.get("scoring"))
        directions = self._directions or _validated_directions(
            params.get("directions"), scoring
        )
        entry_score = _bounded_float(params.get("entry_score", 70.0), "entry_score")
        exit_score = _bounded_float(params.get("exit_score", 40.0), "exit_score")
        top_rank = int(params.get("top_rank", 20))
        if not 1 <= top_rank <= 100:
            raise ValueError("top_rank must be between 1 and 100")
        if exit_score > entry_score:
            raise ValueError("exit_score must not exceed entry_score")

        universe = np.isfinite(market.close)
        score = build_matrix_score(
            market,
            universe,
            scoring,
            "score",
            True,
            fallback=np.zeros(market.shape, dtype=np.float32),
            directions=directions,
            resolve_feature=self._resolve_feature,
        )
        entry = universe & (score >= np.float32(entry_score))
        entry = _limit_top_rank(entry, score, top_rank)
        exit_ = universe & (score <= np.float32(exit_score))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_factor_rank_entry",),
            exit_signal_ids=("signal_factor_rank_exit",),
        )


def _validated_scoring(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("factor-rank research requires a non-empty scoring mapping")
    if len(raw) > _MAX_FACTORS:
        raise ValueError(f"factor-rank research supports at most {_MAX_FACTORS} factors")
    scoring: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("scoring factor names must be non-empty strings")
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"scoring weight for {name!r} must be numeric") from exc
        if not np.isfinite(weight) or weight <= 0.0:
            raise ValueError(f"scoring weight for {name!r} must be finite and positive")
        scoring[name] = weight
    return scoring


def _validated_directions(
    raw: object,
    scoring: dict[str, float],
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("directions must be a mapping")
    unknown = sorted(set(raw) - set(scoring))
    if unknown:
        raise ValueError(f"directions contain factors absent from scoring: {unknown}")
    directions: dict[str, str] = {}
    for name, value in raw.items():
        if value not in _VALID_DIRECTIONS:
            raise ValueError(
                f"direction for {name!r} must be one of {sorted(_VALID_DIRECTIONS)}"
            )
        directions[str(name)] = str(value)
    return directions


def _bounded_float(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not np.isfinite(number) or not 0.0 <= number <= 100.0:
        raise ValueError(f"{name} must be between 0 and 100")
    return number


def _limit_top_rank(
    eligible: np.ndarray,
    score: np.ndarray,
    top_rank: int,
) -> np.ndarray:
    result = np.zeros(eligible.shape, dtype=bool)
    for time_id in range(eligible.shape[0]):
        asset_ids = np.flatnonzero(eligible[time_id])
        if asset_ids.size <= top_rank:
            result[time_id, asset_ids] = True
            continue
        # mergesort preserves asset-axis order for equal scores.
        order = np.argsort(-score[time_id, asset_ids], kind="stable")[:top_rank]
        result[time_id, asset_ids[order]] = True
    return result
