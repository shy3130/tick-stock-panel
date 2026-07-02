"""Provider registry."""
from __future__ import annotations

import os

from app.data_providers.fquant_provider import FQuantProvider
from app.data_providers.tickflow_provider import TickFlowProvider

_PROVIDERS = {
    "tickflow": TickFlowProvider,
    "fquant": FQuantProvider,
}


def normalize_provider_name(name: str | None, default: str = "tickflow") -> str:
    provider_name = (name or default).strip().lower() or default
    if provider_name not in _PROVIDERS:
        raise ValueError(f"Unsupported data provider: {name}")
    return provider_name


def get_active_provider_name() -> str:
    """Return the process-effective provider name.

    ``DATA_PROVIDER`` is an ops-level override. When it is absent, the persisted
    preferences value drives local UI/runtime switching.
    """
    env_provider = os.environ.get("DATA_PROVIDER")
    if env_provider:
        return normalize_provider_name(env_provider)

    try:
        from app.services import preferences
        return normalize_provider_name(preferences.get_data_provider())
    except Exception:  # noqa: BLE001
        return "tickflow"


def get_provider(name: str = "tickflow"):
    provider_cls = _PROVIDERS[normalize_provider_name(name)]
    return provider_cls()
