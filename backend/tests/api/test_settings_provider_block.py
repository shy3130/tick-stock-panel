from app.api.settings import get_preferences, get_settings


def test_settings_has_provider_mode(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )

    out = get_settings()

    assert out["data_provider"] == "fquant_local"
    assert out["mode"] == "fquant_local"
    assert "tickflow" not in out
    assert "tickflow_api_key_masked" not in out


def test_preferences_exposes_financial_and_depth_provider(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr("app.services.preferences.get_financial_data_provider", lambda: "fquant")
    monkeypatch.setattr("app.services.preferences.get_depth_data_provider", lambda: "fquant_local")

    out = get_preferences()

    assert out["financial_data_provider"] == "fquant"
    assert out["depth_data_provider"] == "fquant_local"
