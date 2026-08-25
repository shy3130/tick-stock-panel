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


def _enable_fallback(
    monkeypatch,
    *,
    enabled=True,
    scopes=None,
    trading_day=True,
    http=None,
    code="sh600519",
):
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
        return _Resp(_tencent_bytes(code))

    src = TencentQuoteSource(
        circuit=CircuitBreaker(),
        clock=lambda: _t.monotonic(), sleeper=lambda _s: None, rng=lambda: 0.0,
        http_getter=getter,
    )
    from app.services.external_fallback.adapter import ExternalFallbackAdapter
    fb_adapter.reset_adapter(ExternalFallbackAdapter(tencent_source=src))


def _bytes_for(code: str) -> bytes:
    """腾讯行情 bytes for a single exchange code (e.g. sh600519 / sz000002)."""
    parts = ["0"] * 48
    parts[1] = "Test"; parts[2] = code[2:]
    parts[3] = "10.00"; parts[4] = "9.50"; parts[5] = "9.80"; parts[6] = "1000"
    parts[30] = "20260807"; parts[31] = "103000"
    parts[33] = "10.20"; parts[34] = "9.40"; parts[37] = "10000"
    return (f'v_{code}="' + "~".join(parts) + '";').encode()


def _enable_fallback_multi(monkeypatch, *, http=None, enabled=True, cn_today="2026-08-07"):
    """Enable/Disable realtime fallback with a pinned CN trading day + per-code tencent mock.

    cn_today pins both _cn_today and _cn_today_iso so stock-row date freshness is deterministic.
    """
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled", lambda: enabled
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes",
        lambda: ["realtime"] if enabled else [],
    )
    y, m, d = (int(x) for x in cn_today.split("-"))
    monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(y, m, d))
    monkeypatch.setattr(fb_adapter, "_cn_today_iso", lambda: cn_today)
    import time as _t
    from app.services.external_fallback.circuit import CircuitBreaker
    from app.services.external_fallback.sources.tencent_quote import TencentQuoteSource

    def getter(url, **kw):  # noqa: ARG001
        codes = [c for c in url.split("q=", 1)[-1].split(",") if c]
        if http is not None:
            http.append(url)
        return _Resp(b"".join(_bytes_for(c) for c in codes))

    src = TencentQuoteSource(
        circuit=CircuitBreaker(),
        clock=lambda: _t.monotonic(), sleeper=lambda _s: None, rng=lambda: 0.0,
        http_getter=getter,
    )
    from app.services.external_fallback.adapter import ExternalFallbackAdapter
    fb_adapter.reset_adapter(ExternalFallbackAdapter(tencent_source=src))


def _client_with_qs(quote_service):
    """TestClient wired with a (mocked) global QuoteService on app.state."""
    app = FastAPI()
    app.include_router(intraday_api.router)
    app.state.quote_service = quote_service
    app.state.repo = None
    return TestClient(app)


def _mock_quote_service(
    *, stock_df=None, index_df=None, stock_calls=None, index_calls=None,
    has_recent_data=True,
):
    """QuoteService double exposing get_quotes_compat / get_index_quotes / status."""
    def _gqc():
        if stock_calls is not None:
            stock_calls.append(True)
        return stock_df if stock_df is not None else pl.DataFrame()

    def _giq(symbols=None):  # noqa: ARG001
        if index_calls is not None:
            index_calls.append(list(symbols) if symbols else None)
        return index_df if index_df is not None else pl.DataFrame()

    def _status():
        return {"has_recent_data": has_recent_data}

    return SimpleNamespace(
        get_quotes_compat=_gqc, get_index_quotes=_giq, status=_status,
    )


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
        # provider returns a stale canonical index row → fallback triggers
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local"
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(
                capabilities=SimpleNamespace(realtime=True),
                get_realtime=lambda **kw: pl.DataFrame([
                    {"symbol": "000001.INDEX", "date": "2026-08-01", "last_price": 3100.0,
                     "source": "provider_realtime"}
                ]),
            ),
        )
        http: list = []
        _enable_fallback(monkeypatch, enabled=True, http=http, code="sh000001")
        resp = _client().get("/api/intraday/indices?symbols=000001.SH")
        body = resp.json()
        assert body["source"] == "fallback_external"
        assert body["degraded"] is True
        assert body["sources"] == {"realtime": "tencent_quote"}
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_STALE.value
        assert len(http) == 1
        # each row carries tencent source
        assert body["rows"][0]["source"] == "tencent_quote"

    def test_no_param_stale_core_indices_trigger_fallback(self, monkeypatch):
        """回归: 无参调用 (核心指数默认视图) 本地缓存陈旧时也必须走 fallback。

        旧实现 symbols 为空 → norm_symbols=[] → resolver 短路, 周五数据以
        source=realtime 且不带 degraded 标记返回。
        """
        stale_index_df = pl.DataFrame([
            {"symbol": "000001.INDEX", "last_price": 3100.0, "close": 3100.0,
             "date": "2026-08-06", "timestamp": "2026-08-06T15:00:00+08:00"},
        ])
        qs = _mock_quote_service(index_df=stale_index_df)
        http: list = []
        _enable_fallback_multi(monkeypatch, enabled=True, http=http, cn_today="2026-08-07")
        resp = _client_with_qs(qs).get("/api/intraday/indices")
        body = resp.json()
        assert body["source"] == "fallback_external"
        assert body["degraded"] is True
        assert body["sources"] == {"realtime": "tencent_quote"}
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_STALE.value
        assert len(http) == 1
        assert body["rows"][0]["source"] == "tencent_quote"

    def test_no_param_fresh_core_indices_zero_network(self, monkeypatch):
        """无参调用本地缓存为当日数据时, 不触发外部网络且无 degraded 标记。"""
        fresh_index_df = pl.DataFrame([
            {"symbol": "000001.INDEX", "last_price": 3100.0, "close": 3100.0,
             "date": "2026-08-07", "timestamp": "2026-08-07T10:30:00+08:00"},
        ])
        qs = _mock_quote_service(index_df=fresh_index_df)
        http: list = []
        _enable_fallback_multi(monkeypatch, enabled=True, http=http, cn_today="2026-08-07")
        resp = _client_with_qs(qs).get("/api/intraday/indices")
        body = resp.json()
        assert body["source"] == "realtime"
        assert "degraded" not in body
        assert http == []


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


# ===========================================================================
# snapshot: stock cache reuse (QuoteService.get_quotes_compat)
# ===========================================================================

class TestSnapshotStockCacheReuse:
    """股票 snapshot 必须先读 QuoteService 股票缓存 (get_quotes_compat)。

    resolver 契约 (本地优先, 按 symbol 分新鲜度):
      - 本地当日命中行 → fresh, 零网络, 原样保留 (source=realtime)
      - 缺失/陈旧 symbol + 对应 stale 行 → 交给 resolver:
        开启则外部替换并标 degraded (source=fallback_external);
        关闭则保留 stale 行 (source=local_disk, 绝不叫 realtime)
      - 混合请求 (A fresh + B missing/stale) → 只请求 B, A+B 均返回
      - 指数 symbol → 仍走 get_index_quotes 缓存路径 (不回归)
    """

    @staticmethod
    def _patch_index_provider(monkeypatch, realtime_df=None):
        """指数 provider realtime 钩子 (仅指数路径; 股票不应触达)。"""
        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name",
            lambda capability=None: "fquant_local",
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(
                capabilities=SimpleNamespace(realtime=True),
                get_realtime=lambda **kw: realtime_df if realtime_df is not None else pl.DataFrame(),
            ),
        )

    def test_fresh_cache_zero_network_realtime(self, monkeypatch):
        """本地股票缓存当日命中 → provider 抛错钩子不触发, 外部源零网络, source=realtime。"""
        provider_calls: list = []

        def _boom(**kw):
            provider_calls.append(kw)
            raise AssertionError("realtime provider must not be called when stocks cached fresh")

        monkeypatch.setattr(
            "app.data_providers.registry.get_active_provider_name",
            lambda capability=None: "fquant_local",
        )
        monkeypatch.setattr(
            "app.data_providers.registry.get_provider",
            lambda name: SimpleNamespace(
                capabilities=SimpleNamespace(realtime=True), get_realtime=_boom,
            ),
        )
        # 缓存行 date == 注入的当日 (fresh)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1193.01, "last_price": 1193.01,
             "prev_close": 1185.49, "change_pct": 0.64, "date": "2026-08-07"},
            {"symbol": "000001.SZ", "close": 11.08, "last_price": 11.08,
             "prev_close": 11.00, "change_pct": 0.73, "date": "2026-08-07"},
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH,000001.SZ")
        assert resp.status_code == 200
        body = resp.json()
        assert provider_calls == []           # realtime provider never invoked
        assert http == []                     # external source zero network
        assert "degraded" not in body         # no fallback provenance
        assert body["source"] == "realtime"
        assert {r["symbol"] for r in body["rows"]} == {"600519.SH", "000001.SZ"}
        # 本地行 provenance = realtime (当日)
        assert all(r["source"] == "realtime" for r in body["rows"])

    def test_stale_cache_external_enabled_replaces_and_degrades(self, monkeypatch):
        """本地缓存陈旧 (date < 当日) + 外部开启 → 外部替换并标 degraded。"""
        self._patch_index_provider(monkeypatch)
        # 缓存行 date = 08-01, 当日注入 = 08-07 → 陈旧
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1100.0, "last_price": 1100.0,
             "prev_close": 1090.0, "date": "2026-08-01"},
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=True, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert len(http) == 1                  # external triggered (stale)
        assert "sh600519" in http[0]
        assert body["degraded"] is True
        assert body["source"] == "fallback_external"
        assert body["sources"] == {"realtime": "tencent_quote"}
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_STALE.value
        # 外部行替换本地陈旧行
        assert {r["symbol"] for r in body["rows"]} == {"600519.SH"}
        assert all(r["source"] == "tencent_quote" for r in body["rows"])

    def test_stale_cache_external_disabled_returns_local_disk(self, monkeypatch):
        """本地缓存陈旧 + 外部关闭 → 保留本地行, source=local_disk (绝不叫 realtime)。"""
        self._patch_index_provider(monkeypatch)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1100.0, "last_price": 1100.0,
             "prev_close": 1090.0, "date": "2026-08-01"},
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=False, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert http == []                      # external disabled → zero network
        assert "degraded" not in body
        assert {r["symbol"] for r in body["rows"]} == {"600519.SH"}
        # 昨日数据被正确标为 local_disk
        assert all(r["source"] == "local_disk" for r in body["rows"])
        assert body["source"] == "local_disk"

    def test_all_missing_external_enabled_fills_and_degrades(self, monkeypatch):
        """缓存全缺失 (无行) + 外部开启 → 外部补齐并标 degraded (missing)。"""
        self._patch_index_provider(monkeypatch)
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=True, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service())  # empty stock + index cache
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert body["degraded"] is True
        assert body["source"] == "fallback_external"
        assert body["sources"] == {"realtime": "tencent_quote"}
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_MISSING.value
        assert {r["symbol"] for r in body["rows"]} == {"600519.SH"}
        assert len(http) == 1

    def test_index_symbol_uses_index_cache_no_regression(self, monkeypatch):
        """指数 symbol 仍走 get_index_quotes 缓存; get_quotes_compat 不被调用。"""
        self._patch_index_provider(monkeypatch)
        index_df = pl.DataFrame([
            {"symbol": "000001.INDEX", "last_price": 3193.0, "close": 3193.0,
             "prev_close": 3180.0, "change_pct": 0.41, "date": "2026-08-07"},
        ])
        http: list = []
        stock_calls: list = []
        index_calls: list = []
        _enable_fallback_multi(monkeypatch, http=http, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(
            index_df=index_df, stock_calls=stock_calls, index_calls=index_calls,
        ))
        resp = client.get("/api/intraday/snapshot?symbols=000001.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert stock_calls == []                            # stock cache not consulted for an index
        assert ["000001.INDEX"] in index_calls          # index cache queried with canonical symbol
        assert {r["symbol"] for r in body["rows"]} == {"000001.INDEX"}
        assert "degraded" not in body                       # index cache fresh → no external
        assert http == []

    def test_new_index_suffix_classified_as_index(self, monkeypatch):
        """.INDEX 后缀 symbol 也走指数缓存路径 (与 _is_index_record 口径一致)。"""
        self._patch_index_provider(monkeypatch)
        index_df = pl.DataFrame([
            {"symbol": "000300.INDEX", "last_price": 4001.0, "close": 4001.0,
             "prev_close": 3980.0, "date": "2026-08-07"},
        ])
        http: list = []
        stock_calls: list = []
        _enable_fallback_multi(monkeypatch, http=http, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(
            index_df=index_df, stock_calls=stock_calls,
        ))
        resp = client.get("/api/intraday/snapshot?symbols=000300.INDEX")
        assert resp.status_code == 200
        body = resp.json()
        assert stock_calls == []                            # never routed to stock cache
        assert {r["symbol"] for r in body["rows"]} == {"000300.INDEX"}
        assert "degraded" not in body

    def test_mixed_stock_and_index_both_fresh_cached(self, monkeypatch):
        """股票 + 指数混合请求且均当日命中缓存 → 两者都返回, 无网络, source=realtime。"""
        self._patch_index_provider(monkeypatch)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1193.01, "last_price": 1193.01,
             "prev_close": 1185.49, "date": "2026-08-07"},
        ])
        index_df = pl.DataFrame([
            {"symbol": "000001.INDEX", "last_price": 3193.0, "close": 3193.0,
             "prev_close": 3180.0, "date": "2026-08-07"},
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df, index_df=index_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH,000001.SH")
        assert resp.status_code == 200
        body = resp.json()
        assert {r["symbol"] for r in body["rows"]} == {"600519.SH", "000001.INDEX"}
        assert http == []
        assert "degraded" not in body
        assert body["source"] == "realtime"

    def test_partial_fresh_hit_only_missing_fetched_both_returned(self, monkeypatch):
        """混合请求: 600519 当日 fresh + 000002 缺失 → 只请求 000002, 两者都返回。

        旧实现 (整批 fresh 判定) 会在 600519 fresh 时零网络且丢 000002; per-symbol
        分区后只把 000002 交给 resolver, fresh 行保留。
        """
        self._patch_index_provider(monkeypatch)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1193.01, "last_price": 1193.01,
             "prev_close": 1185.49, "date": "2026-08-07"},  # fresh (== 当日)
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=True, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH,000002.SZ")
        assert resp.status_code == 200
        body = resp.json()
        # 恰好一次网络 —— 只为缺失的 000002.SZ (fresh 的 600519 不进 resolver)
        assert len(http) == 1
        assert "sz000002" in http[0]
        assert "sh600519" not in http[0]
        syms = {r["symbol"] for r in body["rows"]}
        assert syms == {"600519.SH", "000002.SZ"}  # 两者都返回, 不丢 B
        # provenance: fresh 行 realtime, 外部补的行 tencent_quote; 整体 degraded
        by_sym = {r["symbol"]: r for r in body["rows"]}
        assert by_sym["600519.SH"]["source"] == "realtime"
        assert by_sym["000002.SZ"]["source"] == "tencent_quote"
        assert body["degraded"] is True
        assert body["source"] == "fallback_external"
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_MISSING.value

    def test_partial_fresh_with_stale_only_stale_to_resolver(self, monkeypatch):
        """混合请求: 600519 fresh + 000002 stale → 只把 000002 (stale) 交给 resolver。"""
        self._patch_index_provider(monkeypatch)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1193.01, "last_price": 1193.01,
             "prev_close": 1185.49, "date": "2026-08-07"},  # fresh
            {"symbol": "000002.SZ", "close": 30.0, "last_price": 30.0,
             "prev_close": 29.5, "date": "2026-08-01"},      # stale
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=True, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH,000002.SZ")
        assert resp.status_code == 200
        body = resp.json()
        assert len(http) == 1
        assert "sz000002" in http[0]
        assert "sh600519" not in http[0]
        syms = {r["symbol"] for r in body["rows"]}
        assert syms == {"600519.SH", "000002.SZ"}
        by_sym = {r["symbol"]: r for r in body["rows"]}
        assert by_sym["600519.SH"]["source"] == "realtime"      # fresh 本地
        assert by_sym["000002.SZ"]["source"] == "tencent_quote"  # 外部替换 stale
        assert body["degraded"] is True
        assert body["fallback_reason"] == FallbackReason.LOCAL_SNAPSHOT_STALE.value

    def test_partial_fresh_external_disabled_keeps_fresh_and_stale(self, monkeypatch):
        """混合请求 + 外部关闭: fresh 行 realtime, stale 行 local_disk, 零网络。"""
        self._patch_index_provider(monkeypatch)
        stock_df = pl.DataFrame([
            {"symbol": "600519.SH", "close": 1193.01, "last_price": 1193.01,
             "prev_close": 1185.49, "date": "2026-08-07"},  # fresh
            {"symbol": "000002.SZ", "close": 30.0, "last_price": 30.0,
             "prev_close": 29.5, "date": "2026-08-01"},      # stale
        ])
        http: list = []
        _enable_fallback_multi(monkeypatch, http=http, enabled=False, cn_today="2026-08-07")
        client = _client_with_qs(_mock_quote_service(stock_df=stock_df))
        resp = client.get("/api/intraday/snapshot?symbols=600519.SH,000002.SZ")
        assert resp.status_code == 200
        body = resp.json()
        assert http == []                          # 外部关闭零网络
        assert "degraded" not in body
        syms = {r["symbol"] for r in body["rows"]}
        assert syms == {"600519.SH", "000002.SZ"}  # 两者都返回
        by_sym = {r["symbol"]: r for r in body["rows"]}
        assert by_sym["600519.SH"]["source"] == "realtime"     # fresh 当日
        assert by_sym["000002.SZ"]["source"] == "local_disk"   # stale 昨日
        assert body["source"] == "local_disk"
