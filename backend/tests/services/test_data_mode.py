from app.services import data_mode


def test_mode_is_provider_name_when_not_tickflow(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )

    assert data_mode.current_data_mode() == "fquant_local"


def test_mode_delegates_to_tf_client_when_tickflow(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "tickflow",
    )
    monkeypatch.setattr("app.tickflow.client.current_mode", lambda: "api_key")

    assert data_mode.current_data_mode() == "api_key"
