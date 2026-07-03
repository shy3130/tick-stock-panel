from app.data_providers import registry
from app.services import preferences


def test_fquant_local_registered():
    assert registry.normalize_provider_name("fquant_local") == "fquant_local"


def test_daily_provider_uses_daily_preference(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")

    assert registry.get_active_provider_name("daily") == "fquant_local"


def test_financial_and_depth_use_capability_preferences(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(preferences, "get_financial_data_provider", lambda: "fquant")
    monkeypatch.setattr(preferences, "get_depth_data_provider", lambda: "tickflow")

    assert registry.get_active_provider_name("financial") == "fquant"
    assert registry.get_active_provider_name("depth") == "tickflow"


def test_env_provider_overrides_capability_preference(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "tickflow")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")

    assert registry.get_active_provider_name("daily") == "tickflow"


def test_fquant_local_uses_disk_engine():
    provider = registry.get_provider("fquant_local")

    assert provider.name == "fquant_local"
    assert provider._engine.__class__.__name__ == "EngineDataDiskClient"
