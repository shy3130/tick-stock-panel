"""Product-facing lifecycle metadata for the strategy registry.

Trading implementations remain independently loadable.  This catalog only controls
default discovery and default bulk execution; explicit strategy IDs always work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CORE_STRATEGY_IDS = frozenset(
    {
        "bullish_alignment",
        "trend_breakout",
        "pullback_to_support",
    }
)

_TOOL_IDS = frozenset({"custom_factor"})
_EXPERIMENTAL_IDS = frozenset(
    {
        "factor_ensemble",
        "regime_conditional",
        "oversold_reversal",
        "limit_up_momentum",
        "quality_momentum_v1",
    }
)
_LEGACY_IDS = frozenset(
    {
        "boll_breakout",
        "broken_board_recovery",
        "consecutive_limit_ups",
        "high_turnover_surge",
        "low_volatility_leader",
        "ma_golden_cross",
        "macd_golden",
        "n_day_low_reversal",
        "near_limit_up",
        "oversold_bounce",
        "pullback_ma20_bounce",
        "strong_open",
        "volume_price_surge",
    }
)

BUILTIN_STRATEGY_IDS = CORE_STRATEGY_IDS | _TOOL_IDS | _EXPERIMENTAL_IDS | _LEGACY_IDS

_FAILED_REPLAY_IDS = frozenset(
    {
        "bullish_alignment",
        "trend_breakout",
        "pullback_to_support",
        "oversold_reversal",
        "limit_up_momentum",
        "factor_ensemble",
        "regime_conditional",
        "quality_momentum_v1",
    }
)


def _builtin_metadata(strategy_id: str) -> dict[str, Any]:
    if strategy_id in CORE_STRATEGY_IDS:
        lifecycle = "core"
    elif strategy_id in _TOOL_IDS:
        lifecycle = "tool"
    elif strategy_id in _EXPERIMENTAL_IDS:
        lifecycle = "experimental"
    else:
        lifecycle = "legacy"

    if strategy_id == "custom_factor":
        evidence_status = "not_a_standalone_alpha"
    elif strategy_id in _FAILED_REPLAY_IDS:
        evidence_status = "historical_replay_failed"
    else:
        evidence_status = "unverified"

    return {
        "lifecycle": lifecycle,
        "visible_by_default": strategy_id in CORE_STRATEGY_IDS,
        "evidence_status": evidence_status,
    }


def apply_catalog_metadata(meta: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Return registry metadata with safe product defaults.

    Unknown built-ins are hidden until deliberately classified.  User-created and AI
    strategies stay visible so saving a strategy never makes it disappear from lists.
    """
    enriched = dict(meta)
    strategy_id = str(enriched.get("id", ""))
    if source == "builtin":
        enriched.update(_builtin_metadata(strategy_id))
    else:
        enriched.update(
            {
                "lifecycle": "user",
                "visible_by_default": True,
                "evidence_status": "unverified",
            }
        )
    return enriched


def is_default_strategy(meta: Mapping[str, Any]) -> bool:
    return bool(meta.get("visible_by_default", False))


def include_strategy(meta: Mapping[str, Any], *, include_experimental: bool) -> bool:
    """The compatibility flag includes every non-default lifecycle, not just experiments."""
    return bool(include_experimental or is_default_strategy(meta))
