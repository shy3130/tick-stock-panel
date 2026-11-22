"""Bind existing daily Polars filters to the shared long-panel selection path."""

import json

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..research.matrix_selection import MatrixPipelineConfig
from ..research.panel_selection import (
    DEFAULT_BASIC_FILTER,
    apply_panel_score,
    panel_basic_filter,
    panel_history_mask,
    panel_signal_column,
    panel_signal_mask,
)


def legacy_parameter_defaults(meta: dict) -> dict:
    params = meta.get("params") or []
    if isinstance(params, dict):
        return {
            name: value.get("default") if isinstance(value, dict) else value
            for name, value in params.items()
        }
    defaults = {
        entry["id"]: entry.get("default") for entry in params
        if isinstance(entry, dict) and entry.get("id")
    }
    defaults.update({name: None for name in params if isinstance(name, str)})
    return defaults


class LegacyPolarsStrategy:
    def __init__(self, source, parameters_json: str):
        self.filter = getattr(source, "filter", None)
        self.filter_history = getattr(source, "filter_history", None)
        if (self.filter is None) == (self.filter_history is None) or not callable(
            self.filter_history if self.filter_history is not None else self.filter
        ):
            raise ValueError("Expected one legacy Polars filter")
        self.entry_signals = list(getattr(source, "ENTRY_SIGNALS", []) or [])
        self.exit_signals = list(getattr(source, "EXIT_SIGNALS", []) or [])
        meta = source.META
        self.required_fields = frozenset(meta.get("required_features") or ()) | frozenset(
            getattr(source, "REQUIRED_FEATURES", None) or ()
        )
        self.lookback = max(60, int(getattr(source, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1))
        self.defaults = {**legacy_parameter_defaults(meta), **json.loads(parameters_json)}
        self.selection = MatrixPipelineConfig(
            basic_filter={
                **DEFAULT_BASIC_FILTER,
                **(getattr(source, "BASIC_FILTER", None) or {}),
                **(meta.get("basic_filter") or {}),
            },
            scoring=dict(meta.get("scoring") or {}),
            order_by=meta.get("order_by"),
            descending=bool(meta.get("descending", True)),
        )

    def required_warmup_bars(self, params: dict) -> int:
        return self.lookback

    def validate_panel(self, panel: pl.DataFrame, selection: MatrixPipelineConfig | None = None) -> MatrixPipelineConfig:
        config = selection if selection is not None else self.selection
        if config.asset_mask is not None:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Polars selection expects an explicit panel universe")
        missing = (self.required_fields | {
            panel_signal_column(name) for name in (*self.entry_signals, *self.exit_signals)
        }) - set(panel.columns)
        if missing:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Missing Polars strategy inputs: " + ", ".join(sorted(missing))
            )
        return config

    def evaluate(self, panel: pl.DataFrame, params: dict, selection: MatrixPipelineConfig | None = None):
        config = self.validate_panel(panel, selection)
        try:
            candidate = pl.repeat(True, panel.height, eager=True).cast(pl.Boolean)
            if config.basic_filter.get("enabled", True):
                basic = panel_basic_filter(panel, config.basic_filter)
                if basic is not None:
                    candidate &= panel.select(basic).to_series().fill_null(False)
            effective_params = {**self.defaults, **params}
            if self.filter_history is not None:
                candidate &= panel_history_mask(panel, self.filter_history(panel, effective_params))
            else:
                condition = self.filter(panel, effective_params)
                if condition is not None:
                    mask = panel.select(condition.alias("_candidate_filter")).to_series()
                    candidate &= mask.fill_null(False).cast(pl.Boolean)
            scored = apply_panel_score(
                panel, config.scoring, config.scoring_directions,
                order_by=config.order_by, descending=config.descending, universe_mask=candidate,
            )
        except pl.exceptions.ColumnNotFoundError as exc:
            raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Missing Polars strategy input") from exc
        except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
            raise TiveloError(ErrorCode.STRATEGY_FAILED, "Legacy Polars strategy evaluation failed") from exc
        entry = candidate
        if self.entry_signals:
            entry = entry & panel_signal_mask(scored, self.entry_signals, "_entry")
        exits = panel_signal_mask(scored, self.exit_signals, "_exit")
        return scored, candidate, entry, exits
