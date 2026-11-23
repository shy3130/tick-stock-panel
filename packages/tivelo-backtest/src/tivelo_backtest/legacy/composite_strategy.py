"""Bind locked daily matrix child strategies to the legacy composite merger."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from tivelo_contracts.errors import ErrorCode, TiveloError

from ..research.composite import CompositeChild, merge_signal_matrices, parse_composite_children
from ..research.matrix import MarketDataMatrix, SignalMatrix
from ..research.matrix_selection import MatrixPipelineConfig, select_matrix_signals
from ..research.panel_selection import DEFAULT_BASIC_FILTER
from ..research.scoring import effective_scoring, effective_scoring_directions, scoring_warmup_bars

if TYPE_CHECKING:
    from .daily_optimization import DailyMatrixStrategy


@dataclass(frozen=True)
class _ChildBinding:
    spec: CompositeChild
    strategy: DailyMatrixStrategy
    params: dict
    selection: MatrixPipelineConfig


class LegacyCompositeStrategy:
    def __init__(self, source, parameters_json, dependencies):
        settings = json.loads(parameters_json)
        meta = source.META
        specs = parse_composite_children(settings.get("children") or meta["children"]).children
        if {spec.strategy_id for spec in specs} != dependencies.keys():
            raise ValueError("Composite children differ from the locked dependencies")
        overrides = settings.get("child_overrides", {})
        if not isinstance(overrides, dict) or not overrides.keys() <= dependencies.keys():
            raise ValueError("Invalid composite child overrides")
        self.defaults = {
            **{p["id"]: p.get("default") for p in meta.get("params", []) if p.get("id")},
            **settings,
        }
        self.basic_filter = {
            **DEFAULT_BASIC_FILTER,
            **(getattr(source, "BASIC_FILTER", None) or {}),
            **(meta.get("basic_filter") or {}),
        }
        self.children = tuple(
            self._bind_child(spec, dependencies[spec.strategy_id], overrides.get(spec.strategy_id, {}))
            for spec in specs
        )

    @staticmethod
    def _bind_child(spec, strategy, overrides):
        if not math.isfinite(spec.weight):
            raise ValueError("Composite weights must be finite")
        if isinstance(strategy, LegacyCompositeStrategy) or not all(
            callable(getattr(strategy, name, None))
            for name in ("required_warmup_bars", "compute_signals")
        ):
            raise ValueError("Composite children must be non-composite daily matrix strategies")
        if not isinstance(overrides, dict) or not isinstance(overrides.get("params", {}), dict):
            raise ValueError("Invalid matrix child parameters")
        default = getattr(strategy, "selection", None)
        if not isinstance(default, MatrixPipelineConfig):
            default = MatrixPipelineConfig({}, {}, None, True)
        weights = effective_scoring(default.scoring, overrides)
        order = overrides.get("scoring_order")
        if order is not None:
            if not isinstance(order, list) or len(order) != len(weights) or set(order) != weights.keys():
                raise ValueError("Composite child scoring order differs from its weights")
            weights = {name: weights[name] for name in order}
        config = replace(
            default,
            scoring=weights,
            scoring_directions=effective_scoring_directions(overrides),
        )
        return _ChildBinding(spec, strategy, dict(overrides.get("params", {})), config)

    def required_warmup_bars(self, params: dict) -> int:
        warmup = 60
        for child in self.children:
            parameter_fields = getattr(child.strategy, "required_fields_for_params", None)
            fields = parameter_fields(child.params) if callable(parameter_fields) else ()
            warmup = max(
                warmup,
                child.strategy.required_warmup_bars(child.params),
                scoring_warmup_bars(child.selection.scoring),
                scoring_warmup_bars(dict.fromkeys(fields, 1.0)),
            )
        return warmup

    @staticmethod
    def validate_selection(selection: MatrixPipelineConfig | None) -> None:
        if selection is not None and (
            selection.scoring or selection.scoring_directions or selection.order_by not in (None, "score")
            or not selection.descending
        ):
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Composite ranking uses child scoring")

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
        *,
        selection: MatrixPipelineConfig | None = None,
        max_hold: int = 250,
    ) -> SignalMatrix:
        self.validate_selection(selection)
        combined = {**self.defaults, **params}
        mode = combined.get("merge_mode") or "union"
        if mode not in {"union", "intersect"}:
            raise ValueError("Unknown composite merge mode")
        basic = self.basic_filter if selection is None else selection.basic_filter
        asset_mask = None if selection is None else selection.asset_mask
        signals = [
            select_matrix_signals(
                market,
                child.strategy.compute_signals(market, child.params),
                replace(child.selection, basic_filter=basic, asset_mask=asset_mask),
            )
            for child in self.children
        ]
        return merge_signal_matrices(
            market.shape, signals,
            [(child.spec.strategy_id, child.spec.weight) for child in self.children],
            mode, int(combined.get("min_confirm") or 0), max_hold,
        )
