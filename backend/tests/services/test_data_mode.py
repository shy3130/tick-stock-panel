from app.services import data_mode


def test_mode_is_provider_name(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )

    assert data_mode.current_data_mode() == "fquant_local"


def test_mode_falls_back_to_fquant_local(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert data_mode.current_data_mode() == "fquant_local"
