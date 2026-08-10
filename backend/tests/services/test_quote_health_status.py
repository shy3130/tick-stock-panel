"""QuoteService 数据健康状态行为测试。

覆盖共享 Contract 的可观察状态:
  - disabled (未启用)
  - warming_up (启用但未完成任何成功/失败轮次)
  - ready (成功非空且新鲜)
  - empty (provider 返回空)
  - error (provider 异常, 不暴露原始错误)
  - stale (成功数据过期)
  - source_as_of 追加字段
  - 零外部 (新浪/腾讯) 调用: 行情只走 provider.get_realtime
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl

import app.services.quote_service as quote_service


# --------------------------------------------------------------------------- #
# 隔离 fixtures
# --------------------------------------------------------------------------- #
def _make_provider(get_realtime, realtime: bool = True):
    """构造一个 fake provider, 仅暴露 quote_service 实际用到的接口。"""

    class _FakeProvider:
        name = "fquant_local"

        def __init__(self) -> None:
            self.capabilities = SimpleNamespace(realtime=realtime)
            self.get_realtime_calls: list[dict] = []

        # quote_service 仅调用 get_realtime(universes=...) / get_realtime(symbols=...)
        def get_realtime(self, symbols=None, universes=None):
            self.get_realtime_calls.append({"symbols": symbols, "universes": universes})
            return get_realtime(symbols=symbols, universes=universes)

    return _FakeProvider()


def _wire_provider(monkeypatch, provider) -> None:
    """让 quote_service 模块级的 provider 解析与 realtime_mode 走 fake provider。"""
    quote_service._provider_instance = provider
    monkeypatch.setattr(
        quote_service, "get_active_provider_name", lambda capability=None: "fquant_local"
    )
    monkeypatch.setattr(quote_service, "get_provider", lambda name: provider)


def _wire_preferences(monkeypatch, **overrides) -> None:
    """打桩所有 quote_service 读取的 preferences getter。"""
    defaults = {
        "watchlist_symbols": [],
        "pull_stock": True,
        "pull_etf": False,
        "pull_index": True,
        "index_mode": "all",  # 走 universes 路径, 避免触碰 _repo
        "index_symbols": list(quote_service.QuoteService.CORE_INDEX_SYMBOLS),
    }
    defaults.update(overrides)

    import app.services.preferences as preferences

    monkeypatch.setattr(
        preferences,
        "get_realtime_watchlist_symbols",
        lambda: list(defaults["watchlist_symbols"]),
    )
    monkeypatch.setattr(preferences, "get_realtime_pull_stock", lambda: defaults["pull_stock"])
    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: defaults["pull_etf"])
    monkeypatch.setattr(preferences, "get_realtime_pull_index", lambda: defaults["pull_index"])
    monkeypatch.setattr(preferences, "get_realtime_index_mode", lambda: defaults["index_mode"])
    monkeypatch.setattr(
        preferences, "get_realtime_index_symbols", lambda: list(defaults["index_symbols"])
    )


def _new_service(monkeypatch, provider) -> quote_service.QuoteService:
    """构造一个 quote_service, 隔离 repo (None) 与交易时段判定。"""
    _wire_provider(monkeypatch, provider)
    _wire_preferences(monkeypatch)
    service = quote_service.QuoteService()
    # 隔离交易时段判定与写盘/监控副作用
    monkeypatch.setattr(quote_service.QuoteService, "_is_trading_hours", staticmethod(lambda: True))
    service._repo = None  # 不触发任何 parquet 写盘 / enriched
    return service


def _quote_row(symbol="600519.SH", ts="2026-08-07") -> dict:
    return {
        "symbol": symbol,
        "name": "贵州茅台",
        "last_price": 101.0,
        "prev_close": 100.0,
        "open": 100.5,
        "high": 102.0,
        "low": 99.5,
        "volume": 123456,
        "amount": 6543210.0,
        "timestamp": ts,
        "source": "fquant_local:fstore:daily_markets",
        "ext": {"change_pct": 1.0, "change_amount": 1.0, "amplitude": 2.5, "turnover_rate": 0.8},
    }


# --------------------------------------------------------------------------- #
# 状态行为
# --------------------------------------------------------------------------- #
def test_disabled_when_not_enabled(monkeypatch):
    """未启用的服务 data_state 必须是 disabled。"""
    provider = _make_provider(lambda **kw: pl.DataFrame())
    service = _new_service(monkeypatch, provider)
    # 构造后默认 _enabled = False
    status = service.status()
    assert status["data_state"] == "disabled"
    assert status["has_recent_data"] is False
    assert status["last_error_code"] is None
    assert status["source_as_of"] is None
    # 既有字段仍存在
    for legacy in (
        "enabled", "running", "mode", "realtime_allowed",
        "watchlist_symbol_count", "interval_s", "symbol_count",
        "index_symbol_count", "etf_symbol_count", "quote_age_ms",
        "is_trading_hours", "last_fetch_ms",
    ):
        assert legacy in status


def test_warming_up_before_first_fetch(monkeypatch):
    """启用但尚未完成任何成功/失败轮次时 data_state 必须是 warming_up。"""
    provider = _make_provider(lambda **kw: pl.DataFrame())
    service = _new_service(monkeypatch, provider)
    service._enabled = True
    status = service.status()
    assert status["data_state"] == "warming_up"
    assert status["has_recent_data"] is False


def test_ready_after_non_empty_success(monkeypatch):
    """provider 返回非空数据后 data_state 必须是 ready 且 source_as_of 回填。"""
    provider = _make_provider(lambda **kw: pl.DataFrame([_quote_row()]))
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    status = service.status()
    assert status["data_state"] == "ready"
    assert status["has_recent_data"] is True
    assert status["total_symbol_count"] > 0
    assert status["last_error_code"] is None
    assert status["source_as_of"] == "2026-08-07"


def test_normalized_index_symbol_populates_index_cache(monkeypatch):
    """本地 provider 的 .INDEX 归一化符号必须进入指数缓存，不能误归为股票。"""
    provider = _make_provider(
        lambda **kw: pl.DataFrame([_quote_row(symbol="000001.INDEX")])
    )
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()

    status = service.status()
    assert status["symbol_count"] == 0
    assert status["index_symbol_count"] == 1
    assert service.get_index_quotes().height == 1


def test_empty_when_provider_returns_empty(monkeypatch):
    """provider 返回空数据后 data_state 必须是 empty, last_error_code = provider_empty。"""
    provider = _make_provider(lambda **kw: pl.DataFrame())
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    status = service.status()
    assert status["data_state"] == "empty"
    assert status["has_recent_data"] is False
    assert status["last_error_code"] == "provider_empty"
    assert status["total_symbol_count"] == 0


def test_error_when_provider_raises(monkeypatch):
    """provider 抛异常时 data_state 必须是 error, 且不暴露原始异常文本。"""
    class _Boom(Exception):
        pass

    def _raise(**kw):
        raise _Boom("sensitive: file=/data/secret.duckdb url=http://x token=abc")

    provider = _make_provider(_raise)
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    status = service.status()
    assert status["data_state"] == "error"
    assert status["has_recent_data"] is False
    assert status["last_error_code"] == "provider_error"
    # 关键: 不得泄露原始异常/路径/URL/凭据
    payload = repr(status)
    assert "sensitive" not in payload
    assert "/data/secret.duckdb" not in payload
    assert "http://x" not in payload
    assert "token=abc" not in payload
    assert "error" in payload.lower()  # 仅枚举值


def test_stale_when_success_data_exceeds_recent_window(monkeypatch):
    """成功数据存在但超过 recent_window 时 data_state 必须降级为 stale。"""
    provider = _make_provider(lambda **kw: pl.DataFrame([_quote_row()]))
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    # 成功一次, 然后把"最近成功时间"人工拨到很久以前
    service._last_success_perf -= 10_000.0  # 远超 recent_window
    status = service.status()
    assert status["data_state"] == "stale"
    assert status["has_recent_data"] is False
    # source_as_of 仍保留历史快照时间戳
    assert status["source_as_of"] == "2026-08-07"


def test_ready_then_empty_transitions_back(monkeypatch):
    """ready 之后再来一次空轮次, 必须切回 empty (最近轮次优先)。"""
    rows = {"value": pl.DataFrame([_quote_row()])}

    def _switch(**kw):
        return rows["value"]

    provider = _make_provider(_switch)
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    assert service.status()["data_state"] == "ready"

    rows["value"] = pl.DataFrame()
    service._fetch_full_market_quotes()
    status = service.status()
    assert status["data_state"] == "empty"
    assert status["has_recent_data"] is False


def test_watchlist_path_records_health(monkeypatch):
    """watchlist 拉取路径同样维护健康状态 (ready / empty / error)。"""
    provider = _make_provider(lambda **kw: pl.DataFrame([_quote_row(symbol="600519.SH")]))
    service = _new_service(monkeypatch, provider)
    service._enabled = True
    # 强制走 watchlist 路径
    monkeypatch.setattr(
        quote_service.QuoteService, "realtime_mode", staticmethod(lambda: "watchlist")
    )
    _wire_preferences(monkeypatch, watchlist_symbols=["600519.SH"], pull_index=False)

    service._fetch_quotes()
    assert service.status()["data_state"] == "ready"

    # 切空
    provider.get_realtime = lambda **kw: pl.DataFrame()  # type: ignore[assignment]
    service._fetch_quotes()
    assert service.status()["data_state"] == "empty"

    # 切异常
    def _boom(**kw):
        raise RuntimeError("boom")

    provider.get_realtime = _boom  # type: ignore[assignment]
    service._fetch_quotes()
    assert service.status()["data_state"] == "error"


# --------------------------------------------------------------------------- #
# 边界: 零外部调用
# --------------------------------------------------------------------------- #
def test_no_external_quote_clients_instantiated(monkeypatch):
    """QuoteService 不得再实例化 SinaTencentClient (恢复 data_providers 边界)。"""
    import app.data_providers.fquant.sina_tencent_client as stc

    constructed: list = []
    original = stc.SinaTencentClient

    class _Probe(original):  # 继承以兼容签名
        def __init__(self, *a, **kw):
            constructed.append(self)

    monkeypatch.setattr(stc, "SinaTencentClient", _Probe, raising=True)

    # 模块 import 级 monkeypatch: quote_service 内部已无 import, 但若将来重新引入,
    # 这里会因 _Probe 被构造而捕获。
    quote_service.QuoteService()
    assert constructed == [], "QuoteService 不应再实例化 SinaTencentClient"


def test_no_external_network_calls_during_fetch(monkeypatch):
    """成功/空/异常路径都只应调用 provider.get_realtime, 不触发任何网络层。"""
    provider = _make_provider(lambda **kw: pl.DataFrame([_quote_row()]))
    service = _new_service(monkeypatch, provider)
    service._enabled = True

    service._fetch_full_market_quotes()
    assert provider.get_realtime_calls, "必须通过 provider.get_realtime 取数"
    # 客户端实例不应存在
    assert not hasattr(service, "_live_client")
