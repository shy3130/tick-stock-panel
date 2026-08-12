import json
import threading
from datetime import date
from types import SimpleNamespace

import app.services.quote_service as quote_service


def _fake_provider(name: str, realtime: bool = True):
    return SimpleNamespace(
        name=name,
        capabilities=SimpleNamespace(realtime=realtime),
    )


def test_local_realtime_provider_uses_full_market_mode(monkeypatch):
    quote_service._provider_instance = None
    monkeypatch.setattr(quote_service, "get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr(quote_service, "get_provider", lambda name: _fake_provider(name))

    service = quote_service.QuoteService()

    assert quote_service.QuoteService.realtime_mode() == "full_market"
    assert quote_service.QuoteService.is_realtime_allowed()
    assert service.get_min_interval() == quote_service.QuoteService.DEFAULT_INTERVAL


def test_provider_without_realtime_blocks_realtime(monkeypatch):
    quote_service._provider_instance = None
    monkeypatch.setattr(quote_service, "get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr(quote_service, "get_provider", lambda name: _fake_provider(name, realtime=False))

    assert quote_service.QuoteService.realtime_mode() == "none"
    assert not quote_service.QuoteService.is_realtime_allowed()


def test_record_from_quote_converts_realtime_points_to_ratio():
    record = quote_service.QuoteService._record_from_quote({
        "symbol": "600519.SH",
        "last_price": 101,
        "prev_close": 100,
        "ext": {"change_pct": 1.23, "amplitude": 4.56, "turnover_rate": 7.89},
    })

    assert abs(record["change_pct"] - 0.0123) < 1e-12
    assert abs(record["amplitude"] - 0.0456) < 1e-12
    assert record["turnover_rate"] == 7.89


def test_record_from_quote_converts_non_finite_numbers_to_null():
    record = quote_service.QuoteService._record_from_quote({
        "symbol": "600519.SH",
        "last_price": float("nan"),
        "prev_close": float("inf"),
        "open": -float("inf"),
        "volume": float("nan"),
        "ext": {"change_amount": float("inf"), "turnover_rate": float("nan")},
    })
    assert record["last_price"] is None
    assert record["prev_close"] is None
    assert record["open"] is None
    assert record["volume"] is None
    assert record["change_amount"] is None
    assert record["turnover_rate"] is None
    json.dumps(record, allow_nan=False)


def test_index_quote_cache_outputs_percentage_points():
    record = quote_service.QuoteService._record_from_quote({
        "symbol": "000001.INDEX",
        "last_price": 101,
        "prev_close": 100,
        "ext": {"change_pct": 1.23, "amplitude": 4.56},
    })

    row = quote_service.QuoteService._build_index_quotes([record]).to_dicts()[0]

    assert row["change_pct"] == 1.23
    assert row["amplitude"] == 4.56



def test_stop_in_close_final_calls_run_final_sync_exactly_once(monkeypatch):
    """stop() 在 close_final 阶段必须恰好调用 _run_final_sync 一次。"""
    quote_service._provider_instance = None
    monkeypatch.setattr(quote_service, "get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr(quote_service, "get_provider", lambda name: _fake_provider(name))

    service = quote_service.QuoteService()
    # 强制 close_final 阶段
    monkeypatch.setattr(quote_service.QuoteService, "_market_phase", staticmethod(lambda: "close_final"))
    # _fetch_quotes 不应实际执行 (无 repo / 无 provider 连接)
    monkeypatch.setattr(service, "_fetch_quotes", lambda: True)

    # spy: 捕获 _run_final_sync 调用次数
    call_count = 0
    original_run_final_sync = service._run_final_sync

    def spy_run_final_sync():
        nonlocal call_count
        call_count += 1
        original_run_final_sync()

    monkeypatch.setattr(service, "_run_final_sync", spy_run_final_sync)

    service.stop()

    assert call_count == 1, f"expected _run_final_sync called exactly once, got {call_count}"


def test_final_sync_skipped_by_reentrancy_remains_pending(monkeypatch):
    """慢拉取占用执行权时，收盘同步必须保留 pending，不能误记完成。"""
    service = quote_service.QuoteService()
    key = (date(2026, 8, 10), "close")
    monkeypatch.setattr(
        quote_service.QuoteService, "_market_phase", staticmethod(lambda: "close_final")
    )
    monkeypatch.setattr(
        quote_service.QuoteService, "_final_sync_key", staticmethod(lambda _phase: key)
    )
    service._fetch_in_progress = True

    assert service._run_final_sync() is False
    assert key not in service._final_sync_done
    assert key not in service._final_sync_failed


def test_fetch_quotes_reentrancy_guard_skips_overlapping_pull(monkeypatch):
    """轮询间隔小于一次 full-market 拉取耗时时, 后台轮询 / 手动 refresh /
    收盘 final_sync 并发触发 _fetch_quotes, 上一轮未结束必须直接跳过,
    不并发重入重复写盘 / 重复算指标 (状态语义仍准确, 不假装 ready)。"""
    quote_service._provider_instance = None
    monkeypatch.setattr(
        quote_service, "get_active_provider_name", lambda capability=None: "fquant_local"
    )
    monkeypatch.setattr(quote_service, "get_provider", lambda name: _fake_provider(name))

    service = quote_service.QuoteService()
    monkeypatch.setattr(
        quote_service.QuoteService, "realtime_mode", staticmethod(lambda: "full_market")
    )

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_full_market(self):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2.0)  # 模拟一次慢的全市场拉取 (> interval)

    monkeypatch.setattr(
        quote_service.QuoteService, "_fetch_full_market_quotes", slow_full_market
    )

    t1 = threading.Thread(target=service._fetch_quotes)
    t2 = threading.Thread(target=service._fetch_quotes)
    t1.start()
    assert started.wait(timeout=2.0)  # t1 已进入拉取并持有重入标志
    t2.start()
    t2.join(timeout=2.0)
    assert not t2.is_alive(), "重入调用应立即跳过返回, 不阻塞"
    assert calls["n"] == 1, "并发重入必须被跳过, 不得重复拉取"
    release.set()
    t1.join(timeout=2.0)
    assert calls["n"] == 1