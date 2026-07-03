from app.capabilities import Cap
from app.data_providers import capability_gate
from app.data_providers.base import ProviderCapabilities


class _StubProvider:
    def __init__(self, caps: ProviderCapabilities) -> None:
        self.capabilities = caps


def test_detect_translates_provider_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(capability_gate, "_active_provider_name", lambda: "fquant_local")
    monkeypatch.setattr(
        "app.data_providers.get_provider",
        lambda name: _StubProvider(ProviderCapabilities(daily=True, minute=True, realtime=True)),
    )
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)

    capset = capability_gate.detect_capabilities()

    assert capset.has(Cap.KLINE_DAILY_BATCH)
    assert capset.has(Cap.KLINE_MINUTE_BATCH)
    assert capset.has(Cap.QUOTE_BATCH)
    assert not capset.has(Cap.DEPTH5_BATCH)
    assert capability_gate.tier_label() == "Fquant_local"
