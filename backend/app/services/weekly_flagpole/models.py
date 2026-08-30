"""Frozen request/response contract for weekly flagpole research."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

FACTOR_ID = "weekly_flagpole_v1"
FACTOR_VERSION = 1
FACTOR_NAME = "周线拉旗杆旗形整理(研究)"
FACTOR_DESCRIPTION = "2-4 根连续周阳旗杆与旗面三口径的只读事件研究"
REACHABILITY = "daily_price_only"
DEFINITION_DOCUMENT = "docs/TODO.md#周线拉旗杆旗形整理介入因子"
POLE_WEEKS_MIN, POLE_WEEKS_MAX = 2, 4
THETA1_GRID = (0.15, 0.25, 0.40)
THETA2_GRID = (0.10, 0.20, 0.30)
WEEKLY_ZIGZAG_THRESHOLD = 0.05
FLAG_VOLUME_SHRINK_RATIO, RESTART_VOLUME_RATIO = 0.70, 1.20
FLAG_RETEST_TOLERANCE = 0.02
FLAG_MAX_WEEKS, NEW_POLE_WINDOW_WEEKS = 13, 13
ENTRY_VARIANTS = ("weekly_reclaim", "volume_shrink_restart", "flag_low_retest")
POLE_CONDITIONS = ("strict_limit_up", "loose")
FORWARD_HORIZONS = (21, 63, 126)
OOS_START = date(2025, 7, 1)
COST_BPS = 10.0
MIN_OOS_EVENTS, MIN_OOS_SYMBOLS = 30, 10
BOOTSTRAP_SEED, BOOTSTRAP_ROUNDS, MIN_VALID_BOOTSTRAP_REPLICATES = 42, 5000, 4750
VERDICT_HORIZON = 63


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WeeklyFlagpoleFactor(StrictModel):
    factor_id: str = FACTOR_ID
    version: int = FACTOR_VERSION
    name: str = FACTOR_NAME
    description: str = FACTOR_DESCRIPTION
    reachability: Literal["daily_price_only"] = REACHABILITY
    definition: str = DEFINITION_DOCUMENT


class WeeklyFlagpoleRequest(StrictModel):
    start: date
    end: date
    symbols: list[str] | None = None
    oos_start: date = OOS_START
    cost_bps: float = COST_BPS

    @model_validator(mode="after")
    def valid_range(self) -> WeeklyFlagpoleRequest:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if self.cost_bps < 0 or not math.isfinite(self.cost_bps):
            raise ValueError("cost_bps must be finite and non-negative")
        return self


class WeeklyFlagpoleCapabilities(StrictModel):
    reader_available: bool = False
    provenance_valid: bool = False
    methods_complete: bool = False
    problems: list[str] = []


class WeeklyFlagpoleResponse(StrictModel):
    factor: WeeklyFlagpoleFactor
    status: Literal["ok", "unavailable"]
    unavailable_reasons: list[str]
    request: WeeklyFlagpoleRequest
    capabilities: WeeklyFlagpoleCapabilities
    parameters: dict[str, Any]
    provenance: dict[str, Any]
    coverage: dict[str, Any] | None
    events: list[Any]
    censored: list[Any]
    research: dict[str, Any] | None
    diagnostics: dict[str, Any]
    note: str


_NON_ST_REGIMES = frozenset({"main_10", "star_20", "chinext_20", "beijing_30"})


def valid_limit_regime_fact(fact: Any) -> bool:
    if not isinstance(fact, dict):
        return False
    name, is_st, regime, price = (
        fact.get("name"),
        fact.get("is_st"),
        fact.get("regime"),
        fact.get("limit_up_price"),
    )
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(is_st, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(float(price))
        or price <= 0
    ):
        return False
    if is_st != ("ST" in name.upper()):
        return False
    return regime == "st_5" if is_st else regime in _NON_ST_REGIMES


def is_limit_up(
    facts: dict[date, dict[str, Any]], bar: dict[str, Any], tolerance: float = 0.005
) -> bool | None:
    fact = facts.get(bar.get("date"))
    close = bar.get("raw_close")
    if not valid_limit_regime_fact(fact) or close is None:
        return None
    return abs(float(close) - float(fact["limit_up_price"])) <= tolerance


_BANNED = (
    "buy",
    "sell",
    "target",
    "stop",
    "action",
    "entry",
    "exit",
    "position",
    "order",
    "long",
    "short",
    "hold",
    "trade",
)
_EVIDENCE_KEYS = frozenset({"field", "actual", "op", "target"})


def assert_no_trading_tokens(name: str) -> None:
    low = name.lower()
    for token in _BANNED:
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", low):
            raise ValueError(f"trading semantics token {token!r} forbidden in field {name!r}")


def validate_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) not in _EVIDENCE_KEYS:
                assert_no_trading_tokens(str(key))
            validate_payload(item)
    elif isinstance(value, list):
        for item in value:
            validate_payload(item)


def evidence(field: str, actual: Any, op: str, target: Any) -> dict[str, Any]:
    assert_no_trading_tokens(field)
    return {"field": field, "actual": actual, "op": op, "target": target}


def valid_manifest_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def valid_provenance(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"canonical", "markets"}:
        return False
    return all(
        isinstance(value[s], dict)
        and isinstance(value[s].get("generation"), str)
        and valid_manifest_sha256(value[s].get("manifest_sha256"))
        for s in value
    )
