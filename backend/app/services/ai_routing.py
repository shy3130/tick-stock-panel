"""Explicit AI profile route policy + in-memory health registry (P3).

Default: allow_profile_fallback=False (no fallback).
- primary always tried first.
- when enabled: try in [primary] + allowlist order (dedup, order preserved).
- only non-primary candidates are skipped if in cooldown.
- qualifying failures (provider/quota/auth/timeout) trigger capped exponential cooldown.
- success updates latency EWMA.
- health is purely in-memory; never contains keys, prompts or full responses.
- cancelled and (schema/immutable validation) never trigger profile fallback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.services import preferences

FAILURE_CATEGORIES_FOR_COOLDOWN: set[str] = {"provider", "quota", "auth", "timeout"}


@dataclass
class RoutePolicy:
    allow_profile_fallback: bool = False
    fallback_profile_ids: list[str] = field(default_factory=list)


@dataclass
class ProfileHealth:
    profile_id: str
    consecutive_failures: int = 0
    last_success_monotonic: float = 0.0
    last_error_category: str | None = None
    cooldown_until_monotonic: float = 0.0
    latency_ewma_ms: float = 0.0


class ProfileHealthRegistry:
    """Memory-only health for profiles.

    - record_failure on qualifying cats increments consecutive and sets cooldown.
    - record_success resets counters and updates EWMA.
    - No credentials or prompt content ever stored.
    """

    def __init__(self, base_cooldown_s: float = 5.0, max_cooldown_s: float = 300.0, ewma_alpha: float = 0.25) -> None:
        self._health: dict[str, ProfileHealth] = {}
        self._base = base_cooldown_s
        self._max = max_cooldown_s
        self._alpha = ewma_alpha

    def _ensure(self, profile_id: str) -> ProfileHealth:
        if profile_id not in self._health:
            self._health[profile_id] = ProfileHealth(profile_id=profile_id)
        return self._health[profile_id]

    def record_success(self, profile_id: str, latency_ms: float) -> None:
        h = self._ensure(profile_id)
        h.consecutive_failures = 0
        h.last_success_monotonic = time.monotonic()
        h.cooldown_until_monotonic = 0.0
        h.last_error_category = None
        if h.latency_ewma_ms <= 0.0:
            h.latency_ewma_ms = float(latency_ms)
        else:
            h.latency_ewma_ms = self._alpha * float(latency_ms) + (1.0 - self._alpha) * h.latency_ewma_ms

    def record_failure(self, profile_id: str, category: str, latency_ms: float = 0.0) -> None:
        if category not in FAILURE_CATEGORIES_FOR_COOLDOWN:
            return
        h = self._ensure(profile_id)
        h.consecutive_failures += 1
        h.last_error_category = category
        cd = min(self._base * (2 ** max(0, h.consecutive_failures - 1)), self._max)
        h.cooldown_until_monotonic = time.monotonic() + cd

    def is_in_cooldown(self, profile_id: str) -> bool:
        h = self._ensure(profile_id)
        if h.cooldown_until_monotonic <= 0.0:
            return False
        return time.monotonic() < h.cooldown_until_monotonic

    def get_health(self, profile_id: str) -> dict[str, Any]:
        h = self._ensure(profile_id)
        now = time.monotonic()
        rem = max(0.0, h.cooldown_until_monotonic - now)
        return {
            "profile_id": h.profile_id,
            "consecutive_failures": h.consecutive_failures,
            "last_error_category": h.last_error_category,
            "in_cooldown": rem > 0,
            "cooldown_remaining_s": round(rem, 2),
            "latency_ewma_ms": round(h.latency_ewma_ms, 1),
        }

    def reset(self, profile_id: str | None = None) -> None:
        if profile_id:
            self._health.pop(profile_id, None)
        else:
            self._health.clear()


_registry: ProfileHealthRegistry | None = None


def get_health_registry() -> ProfileHealthRegistry:
    global _registry
    if _registry is None:
        _registry = ProfileHealthRegistry()
    return _registry


# --- persistence (delegates to preferences for non-secret policy) ---

def load_route_policy() -> RoutePolicy:
    d = preferences.get_ai_route_policy()
    return RoutePolicy(
        allow_profile_fallback=bool(d.get("allow_profile_fallback", False)),
        fallback_profile_ids=[str(x).strip() for x in (d.get("fallback_profile_ids") or []) if str(x).strip()],
    )


def save_route_policy(policy: RoutePolicy) -> RoutePolicy:
    preferences.set_ai_route_policy(
        bool(policy.allow_profile_fallback),
        [str(x).strip() for x in (policy.fallback_profile_ids or []) if str(x).strip()],
    )
    return load_route_policy()


def validate_route_policy(allow: bool, ids: list[str], available_ids: set[str]) -> RoutePolicy:
    """Normalize + validate. Raises ValueError for unknown/illegal ids. Dedups preserving order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in (ids or []):
        iid = str(raw).strip()
        if not iid:
            continue
        if iid in seen:
            continue
        if iid not in available_ids:
            raise ValueError(f"fallback profile id not found: {iid}")
        seen.add(iid)
        cleaned.append(iid)
    return RoutePolicy(allow_profile_fallback=bool(allow), fallback_profile_ids=cleaned)


def build_fallback_chain(primary: str | None, policy: RoutePolicy, available: set[str]) -> list[str]:
    """primary (if present) always first, followed by policy list (dedup, order preserved)."""
    chain: list[str] = []
    p = (primary or "").strip()
    if p and p in available:
        chain.append(p)
    for fid in (policy.fallback_profile_ids or []):
        fid = fid.strip()
        if fid and fid in available and fid not in chain:
            chain.append(fid)
    if not chain and p:
        chain = [p]
    return chain


def classify_provider_error(exc: BaseException) -> str:
    """Classify for health + fallback decision. Mirrors runtime intent but focused on provider surface."""
    text = str(exc or "").lower()
    name = type(exc).__name__.lower()
    if "quota" in name or "ratelimit" in name or "rate limit" in text or "quota" in text:
        return "quota"
    if any(k in name or k in text for k in ("auth", "permission", "unauthor", "api key", "invalid_api")):
        return "auth"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in name or "timeout" in text:
        return "timeout"
    if ("model" in text and any(x in text for x in ("not", "exist", "unknown", "404"))) or "404" in text:
        return "provider"
    return "provider"


def is_fallback_worthy(category: str) -> bool:
    return category in FAILURE_CATEGORIES_FOR_COOLDOWN
