import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices
from app.services import index_sync


class InstrumentRepo:
    def __init__(self, frame: pl.DataFrame | None = None) -> None:
        self.frame = frame if frame is not None else pl.DataFrame()
        self.saved = pl.DataFrame()
        self.refreshed = False

    def get_index_instruments(self) -> pl.DataFrame:
        return self.frame

    def save_index_instruments(self, frame: pl.DataFrame) -> None:
        self.saved = frame
        self.frame = frame

    def refresh_index_views(self) -> None:
        self.refreshed = True


def test_index_list_filters_results_by_market() -> None:
    repo = InstrumentRepo(pl.DataFrame([
        {
            "symbol": "000001.SH", "name": "上证指数", "code": "000001",
            "market": "cn", "asset_type": "index",
        },
        {
            "symbol": "HSI.HK", "name": "恒生指数", "code": "HSI",
            "market": "hk", "asset_type": "index",
        },
        {
            "symbol": ".SPX.US", "name": "标普500", "code": ".SPX",
            "market": "us", "asset_type": "index",
        },
    ]))
    app = FastAPI()
    app.include_router(indices.router)
    app.state.repo = repo

    response = TestClient(app).get("/api/index/list?market=hk")

    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()["results"]] == ["HSI.HK"]


def test_sync_index_instruments_keeps_cross_market_core_catalog(monkeypatch) -> None:
    repo = InstrumentRepo()
    monkeypatch.setattr(index_sync, "_fetch_instruments_by_type", lambda *_args: pl.DataFrame())
    monkeypatch.setattr("app.tickflow.policy.detect_capabilities", lambda force=False: None)

    count = index_sync.sync_index_instruments(repo, pull_index=True, pull_etf=False)

    symbols = set(repo.saved.get_column("symbol").to_list())
    assert {"HSI.HK", "HSTECH.HK", ".SPX.US", ".IXIC.US", ".DJI.US", ".VIX.US"} <= symbols
    assert count == repo.saved.height
    assert repo.refreshed is True
