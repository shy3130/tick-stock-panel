from app.api.settings import get_settings


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
