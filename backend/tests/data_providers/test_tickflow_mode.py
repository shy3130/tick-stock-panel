from app.tickflow import client


def test_current_mode_returns_non_tickflow_provider(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "fquant_local")

    assert client.current_mode() == "fquant_local"
