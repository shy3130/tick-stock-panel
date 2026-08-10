"""Controlled external fallback boundary tests (P1: realtime).

All HTTP is mocked — no real network. Covers:
  - preferences gate (off / missing scope / on)
  - trading-day gate (weekend → zero network)
  - local fresh snapshot → zero network
  - stale/missing local + enabled → tencent_quote rows + degraded + reason
  - invalid / over-limit symbols rejected
  - symbol/unit/timezone calibration
  - circuit breaker + cache + single-flight
  - no write-path contamination (adapter holds no repo/QuoteService handle)
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.services.external_fallback import adapter as fb_adapter
from app.services.external_fallback.adapter import (
    ExternalFallbackAdapter,
    FallbackReason,
)
from app.services.external_fallback.calibration import validate_row
from app.services.external_fallback.circuit import CircuitBreaker
from app.services.external_fallback.sources import tencent_quote
from app.services.external_fallback.sources.tencent_quote import (
    TencentQuoteSource,
    _parse_tencent,
    decode_response,
    is_supported,
    to_exch_code,
    to_symbol,
)


def _tencent_response(symbol_code: str = "sh600519") -> bytes:
    """Build a minimal valid tencent payload (GBK-safe ASCII) for one symbol.

    Field layout matches _parse_tencent indices.
    """
    parts = ["0"] * 48
    parts[1] = "TestStock"
    parts[2] = symbol_code[2:]
    parts[3] = "1193.01"   # last
    parts[4] = "1185.49"   # prev close
    parts[5] = "1180.10"   # open
    parts[6] = "42473"     # vol (手)
    parts[30] = "20260807"  # date
    parts[31] = "103000"   # time
    parts[33] = "1196.80"  # high
    parts[34] = "1166.33"  # low
    parts[37] = "503383"   # amount (万元)
    return (f'v_{symbol_code}="' + "~".join(parts) + '";').encode()


def _fake_response(content: bytes, status: int = 200) -> httpx.Response:
    """Build an httpx.Response with given bytes content."""
    return httpx.Response(status_code=status, content=content, request=httpx.Request("GET", "https://qt.gtimg.cn/q=x"))


def _make_adapter(calls: list, *, responses=None) -> ExternalFallbackAdapter:
    """Build an adapter with a tencent source whose HTTP is mocked.

    calls: list that records each http_getter invocation.
    responses: optional [content, ...] to cycle; default single 200 response.
    clock/sleeper/rng are no-ops so tests run without real delays.
    """
    resp_iter = iter(responses) if responses else None

    def http_getter(url, **kw):  # noqa: ARG001
        calls.append(url)
        # enforce trust_env=False invariant
        assert kw.get("trust_env") is False, "trust_env must be False"
        assert kw.get("timeout", 0) <= 5.0, "timeout must be <= 5s"
        if resp_iter is not None:
            content = next(resp_iter)
        else:
            content = _tencent_response()
        return _fake_response(content)

    source = TencentQuoteSource(
        circuit=CircuitBreaker(),
        clock=lambda: time.monotonic(),
        sleeper=lambda _s: None,
        rng=lambda: 0.0,
        http_getter=http_getter,
    )
    return ExternalFallbackAdapter(tencent_source=source)


@pytest.fixture(autouse=True)
def _reset_adapter_singleton():
    fb_adapter.reset_adapter()
    yield
    fb_adapter.reset_adapter()


@pytest.fixture
def enabled_realtime(monkeypatch):
    """Enable external fallback with realtime scope + freeze trading day."""
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled", lambda: True
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes",
        lambda: ["realtime"],
    )
    # Force a weekday trading day
    monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 7))  # Friday
    monkeypatch.setattr(fb_adapter, "_cn_today_iso", lambda: "2026-08-07")


# ===========================================================================
# 1. Gate: disabled / missing scope → zero network
# ===========================================================================

class TestGateNoNetwork:
    def test_disabled_means_zero_network(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_enabled", lambda: False
        )
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_scopes", lambda: ["realtime"]
        )
        calls: list = []
        adapter = _make_adapter(calls)
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert result.used_fallback is False
        assert calls == []

    def test_enabled_but_scope_missing_zero_network(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_enabled", lambda: True
        )
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_scopes", lambda: []
        )
        calls: list = []
        adapter = _make_adapter(calls)
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert result.used_fallback is False
        assert calls == []

    def test_non_trading_day_zero_network(self, monkeypatch, enabled_realtime):
        # Override to Sunday
        monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 9))  # Sunday
        calls: list = []
        adapter = _make_adapter(calls)
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert result.used_fallback is False
        assert calls == []


# ===========================================================================
# 2. Local fresh → zero network
# ===========================================================================

class TestLocalFresh:
    def test_local_current_day_zero_network(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        # local rows with today's date → fresh
        local = [{"symbol": "600519.SH", "timestamp": "2026-08-07T15:00:00+08:00", "last_price": 1193.0}]
        result = adapter.resolve_realtime(["600519.SH"], local_rows=local)
        assert result.used_fallback is False
        assert calls == []

    def test_local_date_field_treated_as_fresh(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        local = [{"symbol": "600519.SH", "date": "2026-08-07", "last_price": 1193.0}]
        result = adapter.resolve_realtime(["600519.SH"], local_rows=local)
        assert result.used_fallback is False
        assert calls == []


# ===========================================================================
# 3. Stale / missing + enabled → tencent_quote + degraded + reason
# ===========================================================================

class TestFallbackTriggered:
    def test_missing_local_triggers_fallback(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert result.used_fallback is True
        assert result.source == "tencent_quote"
        assert result.reason == FallbackReason.LOCAL_SNAPSHOT_MISSING
        assert len(calls) == 1
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row["source"] == "tencent_quote"
        assert row["symbol"] == "600519.SH"
        assert "+08:00" in row["timestamp"]

    def test_stale_local_triggers_fallback(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        stale = [{"symbol": "600519.SH", "date": "2026-08-01", "last_price": 1100.0}]
        result = adapter.resolve_realtime(["600519.SH"], local_rows=stale)
        assert result.used_fallback is True
        assert result.reason == FallbackReason.LOCAL_SNAPSHOT_STALE

    def test_change_pct_is_percentage_points(self, enabled_realtime):
        adapter = _make_adapter([])
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        row = result.rows[0]
        # 1193.01 vs 1185.49 → ~0.634%
        assert abs(row["change_pct"] - (1193.01 - 1185.49) / 1185.49 * 100) < 0.01

    def test_volume_is_shares(self, enabled_realtime):
        adapter = _make_adapter([])
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        row = result.rows[0]
        # tencent vol field is 手 → ×100
        assert row["volume"] == 42473 * 100

    def test_amount_is_yuan(self, enabled_realtime):
        adapter = _make_adapter([])
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        row = result.rows[0]
        # tencent amount field is 万元 → ×10000
        assert row["amount"] == 503383 * 10000


# ===========================================================================
# 4. Invalid / over-limit symbols rejected
# ===========================================================================

class TestSymbolValidation:
    def test_unsupported_symbol_filtered(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        # US stock suffix unsupported
        result = adapter.resolve_realtime(["AAPL.US"], local_rows=[])
        assert result.used_fallback is False
        assert calls == []

    def test_over_limit_capped_at_60(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        many = [f"{600000 + i}.SH" for i in range(100)]
        result = adapter.resolve_realtime(many, local_rows=[])
        assert result.used_fallback is True
        # exactly 60 symbols sent (one batch of 60 → one HTTP call)
        assert len(calls) == 1


# ===========================================================================
# 5. Calibration (symbol/unit/timezone)
# ===========================================================================

class TestCalibration:
    def test_valid_row_passes(self):
        row = {
            "symbol": "600519.SH", "last_price": 1193.01, "prev_close": 1185.49,
            "volume": 100, "amount": 1000.0, "change_pct": 0.6,
            "timestamp": "2026-08-07T10:30:00+08:00", "source": "tencent_quote",
        }
        assert validate_row(row) is True

    @pytest.mark.parametrize("bad", [
        {"symbol": "xxx"},  # bad symbol
        {"symbol": "600519.SH", "last_price": None},  # no price
        {"symbol": "600519.SH", "last_price": 10.0, "volume": -1},  # neg volume
        {"symbol": "600519.SH", "last_price": 10.0, "timestamp": "2026-08-07"},  # no tz
    ])
    def test_invalid_rows_rejected(self, bad):
        full = {"symbol": "600519.SH", "last_price": 10.0,
                "timestamp": "2026-08-07T10:30:00+08:00"}
        full.update(bad)
        assert validate_row(full) is False

    def test_symbol_mapping_roundtrip(self):
        assert to_exch_code("600519.SH") == "sh600519"
        assert to_symbol("sh600519") == "600519.SH"
        assert to_exch_code("00700.HK") == "hk00700"
        assert to_symbol("hk00700") == "00700.HK"
        assert to_exch_code("BAD") is None
        assert is_supported("600519.SH") is True
        assert is_supported("AAPL.US") is False

    def test_decode_gbk_and_utf8(self):
        assert decode_response("hello".encode("utf-8")) == "hello"
        # GBK chinese
        assert decode_response("茅台".encode("gbk")) == "茅台"
        # latin-1 fallback never raises
        assert isinstance(decode_response(b"\xff\xfe"), str)

    def test_session_marker_is_true_only_outside_symbol_market_hours(self):
        tz = timezone(timedelta(hours=8))
        open_rows = _parse_tencent(
            _tencent_response().decode(),
            now=datetime(2026, 8, 7, 10, 30, tzinfo=tz),
        )
        closed_rows = _parse_tencent(
            _tencent_response().decode(),
            now=datetime(2026, 8, 7, 20, 0, tzinfo=tz),
        )

        assert open_rows[0]["stale_session"] is False
        assert closed_rows[0]["stale_session"] is True


# ===========================================================================
# 6. Circuit breaker + cache + single-flight
# ===========================================================================

class TestReliability:
    def test_circuit_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60, clock=lambda: 100.0)
        assert cb.source_available("tencent_quote") is True
        cb.record_failure("tencent_quote")
        cb.record_failure("tencent_quote")
        assert cb.source_available("tencent_quote") is True  # 2 < 3
        cb.record_failure("tencent_quote")
        assert cb.source_available("tencent_quote") is False  # opened

    def test_circuit_recovers_after_cooldown(self):
        t = [100.0]
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60, clock=lambda: t[0])
        cb.record_failure("x"); cb.record_failure("x")
        assert cb.source_available("x") is False
        t[0] = 200.0  # past cooldown
        assert cb.source_available("x") is True
        cb.record_success("x")
        assert cb.source_available("x") is True

    def test_open_circuit_cooldown_is_not_extended(self):
        now = [100.0]
        cb = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=60,
            clock=lambda: now[0],
        )
        cb.force_open("tencent_quote")
        now[0] = 110.0
        cb.force_open("tencent_quote")

        now[0] = 159.9
        assert cb.source_available("tencent_quote") is False
        now[0] = 160.0
        assert cb.source_available("tencent_quote") is True

    def test_cache_avoids_second_fetch(self, enabled_realtime):
        calls: list = []
        adapter = _make_adapter(calls)
        # first call fetches
        adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert len(calls) == 1
        # second call within cache TTL reuses → no new fetch
        adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert len(calls) == 1

    def test_production_client_uses_its_fixed_proxy_policy(self, monkeypatch):
        """生产 Client 已固定 trust_env=False, 调用时不得再传 Client.get 不支持的参数。"""
        source = TencentQuoteSource()
        calls: list[dict] = []

        def fake_get(_url, **kwargs):
            calls.append(kwargs)
            return _fake_response(_tencent_response())

        monkeypatch.setattr(source._client, "get", fake_get)
        try:
            rows = source.get_realtime(["600519.SH"])
        finally:
            source._client.close()

        assert rows and rows[0]["symbol"] == "600519.SH"
        assert calls == [{"timeout": 5.0}]

    def test_network_failures_are_not_misclassified_as_calibration_failures(
        self, enabled_realtime
    ):
        """网络失败由源的熔断器单独计数, 不得被 adapter 重复计成口径失败。"""
        calls: list[str] = []

        def getter(url, **kw):
            calls.append(url)
            raise httpx.ConnectError(
                "offline",
                request=httpx.Request("GET", "https://qt.gtimg.cn/q=x"),
            )

        circuit = CircuitBreaker(failure_threshold=5, cooldown_seconds=600)
        source = TencentQuoteSource(
            circuit=circuit,
            clock=lambda: time.monotonic(),
            sleeper=lambda _s: None,
            rng=lambda: 0.0,
            http_getter=getter,
        )
        adapter = ExternalFallbackAdapter(tencent_source=source)

        for symbol in ("600001.SH", "600002.SH", "600003.SH"):
            result = adapter.resolve_realtime([symbol], local_rows=[])
            assert result.used_fallback is False

        # Each source call has 2 bounded retries, yet only the source circuit
        # sees transport failure; the adapter's calibration counter stays empty.
        assert len(calls) == 9
        assert adapter._calibration_failures == {}
        assert circuit.source_available("tencent_quote") is True

    def test_single_flight_dedupes_concurrent(self, enabled_realtime):
        barrier = threading.Event()
        started = threading.Event()
        calls: list = []
        result_content = _tencent_response()

        def slow_getter(url, **kw):  # noqa: ARG001
            calls.append(url)
            started.set()
            barrier.wait(timeout=2)
            return _fake_response(result_content)

        source = TencentQuoteSource(
            circuit=CircuitBreaker(),
            clock=lambda: time.monotonic(),
            sleeper=lambda _s: None,
            rng=lambda: 0.0,
            http_getter=slow_getter,
        )
        adapter = ExternalFallbackAdapter(tencent_source=source)

        results: list = []
        threads = [
            threading.Thread(target=lambda: results.append(
                adapter.resolve_realtime(["600519.SH"], local_rows=[])))
            for _ in range(4)
        ]
        for th in threads:
            th.start()
        started.wait(timeout=2)
        barrier.set()
        for th in threads:
            th.join(timeout=5)
        # single-flight: only 1 real HTTP call for identical URL
        assert len(calls) == 1
        assert all(r.used_fallback for r in results)

    def test_calibration_failure_counts_toward_circuit(self, enabled_realtime):
        """腾讯 HTTP 200 但返回全口径无效行 → 计入熔断; 连续 3 次后 source unavailable。

        每次用不同 symbol (不同 URL → 绕过响应缓存), 验证连续校准失败独立计数。
        """
        calls: list = []

        def getter(url, **kw):  # noqa: ARG001
            calls.append(url)
            assert kw.get("trust_env") is False
            return _fake_response(b"v_sh000000=\"garbage\";")

        source = TencentQuoteSource(
            circuit=CircuitBreaker(failure_threshold=3, cooldown_seconds=600.0),
            clock=lambda: 0.0, sleeper=lambda _s: None, rng=lambda: 0.0,
            http_getter=getter,
        )
        adapter = ExternalFallbackAdapter(tencent_source=source)
        # 3 calls with all-invalid calibration (distinct symbols) → circuit opens
        syms = ["600001.SH", "600002.SH", "600003.SH"]
        for s in syms:
            r = adapter.resolve_realtime([s], local_rows=[])
            assert r.used_fallback is False  # no valid rows
        assert len(calls) == 3
        # circuit now force-opened → 4th call short-circuits at source level (no fetch)
        adapter.resolve_realtime(["600004.SH"], local_rows=[])
        assert len(calls) == 3  # no new fetch while circuit open

    def test_get_circuit_safe_when_singleton_unconstructed(self):
        """get_circuit 不得假设单例已构造。"""
        fb_adapter.reset_adapter(None)
        assert ExternalFallbackAdapter.get_circuit() is None


# ===========================================================================
# 7. Write-path isolation
# ===========================================================================

class TestWriteIsolation:
    def test_adapter_has_no_repo_handle(self):
        """Adapter must not hold repository / QuoteService write references."""
        adapter = ExternalFallbackAdapter()
        # only allowed attributes: tencent source + lock
        forbidden = ["_repo", "repository", "_quote_service", "quote_service", "_writer"]
        for attr in forbidden:
            assert not hasattr(adapter, attr), f"adapter must not have {attr}"

    def test_result_rows_carry_source_marker(self, enabled_realtime):
        adapter = _make_adapter([])
        result = adapter.resolve_realtime(["600519.SH"], local_rows=[])
        assert result.used_fallback
        for row in result.rows:
            assert row["source"] == "tencent_quote"


# ===========================================================================
# 8. Privacy: no raw response/url in logs or exceptions
# ===========================================================================

class TestPrivacy:
    def test_host_allowlist_rejects_injected_host(self, caplog):
        calls: list = []

        def getter(url, **kw):  # noqa: ARG001
            calls.append(url)
            return _fake_response(b"")

        source = TencentQuoteSource(
            circuit=CircuitBreaker(),
            clock=lambda: 0.0, sleeper=lambda _s: None, rng=lambda: 0.0,
            http_getter=getter,
        )
        # _http_get with a non-allowlisted url → None, no call
        assert source._http_get("https://evil.example.com/q=sh600519") is None
        assert calls == []

    def test_tencent_url_constant_is_allowlisted_host(self):
        from urllib.parse import urlparse
        host = urlparse(tencent_quote.TENCENT_URL).hostname
        assert host in tencent_quote._ALLOWED_HOSTS



# ===========================================================================
# 9. Depth (P2): gate / fallback / calibration / circuit / write isolation
# ===========================================================================

from app.services.external_fallback.calibration import (
    filter_valid_depth,
    validate_depth_row,
)
from app.services.external_fallback.sources.tencent_quote import _parse_tencent_depth


def _tencent_depth_response(symbol_code: str = "sh600519", *, zero_ask1: bool = False) -> bytes:
    """Build a tencent payload with five-level depth fields populated.

    Field layout matches _parse_tencent_depth indices:
      bid_prices[i]=parts[9+i*2], bid_volumes[i]=parts[10+i*2],
      ask_prices[i]=parts[19+i*2], ask_volumes[i]=parts[20+i*2].
    """
    parts = ["0"] * 48
    parts[1] = "TestStock"
    parts[2] = symbol_code[2:]
    parts[30] = "20260807"
    parts[31] = "103000"
    for i in range(5):
        parts[9 + i * 2] = f"{10.0 - i * 0.01:.2f}"   # bid prices descending
        parts[10 + i * 2] = str(100 + i * 10)          # bid volumes
        parts[19 + i * 2] = f"{10.01 + i * 0.01:.2f}"  # ask prices ascending
        ask_vol = 0 if (zero_ask1 and i == 0) else (200 + i * 10)
        parts[20 + i * 2] = str(ask_vol)
    return (f'v_{symbol_code}="' + "~".join(parts) + '";').encode()


def _make_depth_adapter(calls: list, *, content_fn=None) -> ExternalFallbackAdapter:
    """Build an adapter whose HTTP returns depth responses."""
    factory = content_fn or _tencent_depth_response

    def http_getter(url, **kw):  # noqa: ARG001
        calls.append(url)
        assert kw.get("trust_env") is False, "trust_env must be False"
        assert kw.get("timeout", 0) <= 5.0
        return _fake_response(factory())

    source = TencentQuoteSource(
        circuit=CircuitBreaker(),
        clock=lambda: 0.0, sleeper=lambda _s: None, rng=lambda: 0.0,
        http_getter=http_getter,
    )
    return ExternalFallbackAdapter(tencent_source=source)


@pytest.fixture
def enabled_depth(monkeypatch):
    """Enable external fallback with depth scope + freeze trading day."""
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled", lambda: True
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes", lambda: ["depth"]
    )
    monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 7))  # Friday
    monkeypatch.setattr(fb_adapter, "_cn_today_iso", lambda: "2026-08-07")


class TestDepthGate:
    """未开启 / 无 scope / 非交易日 / provider 自有能力 → 零网络。"""

    def test_disabled_zero_network(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_enabled", lambda: False
        )
        calls: list = []
        adapter = _make_depth_adapter(calls)
        result = adapter.resolve_depth(["600519.SH"])
        assert result.used_fallback is False
        assert calls == []

    def test_scope_missing_zero_network(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_enabled", lambda: True
        )
        monkeypatch.setattr(
            "app.services.preferences.get_external_fallback_scopes", lambda: ["realtime"]
        )
        calls: list = []
        adapter = _make_depth_adapter(calls)
        result = adapter.resolve_depth(["600519.SH"])
        assert result.used_fallback is False
        assert calls == []

    def test_non_trading_day_zero_network(self, monkeypatch, enabled_depth):
        monkeypatch.setattr(fb_adapter, "_cn_today", lambda: date(2026, 8, 9))  # Sunday
        calls: list = []
        adapter = _make_depth_adapter(calls)
        result = adapter.resolve_depth(["600519.SH"])
        assert result.used_fallback is False
        assert calls == []

    def test_provider_has_depth_no_external(self, enabled_depth):
        """provider 有 depth 能力 → has_local_depth=True → 不走外部。"""
        calls: list = []
        adapter = _make_depth_adapter(calls)
        result = adapter.resolve_depth(["600519.SH"], has_local_depth=True)
        assert result.used_fallback is False
        assert calls == []


class TestDepthFallbackTriggered:
    """provider 无能力 + enabled → 腾讯五档 + provenance。"""

    def test_triggers_depth_fallback(self, enabled_depth):
        calls: list = []
        adapter = _make_depth_adapter(calls)
        result = adapter.resolve_depth(["600519.SH"])
        assert result.used_fallback is True
        assert result.source == "tencent_quote"
        assert result.reason == FallbackReason.PROVIDER_NO_DEPTH
        assert len(calls) == 1
        assert "600519.SH" in result.depth_map

    def test_depth_map_has_five_levels(self, enabled_depth):
        adapter = _make_depth_adapter([])
        result = adapter.resolve_depth(["600519.SH"])
        entry = result.depth_map["600519.SH"]
        assert len(entry["bid_prices"]) == 5
        assert len(entry["bid_volumes"]) == 5
        assert len(entry["ask_prices"]) == 5
        assert len(entry["ask_volumes"]) == 5
        assert entry["source"] == "tencent_quote"
        assert "+08:00" in entry["timestamp"]

    def test_hk_symbol_mapping(self, enabled_depth):
        """00700.HK ↔ hk00700 round-trip in depth path."""
        calls: list = []
        adapter = _make_depth_adapter(
            calls, content_fn=lambda: _tencent_depth_response("hk00700")
        )
        result = adapter.resolve_depth(["00700.HK"])
        assert result.used_fallback is True
        assert "00700.HK" in result.depth_map
        assert len(calls) == 1


class TestDepthCalibration:
    """五档索引 pinning + 单位 + 零量保留。"""

    def test_bid_ask_index_pinning(self):
        """parts[9]=bid_p[0], parts[10]=bid_v[0], parts[19]=ask_p[0], parts[20]=ask_v[0]。"""
        text = _tencent_depth_response().decode()
        depth_map = _parse_tencent_depth(text)
        entry = depth_map["600519.SH"]
        assert entry["bid_prices"][0] == 10.0
        assert entry["bid_volumes"][0] == 100
        assert entry["ask_prices"][0] == 10.01
        assert entry["ask_volumes"][0] == 200
        # full 5-level structure
        assert entry["bid_prices"] == [10.0, 9.99, 9.98, 9.97, 9.96]
        assert entry["bid_volumes"] == [100, 110, 120, 130, 140]
        assert entry["ask_prices"] == [10.01, 10.02, 10.03, 10.04, 10.05]
        assert entry["ask_volumes"] == [200, 210, 220, 230, 240]

    def test_zero_volume_preserved(self):
        """ask_volumes[0]=0 (真封涨停) 不被校准丢弃 — 封单检测不变量。"""
        text = _tencent_depth_response(zero_ask1=True).decode()
        depth_map = _parse_tencent_depth(text)
        entry = depth_map["600519.SH"]
        assert entry["ask_volumes"][0] == 0  # 0 是有效值
        # filter_valid_depth 不可丢弃零量行
        filtered = filter_valid_depth(depth_map)
        assert "600519.SH" in filtered
        assert filtered["600519.SH"]["ask_volumes"][0] == 0

    def test_validate_depth_row_rejects_short_list(self):
        row = {
            "symbol": "600519.SH",
            "bid_prices": [10.0, 9.99],  # too short
            "bid_volumes": [100, 110, 120, 130, 140],
            "ask_prices": [10.01, 10.02, 10.03, 10.04, 10.05],
            "ask_volumes": [200, 210, 220, 230, 240],
            "timestamp": "2026-08-07T10:30:00+08:00",
        }
        assert validate_depth_row(row) is False

    def test_validate_depth_row_rejects_negative_volume(self):
        row = {
            "symbol": "600519.SH",
            "bid_prices": [10.0] * 5,
            "bid_volumes": [100, 110, 120, 130, -1],
            "ask_prices": [10.01] * 5,
            "ask_volumes": [200] * 5,
            "timestamp": "2026-08-07T10:30:00+08:00",
        }
        assert validate_depth_row(row) is False

    def test_validate_depth_row_rejects_bad_symbol(self):
        row = {
            "symbol": "BAD",
            "bid_prices": [10.0] * 5,
            "bid_volumes": [100] * 5,
            "ask_prices": [10.01] * 5,
            "ask_volumes": [200] * 5,
            "timestamp": "2026-08-07T10:30:00+08:00",
        }
        assert validate_depth_row(row) is False


class TestDepthCircuit:
    """depth 校准失败连续 3 次熔断; 网络失败不计入口径失败。"""

    def test_calibration_failure_opens_circuit(self, enabled_depth):
        calls: list = []

        def getter(url, **kw):  # noqa: ARG001
            calls.append(url)
            assert kw.get("trust_env") is False
            return _fake_response(b"v_sh000000=\"garbage\";")

        source = TencentQuoteSource(
            circuit=CircuitBreaker(failure_threshold=3, cooldown_seconds=600.0),
            clock=lambda: 0.0, sleeper=lambda _s: None, rng=lambda: 0.0,
            http_getter=getter,
        )
        adapter = ExternalFallbackAdapter(tencent_source=source)
        # 3 distinct symbols (distinct URLs → bypass cache) → 3 calibration failures
        for s in ("600001.SH", "600002.SH", "600003.SH"):
            r = adapter.resolve_depth([s])
            assert r.used_fallback is False
        assert len(calls) == 3
        # circuit now force-opened → 4th call short-circuits (no fetch)
        adapter.resolve_depth(["600004.SH"])
        assert len(calls) == 3

    def test_network_failure_not_calibration_failure(self, enabled_depth):
        """网络失败由源熔断器单独计数, 不被 adapter 重复计成口径失败。"""
        calls: list = []

        def getter(url, **kw):  # noqa: ARG001
            calls.append(url)
            raise httpx.ConnectError(
                "offline", request=httpx.Request("GET", "https://qt.gtimg.cn/q=x")
            )

        circuit = CircuitBreaker(failure_threshold=5, cooldown_seconds=600)
        source = TencentQuoteSource(
            circuit=circuit, clock=lambda: 0.0, sleeper=lambda _s: None,
            rng=lambda: 0.0, http_getter=getter,
        )
        adapter = ExternalFallbackAdapter(tencent_source=source)
        for symbol in ("600001.SH", "600002.SH", "600003.SH"):
            r = adapter.resolve_depth([symbol])
            assert r.used_fallback is False
        assert adapter._calibration_failures == {}
        assert circuit.source_available("tencent_quote") is True


class TestDepthWriteIsolation:
    """resolve_depth 返回的 depth_map 每项 source='tencent_quote'。"""

    def test_depth_entries_carry_source(self, enabled_depth):
        adapter = _make_depth_adapter([])
        result = adapter.resolve_depth(["600519.SH", "600000.SH"])
        assert result.used_fallback
        for sym, entry in result.depth_map.items():
            assert entry["source"] == "tencent_quote"

    def test_depth_result_has_no_repo_handle(self):
        adapter = ExternalFallbackAdapter()
        forbidden = ["_repo", "repository", "_quote_service", "quote_service", "_writer"]
        for attr in forbidden:
            assert not hasattr(adapter, attr), f"adapter must not have {attr}"