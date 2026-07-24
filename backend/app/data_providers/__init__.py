"""Market data provider abstraction.

Providers normalize external data sources into the internal parquet schema.
"""
from app.data_providers.base import AssetType, MarketDataProvider, ProviderCapabilities
from app.data_providers.registry import get_provider

__all__ = ["AssetType", "MarketDataProvider", "ProviderCapabilities", "get_provider"]
