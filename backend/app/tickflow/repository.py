"""Compatibility import for the old TickFlow-era repository path."""
from app.storage.repository import DataStore, KlineRepository

__all__ = ["DataStore", "KlineRepository"]
