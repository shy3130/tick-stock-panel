from app.capabilities import Cap
from app.data_providers import capability_gate
from app.data_providers.base import ProviderCapabilities


class _Provider:
    def __init__(self, caps: ProviderCapabilities) -> None:
        self.capabilities = caps


def _capset(monkeypatch, caps: ProviderCapabilities):
    monkeypatch.setattr(capability_gate, "_active_provider_name", lambda: "fquant_local")
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: _Provider(caps))
    return capability_gate._provider_capset()


def test_month_cap_granted_only_when_provider_declares_it(monkeypatch):
    cs = _capset(monkeypatch, ProviderCapabilities(minute=True, minute_month_extension=True))
    assert cs.has(Cap.KLINE_MINUTE_BATCH)
    assert cs.has(Cap.KLINE_MINUTE_MONTH)


def test_month_cap_absent_when_provider_only_has_minute(monkeypatch):
    cs = _capset(monkeypatch, ProviderCapabilities(minute=True))
    assert cs.has(Cap.KLINE_MINUTE_BATCH)
    assert not cs.has(Cap.KLINE_MINUTE_MONTH)
