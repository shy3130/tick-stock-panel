"""Provider registry."""
from __future__ import annotations

import logging
import os

from app.data_providers.fquant_provider import FQuantProvider

logger = logging.getLogger(__name__)

_PROVIDERS = {
    "fquant": FQuantProvider,
    "fquant_local": lambda: FQuantProvider(engine_mode="disk"),
}


def normalize_provider_name(name: str | None, default: str = "fquant_local") -> str:
    provider_name = (name or default).strip().lower() or default
    if provider_name not in _PROVIDERS:
        raise ValueError(f"Unsupported data provider: {name}")
    return provider_name


def get_active_provider_name(capability: str | None = None) -> str:
    """Return the process-effective provider name.

    ``DATA_PROVIDER`` is an ops-level override. When it is absent, the persisted
    preferences value drives local UI/runtime switching.
    """
    env_provider = os.environ.get("DATA_PROVIDER")
    if env_provider:
        try:
            return normalize_provider_name(env_provider)
        except ValueError:
            logger.warning("Unsupported DATA_PROVIDER=%s, falling back to fquant_local", env_provider)
            return "fquant_local"

    try:
        from app.services import preferences
        if capability == "daily":
            return normalize_provider_name(preferences.get_daily_data_provider())
        if capability == "minute":
            return normalize_provider_name(preferences.get_minute_data_provider())
        if capability == "realtime":
            return normalize_provider_name(preferences.get_realtime_data_provider())
        if capability == "financial":
            return normalize_provider_name(preferences.get_financial_data_provider())
        if capability == "depth":
            return normalize_provider_name(preferences.get_depth_data_provider())
        if capability == "adj_factor":
            provider = preferences.get_adj_factor_provider()
            if provider == "same_as_daily":
                provider = preferences.get_daily_data_provider()
            return normalize_provider_name(provider)
        return normalize_provider_name(preferences.get_data_provider())
    except Exception:  # noqa: BLE001
        return "fquant_local"


def get_provider(name: str = "fquant_local"):
    provider_factory = _PROVIDERS[normalize_provider_name(name)]
    return provider_factory()
