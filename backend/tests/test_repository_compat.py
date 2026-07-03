from app.storage.repository import DataStore, KlineRepository
from app.tickflow.repository import DataStore as OldDataStore
from app.tickflow.repository import KlineRepository as OldKlineRepository


def test_tickflow_repository_compat_exports_storage_objects():
    assert OldDataStore is DataStore
    assert OldKlineRepository is KlineRepository
