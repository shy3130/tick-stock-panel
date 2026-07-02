"""Provider registry."""
from __future__ import annotations

from app.data_providers.fquant_provider import FQuantProvider
from app.data_providers.tickflow_provider import TickFlowProvider

_PROVIDERS = {
    "tickflow": TickFlowProvider,
    "fquant": FQuantProvider,
}


def get_provider(name: str = "tickflow"):
    provider_cls = _PROVIDERS.get((name or "tickflow").lower())
    if provider_cls is None:
        raise ValueError(f"Unsupported data provider: {name}")
    return provider_cls()
