from datetime import date
from decimal import Decimal

import pytest

from app.custom.holdings.ledger import Trade, load, replay, save

TODAY = date(2026, 11, 17)


def trade(id, side="buy", qty=100, price="10", day=1, **kw):
    return Trade(id=id, symbol="600000.SH", traded_at=f"2026-11-{day:02d}T10:00:00", side=side,
                 qty=qty, price=Decimal(price), **kw)


def test_average_partial_close_and_reopen():
    trades = [trade("a"), trade("b", price="12", day=2), trade("c", "sell", price="13", day=3)]
    p = replay(trades, TODAY)["positions"][0]
    assert (p["qty"], p["avg_cost"], p["realized"], p["holding_days"]) == (100, 11, 200, 16)
    trades += [trade("d", "sell", price="9", day=4), trade("e", day=5)]
    positions = replay(trades, TODAY)["positions"]
    assert len(positions) == 2
    assert positions[1]["realized"] == 0
    assert positions[1]["closed"] == "2026-11-04"
    assert positions[0]["id"] == "e"


def test_fees_and_history_validation(tmp_path):
    trades = [trade("a", fee=Decimal("5")), trade("b", "sell", price="11", day=2, fee=Decimal("6"))]
    save(tmp_path, trades, TODAY)
    assert replay(load(tmp_path), TODAY)["positions"][0]["realized"] == 89
    with pytest.raises(ValueError, match="卖出超过"):
        save(tmp_path, [trades[0].model_copy(update={"qty": 50}), trades[1]], TODAY)
    assert load(tmp_path)[0].qty == 100
    with pytest.raises(ValueError, match="卖出超过"):
        replay([trades[0].model_copy(update={"voided": True}), trades[1]], TODAY)
    with pytest.raises(ValueError, match="重复"):
        replay([trades[0], trades[0]], TODAY)


def test_corruption_and_future_rejected(tmp_path):
    with pytest.raises(ValueError, match="未来"):
        replay([trade("a", day=18)], TODAY)
    p = tmp_path / "user_data/holdings/ledger.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"version": 2, "trades": []}')
    with pytest.raises(ValueError, match="版本"):
        load(tmp_path)


def test_api_revision_idempotency_restore_and_raw_quotes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import polars as pl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom.holdings import routes

    monkeypatch.setattr(routes, "cn_today", lambda: TODAY)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           resolve_asset_type=lambda s: "stock",
                           get_name_map=lambda symbols: {s: "测试" for s in symbols},
                           get_enriched_latest=lambda **kw: (pl.DataFrame({"symbol": ["600000.SH"], "raw_close": [12.0], "close": [6.0], "date": [TODAY]}), TODAY))
    app = FastAPI()
    app.state.repo = repo
    app.include_router(routes.router)
    client = TestClient(app)
    base = "/api/custom/holdings"
    initial = client.get(base).json()
    payload = {"revision": initial["revision"], "trade": trade("a").model_dump(mode="json")}
    assert client.put(base + "/trade", json=payload).status_code == 200
    assert client.put(base + "/trade", json=payload).status_code == 200
    state = client.get(base).json()
    assert state["positions"][0]["unrealized"] == 200
    assert state["positions"][0]["last_price"] == 12
    assert state["trades"][0]["order"] == 1
    correction = {**state["trades"][0], "order": 999}
    assert client.put(base + "/trade", json={"revision": state["revision"], "trade": correction}).status_code == 200
    assert client.get(base).json()["trades"][0]["order"] == 1
    payload["trade"]["qty"] = 200
    assert client.put(base + "/trade", json=payload).status_code == 409
    assert client.post(base + "/restore", json={"version": 2, "revision": state["revision"], "trades": []}).status_code == 400
    assert client.post(base + "/restore", json={"version": 1, "revision": state["revision"], "trades": [trade("s", "sell").model_dump(mode="json")]}).status_code == 400
    assert len(client.get(base).json()["trades"]) == 1
    assert client.post(base + "/restore", json={"version": 1, "revision": state["revision"], "trades": []}).status_code == 200
    assert client.get(base).json()["positions"] == []


def test_market_chips_shared_model():
    import polars as pl

    from app.indicators.levels import _support_resistance, compute_chip_distribution

    df = pl.DataFrame({"high": [12.0] * 25, "low": [10.0] * 25,
                       "close": [11.0] * 25, "volume": [1000.0] * 25,
                       "turnover_rate": [5.0] * 25})
    before = _support_resistance(df)
    bins = compute_chip_distribution(df)
    assert len(bins) == 40
    assert sum(b["weight"] for b in bins) == pytest.approx(1)
    assert all(10 <= b["price"] <= 12 and b["weight"] >= 0 for b in bins)
    assert _support_resistance(df) == before


def test_chip_api_missing_turnover_and_bad_data(tmp_path):
    from types import SimpleNamespace

    import polars as pl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom.holdings.routes import router

    frame = pl.DataFrame({"date": [TODAY] * 25, "high": [12.0] * 25, "low": [10.0] * 25,
                          "close": [11.0] * 25, "volume": [1000.0] * 25})
    app = FastAPI()
    app.state.repo = SimpleNamespace(get_daily_asset=lambda *a: frame)
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/api/custom/holdings/chips?symbol=600000.SH').json()["bins"] == []
    frame = frame.with_columns(pl.lit(5.0).alias("turnover_rate"))
    assert len(client.get('/api/custom/holdings/chips?symbol=600000.SH').json()["bins"]) == 40
    frame = frame.with_columns(pl.lit(float('nan')).alias("turnover_rate"))
    assert client.get('/api/custom/holdings/chips?symbol=600000.SH').json()["bins"] == []


def test_same_date_uses_entry_sequence_not_clock_time():
    buy = trade("z", day=2).model_copy(update={"order": 1})
    sell = trade("a", "sell", day=2).model_copy(update={"order": 2})
    buy = buy.model_copy(update={"traded_at": buy.traded_at.replace(hour=15)})
    sell = sell.model_copy(update={"traded_at": sell.traded_at.replace(hour=9)})
    assert replay([sell, buy], TODAY)["positions"][0]["closed"] == "2026-11-02"
