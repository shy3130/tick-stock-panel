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


def test_index_quote_cache_outputs_percentage_points():
    record = quote_service.QuoteService._record_from_quote({
        "symbol": "000001.SH",
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
    monkeypatch.setattr(service, "_fetch_quotes", lambda: None)

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