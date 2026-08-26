import pytest

from app.services import ext_http, ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig


@pytest.mark.asyncio
async def test_fetch_and_ingest_does_not_inherit_environment_proxy(monkeypatch, tmp_path):
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
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ext_http.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ext_pull, "rows_to_parquet", lambda *_args, **_kwargs: 1)

    rows, snapshot_date = await ext_pull.fetch_and_ingest(config, tmp_path)

    assert rows == 1
    assert snapshot_date
    assert client_kwargs == {"timeout": 30, "trust_env": False}
