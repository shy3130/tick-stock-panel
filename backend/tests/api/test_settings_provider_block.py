from app.api.settings import get_settings


def test_settings_has_provider_and_tickflow_block(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )

    out = get_settings()

    assert out["data_provider"] == "fquant_local"
    assert out["mode"] == "fquant_local"
    assert set(out["tickflow"]) == {
        "api_key_masked",
        "has_key",
        "tier_label",
        "current_endpoint",
        "probe_log",
        "missing_caps",
        "extras_caps",
    }
    assert out["tier_label"] == out["tickflow"]["tier_label"]
    assert out["tickflow_api_key_masked"] == out["tickflow"]["api_key_masked"]
