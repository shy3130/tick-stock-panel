from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router
from app.data_providers import registry
from app.services.mtf_direction_15m5m import MinuteBar, SessionSpec, clear_registered_minute_reader, register_minute_reader


class _Reader:
    def __init__(self, events=None):
        self.days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        self.events = events if events is not None else []

    def catalog_manifest(self): return {"generation": "api-generation"}
    def manifest_sha256(self): return "b" * 64
    def generation(self): return "api-generation"
    def market_days(self, start, end): return [d for d in self.days if start <= d <= end]
    def session(self, symbol, day): return SessionSpec(symbol, day, time(9, 30), time(15, 0))
    def minute_bars(self, symbol, day):
        stamps = []
        cur = datetime.combine(day, time(9, 31))
        while cur.time() <= time(11, 30): stamps.append(cur); cur += timedelta(minutes=1)
        cur = datetime.combine(day, time(13, 1))
        while cur.time() <= time(15, 0): stamps.append(cur); cur += timedelta(minutes=1)
        out = []
        prev = 10.0
        for i, ts in enumerate(stamps):
            close = prev + (0.01 if i % 2 else -0.005)
            out.append(MinuteBar(symbol, ts, prev, max(prev, close) + .01, min(prev, close) - .01, close, 1.0))
            prev = close
        return out
    def sealed_cutoff(self): return datetime(2026, 1, 5, 15, 0)
    def close(self): self.events.append("reader_close")


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _body():
    return {"start": "2026-01-01", "end": "2026-01-05", "oos_start": "2026-01-03", "symbols": ["600000.SH"]}


def test_api_requires_oos_start_and_registered_reader_is_caller_owned():
    reader = _Reader()
    register_minute_reader(reader)
    try:
        client = _client()
        missing = client.post("/api/research/factors/mtf-direction/evaluate", json={k: v for k, v in _body().items() if k != "oos_start"})
        assert missing.status_code == 422
        response = client.post("/api/research/factors/mtf-direction/evaluate", json=_body())
        assert response.status_code == 200
        assert response.json()["provenance"]["manifest_sha256"] == "b" * 64
        assert reader.events == []
    finally:
        clear_registered_minute_reader()


def test_api_owned_reader_then_provider_close(monkeypatch):
    events = []
    reader = _Reader(events)
    provider = SimpleNamespace(
        capabilities=SimpleNamespace(ordered_trans_research=True),
        open_ordered_trans_reader=lambda: reader,
        close=lambda: events.append("provider_close"),
    )
    monkeypatch.setattr(registry, "get_active_provider_name", lambda capability=None: "fake")
    monkeypatch.setattr(registry, "get_provider", lambda name: provider)
    response = _client().post("/api/research/factors/mtf-direction/evaluate", json=_body())
    assert response.status_code == 200
    assert events == ["reader_close", "provider_close"]
