from app.services.ai_provider import _openai_client


def test_openai_client_ignores_proxy_env(monkeypatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")

    client = _openai_client("test-key", 1.0)

    assert client._client.trust_env is False
