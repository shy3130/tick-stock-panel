from types import SimpleNamespace

import httpx
import pytest

from app.api import ext_data
from app.services.ext_data import ExtConfig, ExtField, PullConfig


@pytest.mark.asyncio
async def test_pull_preview_does_not_inherit_environment_proxy(monkeypatch):
    client_kwargs = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"symbol": "000001.SZ"}]

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            assert method == "GET"
            assert url == "https://example.test/data.json"
            assert kwargs == {"headers": {}}
            return FakeResponse()

    config = ExtConfig(
        id="proxy_regression",
        label="proxy regression",
        mode="snapshot",
        fields=[ExtField("symbol")],
        pull=PullConfig(url="https://example.test/data.json"),
    )
    store = SimpleNamespace(get=lambda _config_id: config)
    monkeypatch.setattr(ext_data, "_store", lambda _request: store)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await ext_data.test_pull(SimpleNamespace(), config.id)

    assert result == {
        "status": "ok",
        "total_rows": 1,
        "preview": [{"symbol": "000001.SZ"}],
        "has_symbol": True,
    }
    assert client_kwargs == {"timeout": 30, "trust_env": False}
