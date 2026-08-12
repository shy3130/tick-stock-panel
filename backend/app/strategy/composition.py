"""Deterministic composition of matrix-native strategy signals.

The module owns only the composition contract.  Strategy loading, parameter
resolution and feature preparation remain responsibilities of StrategyEngine
and the backtest service.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from app.backtest.matrix import SignalMatrix, make_signal_matrix, validate_signal_matrix

EntryMode = Literal["and", "or", "regime_switch"]


@dataclass(frozen=True)
class StrategyComponent:
    strategy_id: str
    weight: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyComposition:
    """Validated v1 composition contract.

    The first component is the primary strategy.  It owns portfolio and risk
    settings in the surrounding backtest config.  Every component contributes
    entry eligibility and a cross-sectional ranking score; any component exit
    closes the position.
    """

    components: tuple[StrategyComponent, ...]
    entry_mode: EntryMode = "and"
    score_mode: Literal["weighted_rank", "active_score"] = "weighted_rank"
    regime: dict[str, Any] | None = None

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        primary_strategy_id: str,
    ) -> StrategyComposition:
        if not isinstance(payload, Mapping):
            raise ValueError("composition must be an object")
        unknown = set(payload) - {"components", "entry_mode", "score_mode", "regime"}
        if unknown:
            raise ValueError(f"composition contains unsupported fields: {sorted(unknown)}")

        raw_components = payload.get("components")
        if not isinstance(raw_components, list) or not 2 <= len(raw_components) <= 8:
            raise ValueError("composition.components must contain 2 to 8 strategies")

        components: list[StrategyComponent] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_components):
            if not isinstance(raw, Mapping):
                raise ValueError(f"composition.components[{index}] must be an object")
            component_unknown = set(raw) - {"strategy_id", "weight", "params", "overrides"}
            if component_unknown:
                raise ValueError(
                    f"composition.components[{index}] contains unsupported fields: "
                    f"{sorted(component_unknown)}"
                )
            strategy_id = str(raw.get("strategy_id", "")).strip()
            if not strategy_id:
                raise ValueError(f"composition.components[{index}].strategy_id is required")
            if strategy_id in seen:
                raise ValueError(f"composition contains duplicate strategy: {strategy_id}")
            seen.add(strategy_id)

            try:
                weight = float(raw.get("weight", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"composition.components[{index}].weight must be a number"
                ) from exc
            if not np.isfinite(weight) or weight <= 0.0:
                raise ValueError(
                    f"composition.components[{index}].weight must be finite and positive"
                )
            params = raw.get("params") or {}
            overrides = raw.get("overrides") or {}
            if not isinstance(params, Mapping) or not isinstance(overrides, Mapping):
                raise ValueError(
                    f"composition.components[{index}] params and overrides must be objects"
                )
            components.append(
                StrategyComponent(
                    strategy_id=strategy_id,
                    weight=weight,
                    params=dict(params),
                    overrides=dict(overrides),
                )
            )

        if components[0].strategy_id != primary_strategy_id:
            raise ValueError(
                "composition first component must match the backtest strategy_id "
                f"({primary_strategy_id})"
            )
        entry_mode = str(payload.get("entry_mode", "and")).lower()
        if entry_mode not in {"and", "or", "regime_switch"}:
            raise ValueError(
                "composition.entry_mode must be 'and', 'or', or 'regime_switch'"
            )
        default_score_mode = "active_score" if entry_mode == "regime_switch" else "weighted_rank"
        score_mode = str(payload.get("score_mode", default_score_mode)).lower()
        if score_mode not in {"weighted_rank", "active_score"}:
            raise ValueError(
                "composition.score_mode must be 'weighted_rank' or 'active_score'"
            )
        regime_raw = payload.get("regime")
        regime = dict(regime_raw) if isinstance(regime_raw, Mapping) else None
        if entry_mode == "regime_switch":
            if len(components) != 2:
                raise ValueError("regime_switch composition requires exactly two strategies")
            if regime is None or regime.get("type") != "market_structure_v1":
                raise ValueError(
                    "regime_switch composition requires regime.type='market_structure_v1'"
                )
            if score_mode != "active_score":
                raise ValueError("regime_switch composition requires score_mode='active_score'")
        else:
            if regime_raw is not None:
                raise ValueError("composition.regime is only valid for regime_switch")
            if score_mode != "weighted_rank":
                raise ValueError("and/or composition requires score_mode='weighted_rank'")
        return cls(
            components=tuple(components),
            entry_mode=entry_mode,  # type: ignore[arg-type]
            score_mode=score_mode,  # type: ignore[arg-type]
            regime=regime,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_mode": self.entry_mode,
            "score_mode": self.score_mode,
            **({"regime": self.regime} if self.regime is not None else {}),
            "components": [
                {
                    "strategy_id": component.strategy_id,
                    "weight": component.weight,
                    "params": component.params,
                    "overrides": component.overrides,
                }
                for component in self.components
            ],
        }


def _cross_sectional_percentile(score: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """Rank eligible scores per row into (0, 1], averaging exact ties."""
    ranked = np.zeros(score.shape, dtype=np.float32)
    for row_id in range(score.shape[0]):
        asset_ids = np.flatnonzero(eligible[row_id])
        count = int(asset_ids.size)
        if count == 0:
            continue
        if count == 1:
            ranked[row_id, asset_ids[0]] = np.float32(1.0)
            continue
        values = score[row_id, asset_ids]
        order = np.argsort(values, kind="stable")
        sorted_values = values[order]
        sorted_ranks = np.empty(count, dtype=np.float32)
        start = 0
        while start < count:
            stop = start + 1
            while stop < count and sorted_values[stop] == sorted_values[start]:
                stop += 1
            average_position = ((start + 1) + stop) / 2.0
            sorted_ranks[start:stop] = np.float32(average_position / count)
            start = stop
        row_ranks = np.empty(count, dtype=np.float32)
        row_ranks[order] = sorted_ranks
        ranked[row_id, asset_ids] = row_ranks
    return ranked


def compose_signal_matrices(
    signals: Sequence[SignalMatrix],
    composition: StrategyComposition,
    *,
    regime_allow: np.ndarray | None = None,
) -> SignalMatrix:
    """Compose already-pipelined child signals without changing market data."""
    if len(signals) != len(composition.components):
        raise ValueError("signal matrix count does not match composition components")
    if not signals:
        raise ValueError("composition requires at least one signal matrix")
    shape = signals[0].shape
    for signal in signals:
        validate_signal_matrix(signal, shape)

    entries = [signal.entry.astype(bool, copy=False) for signal in signals]
    if composition.entry_mode == "regime_switch":
        if len(signals) != 2:
            raise ValueError("regime_switch requires exactly two signal matrices")
        if regime_allow is None:
            raise ValueError("regime_switch requires a regime_allow array")
        regime_allow = np.asarray(regime_allow, dtype=bool)
        if regime_allow.ndim != 1 or regime_allow.shape[0] != shape[0]:
            raise ValueError("regime_allow must be one-dimensional and time-aligned")
        bull = regime_allow.reshape(-1, 1)
        bear = ~bull
        combined_entry = (bull & entries[0]) | (bear & entries[1])
        active_exit = (
            bull & signals[0].exit.astype(bool, copy=False)
        ) | (
            bear & signals[1].exit.astype(bool, copy=False)
        )
        flip = np.zeros(shape[0], dtype=bool)
        flip[1:] = regime_allow[1:] != regime_allow[:-1]
        combined_exit = active_exit | np.broadcast_to(flip.reshape(-1, 1), shape)
        combined_score = np.where(
            bull,
            signals[0].score,
            signals[1].score,
        ).astype(np.float32)
        combined_score[~combined_entry] = np.float32(0.0)
        entry_codes = np.full(shape, -1, dtype=np.int16)
        entry_codes[bull & entries[0]] = 0
        entry_codes[bear & entries[1]] = 1
        exit_codes = np.full(shape, -1, dtype=np.int16)
        exit_codes[active_exit] = 0
        exit_codes[np.broadcast_to(flip.reshape(-1, 1), shape)] = 1
        return make_signal_matrix(
            shape,
            entry=combined_entry.astype(np.uint8),
            exit=combined_exit.astype(np.uint8),
            score=combined_score,
            entry_signal_code=entry_codes,
            exit_signal_code=exit_codes,
            entry_signal_ids=(
                f"regime:bull:{composition.components[0].strategy_id}",
                f"regime:bear:{composition.components[1].strategy_id}",
            ),
            exit_signal_ids=("regime:active_leg_exit", "regime:state_flip"),
        )
    if regime_allow is not None:
        raise ValueError("regime_allow is only valid for regime_switch")
    if composition.entry_mode == "and":
        combined_entry = np.logical_and.reduce(entries)
    else:
        combined_entry = np.logical_or.reduce(entries)
    combined_exit = np.logical_or.reduce(
        [signal.exit.astype(bool, copy=False) for signal in signals]
    )

    weight_total = sum(component.weight for component in composition.components)
    combined_score = np.zeros(shape, dtype=np.float32)
    for signal, component, eligible in zip(signals, composition.components, entries, strict=False):
        ranked = _cross_sectional_percentile(signal.score, eligible)
        combined_score += ranked * np.float32(component.weight / weight_total)
    combined_score[~combined_entry] = np.float32(0.0)

    entry_codes = np.where(combined_entry, 0, -1).astype(np.int16)
    exit_codes = np.where(combined_exit, 0, -1).astype(np.int16)
    return make_signal_matrix(
        shape,
        entry=combined_entry.astype(np.uint8),
        exit=combined_exit.astype(np.uint8),
        score=combined_score,
        entry_signal_code=entry_codes,
        exit_signal_code=exit_codes,
        entry_signal_ids=(f"composition:{composition.entry_mode}",),
        exit_signal_ids=("composition:any_exit",),
    )
