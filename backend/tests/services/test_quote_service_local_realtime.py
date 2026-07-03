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
