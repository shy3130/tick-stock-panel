"""Runtime data-source mode helpers."""
from __future__ import annotations


def is_local_daily_mode() -> bool:
    from app.data_providers.registry import get_active_provider_name

    try:
        return get_active_provider_name("daily") == "fquant_local"
    except Exception:  # noqa: BLE001
        return False


def current_data_mode() -> str:
    from app.data_providers.registry import get_active_provider_name

    try:
        provider = get_active_provider_name()
    except Exception:  # noqa: BLE001
        provider = "tickflow"
    if provider != "tickflow":
        return provider

    from app.tickflow import client as tf_client
    return tf_client.current_mode()
