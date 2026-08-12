from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import financials
from app.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    app = FastAPI()
    app.state.capabilities = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(financials.router)
    monkeypatch.setattr(
        financials,
        "get_financial_df",
        lambda data_dir, table: pl.DataFrame(
            {
                "symbol": ["600519.SH", "000001.SZ"],
                "table": [table, table],
            }
        ),
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/financials/metrics",
        "/api/financials/income",
        "/api/financials/balance-sheet",
        "/api/financials/cash-flow",
        "/api/financials/quick",
        "/api/financials/forecast",
    ],
)
def test_financial_queries_require_symbol_and_return_only_that_symbol(
    client: TestClient,
    path: str,
):
    assert client.get(path).status_code == 422

    response = client.get(path, params={"symbol": "600519.SH"})

    assert response.status_code == 200
    assert response.json()["data"] == [
        {"symbol": "600519.SH", "table": path.rsplit("/", 1)[-1].replace("-", "_")}
    ]
