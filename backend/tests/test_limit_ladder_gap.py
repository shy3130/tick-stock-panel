from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.screener import router
from app.services.screener import gap_aware_consecutive


def test_gap_aware_consecutive_counts_boards_and_span():
    # 第 1 天涨停、第 2/3 天断板、第 4 天再涨停 → gap=2 时 4天2板
    days = [{"A": True}, {"A": False}, {"A": False}, {"A": True}]
    assert gap_aware_consecutive(days, 0)["A"] == (1, 1, 0)
    assert gap_aware_consecutive(days, 1)["A"] == (1, 1, 0)
    assert gap_aware_consecutive(days, 2)["A"] == (2, 4, 0)

    # 多次断板仍连成一条链
    days += [{"A": False}, {"A": False}, {"A": True}]
    assert gap_aware_consecutive(days, 2)["A"] == (3, 7, 0)

    # 断板/炸板当日无连板数, 但携带昨日链长 (用于 boards = prev + 1)
    assert gap_aware_consecutive([{"A": True}, {"A": True}, {"A": False}], 1)["A"] == (0, 0, 2)


@pytest.mark.parametrize("direction", ["up", "down"])
def test_limit_ladder_gap_regroups_and_labels(tmp_path, direction):
    as_of = date(2026, 11, 10)
    symbol = "000001.SZ"
    consec_col = "consecutive_limit_ups" if direction == "up" else "consecutive_limit_downs"

    # 三个历史交易日: 涨停 → 断 → 断; 当日再涨停 (严格口径为首板/首跌)
    enriched_dir = tmp_path / "kline_daily_enriched"
    for offset, value in [(3, 1), (2, 0), (1, 0)]:
        day_dir = enriched_dir / f"date={(as_of - timedelta(days=offset)).isoformat()}"
        day_dir.mkdir(parents=True)
        pl.DataFrame({
            "symbol": [symbol],
            "consecutive_limit_ups": [value],
            "consecutive_limit_downs": [value],
        }).write_parquet(day_dir / "part.parquet")

    snapshot = pl.DataFrame({
        "symbol": [symbol], "date": [as_of], "name": ["测试股"],
        "open": [11.0], "high": [11.0], "low": [11.0], "close": [11.0],
        "signal_limit_up": [direction == "up"],
        "signal_limit_down": [direction == "down"],
        "consecutive_limit_ups": [1 if direction == "up" else 0],
        "consecutive_limit_downs": [1 if direction == "down" else 0],
    })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest_asset=lambda asset: (snapshot, as_of),
        get_instruments_asset=lambda asset: pl.DataFrame(),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.depth_service = SimpleNamespace(
        get_sealed_map=lambda *args, **kwargs: {}, is_sealed_ready=lambda *args: False,
    )

    with TestClient(app) as client:
        strict = client.get("/api/screener/limit-ladder", params={
            "as_of": str(as_of), "direction": direction,
        })
        relaxed = client.get("/api/screener/limit-ladder", params={
            "as_of": str(as_of), "direction": direction, "gap": 2,
        })
        invalid = client.get("/api/screener/limit-ladder", params={
            "as_of": str(as_of), "direction": direction, "gap": 6,
        })

    assert strict.status_code == 200 and relaxed.status_code == 200
    assert invalid.status_code == 422
    strict_tier, = strict.json()["tiers"]
    relaxed_tier, = relaxed.json()["tiers"]
    assert strict_tier["boards"] == 1
    assert relaxed_tier["boards"] == 2
    stock, = relaxed_tier["stocks"]
    assert stock["span_days"] == 4
    assert stock[consec_col] == 2
    assert strict.json()["tiers"][0]["stocks"][0].get("span_days") == 0
