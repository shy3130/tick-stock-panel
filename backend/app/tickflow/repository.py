"""Compatibility shim. Use app.storage.repository instead."""
from app.storage.repository import DataStore, KlineRepository  # noqa: F401
