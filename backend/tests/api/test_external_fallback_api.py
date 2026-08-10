"""API-level tests for controlled external fallback wiring.

Covers:
  - GET /api/settings/preferences exposes external_fallback fields (defaults off/[])
  - PUT /api/settings/preferences/external-fallback validates scopes (400 on bad)
  - /api/intraday/indices adds degraded/sources/fallback_reason only on real fallback
  - /api/intraday/snapshot normalizes symbols, rejects over-limit, local-first
  - zero network when disabled / local fresh / non-trading day
All HTTP mocked.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.intraday as intraday_api
import app.services.external_fallback.adapter as fb_adapter
from app.services.external_fallback.adapter import FallbackReason


# ---- shared helpers --------------------------------------------------------

def _tencent_bytes(code: str = "sh600519") -> bytes:
    parts = ["0"] * 48
    parts[1] = "Test"; parts[2] = code[2:]
    parts[3] = "1193.01"; parts[4] = "1185.49"; parts[5] = "1180.10"; parts[6] = "42473"
    parts[30] = "20260807"; parts[31] = "103000"
    parts[33] = "1196.80"; parts[34] = "1166.33"; parts[37] = "503383"
    return (f'v_{code}="' + "~".join(parts) + '";').encode()


class _Resp:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass


def _client():
    app = FastAPI()
    app.include_router(intraday_api.router)
    app.state.quote_service = None
    app.state.repo = None
    return TestClient(app)


def _enable_fallback(monkeypatch, *, enabled=True, scopes=None, trading_day=True, http=None):
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled",
        lambda: enabled,
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes",
        lambda: scopes if scopes is not None else (["realtime"] if enabled else []),
    )
    if trading_day:
        monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 7))
        monkeypatch.setattr(fb_adapter, "_cn_today_iso", lambda: "2026-08-07")
    else:
        monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 9))
    # inject a mocked tencent source into the singleton adapter
    import time as _t
    from app.services.external_fallback.circuit import CircuitBreaker
    from app.services.external_fallback.sources.tencent_quote import TencentQuoteSource

    def getter(url, **kw):  # noqa: ARG001
        if http is not None:
            http.append(url)
        return _Resp(_tencent_bytes())

    src = TencentQuoteSource(
        circuit=CircuitBreaker(),
        clock=lambda: _t.monotonic(), sleeper=lambda _s: None, rng=lambda: 0.0,
        http_getter=getter,
    )
    from app.services.external_fallback.adapter import ExternalFallbackAdapter
    fb_adapter.reset_adapter(ExternalFallbackAdapter(tencent_source=src))


# ===========================================================================
# settings preferences
# ===========================================================================

class TestSettingsPreferences:
    def test_get_preferences_exposes_defaults(self, monkeypatch):
        from app.api.settings import get_preferences
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name",
            lambda capability=None: "fquant_local",
        )
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_enabled", lambda: False
        )
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_scopes", lambda: []
        )
        out = get_preferences()
        assert out["external_fallback_enabled"] is False
        assert out["external_fallback_scopes"] == []

    def test_put_external_fallback_rejects_invalid_scope(self):
        from app.api.settings import update_external_fallback, ExternalFallbackPrefsIn
        import pytest
        with pytest.raises(Exception) as ei:
            update_external_fallback(
                ExternalFallbackPrefsIn(external_fallback_enabled=True, external_fallback_scopes=["bogus"])
            )
        assert "400" in str(ei.value) or "invalid" in str(ei.value).lower()

    def test_put_external_fallback_accepts_realtime(self, monkeypatch):
        from app.api.settings import update_external_fallback, ExternalFallbackPrefsIn
        monkeypatch.setattr(
            "app.services.preferences.set_external_fallback",
            lambda enabled, scopes: (enabled, ["realtime"]),
        )
        out = update_external_fallback(
            ExternalFallbackPrefsIn(external_fallback_enabled=True, external_fallback_scopes=["realtime"])
        )
        assert out == {"external_fallback_enabled": True, "external_fallback_scopes": ["realtime"]}


# ===========================================================================
# intraday indices / snapshot
# ===========================================================================

class TestIntradayIndicesFallback:
    def test_disabled_zero_network_zero_meta(self, monkeypatch):
        # provider returns empty + daily empty → no local; fallback disabled
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(capabilities=SimpleNamespace(realtime=True),
                                         get_realtime=lambda **kw: pl.DataFrame()),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=False, http=http)
        resp = _client().get("/api/intraday/indices?symbols=600519.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert "degraded" not in body
        assert http == []

    def test_stale_local_triggers_fallback_meta(self, monkeypatch):
        # provider returns stale-dated row → fallback triggers
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(
                capabilities=SimpleNamespace(realtime=True),
                get_realtime=lambda **kw: pl.DataFrame([
                    {"symbol": "600519.SH", "date": "2026-08-01", "last_price": 1100.0,
                     "source": "provider_realtime"}
                ]),
            ),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=True, http=http)
        resp = _client().get("/api/intraday/indices?symbols=600519.SH")
        body = resp.json()
        assert body["source"] == "fallback_external"
        assert body["degraded"] is True
        assert body["sources"] == {"realtime": "tencent_quote"}
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_STALE.value
        assert len(http) == 1
        # each row carries tencent source
        assert body["rows"][0]["source"] == "tencent_quote"


class TestSnapshotEndpoint:
    def test_over_limit_rejected_normalized(self, monkeypatch):
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(capabilities=SimpleNamespace(realtime=True),
                                         get_realtime=lambda **kw: pl.DataFrame()),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=True, http=http)
        # 100 symbols but only 60 allowed
        syms = ",".join(f"{600000 + i}.SH" for i in range(100))
        resp = _client().get(f"/api/intraday/snapshot?symbols={syms}")
        assert resp.status_code == 200
        assert len(http) == 1  # capped to 60 → one batch

    def test_invalid_symbols_dropped(self, monkeypatch):
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(capabilities=SimpleNamespace(realtime=True),
                                         get_realtime=lambda **kw: pl.DataFrame()),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=True, http=http)
        resp = _client().get("/api/intraday/snapshot?symbols=AAPL.US,evil.com,600519.SH")
        body = resp.json()
        # only 600519.SH valid → one fetch
        assert len(http) == 1
        assert any(r["symbol"] == "600519.SH" for r in body["rows"])

    def test_non_trading_day_zero_network(self, monkeypatch):
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(capabilities=SimpleNamespace(realtime=True),
                                         get_realtime=lambda **kw: pl.DataFrame()),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=True, trading_day=False, http=http)
        resp = _client().get("/api/intraday/snapshot?symbols=600519.SH")
        assert resp.status_code == 200
        assert http == []
