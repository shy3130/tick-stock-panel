"""Custom depth provider routing and failure-isolation tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

from app.services import depth_service as depth_module
from app.services.depth_service import DepthService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _service(*, batch: int = 2, rpm: int = 30) -> DepthService:
    service = DepthService()
    service._app_state = SimpleNamespace(
        capabilities=CapabilitySet({
            Cap.DEPTH5_BATCH: CapabilityLimits(batch=batch, rpm=rpm),
        }),
    )
    return service


def test_custom_depth_uses_shared_batching_and_rate_limit(monkeypatch):
    provider = SimpleNamespace(
        get_depth_batch=MagicMock(
            side_effect=lambda symbols: {symbol: {"ask_volumes": [0]} for symbol in symbols}
        )
    )
    sleep = MagicMock()
    monkeypatch.setattr(
        "app.services.preferences.get_depth5_data_provider",
        lambda: "custom_depth",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, dataset: name == "custom_depth" and dataset == "depth5",
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    monkeypatch.setattr(depth_module, "sleep_between_batches", sleep)

    result = _service()._call_depth_batch(["A", "B", "C", "D", "E"])

    assert set(result) == {"A", "B", "C", "D", "E"}
    assert provider.get_depth_batch.call_args_list == [
        call(["A", "B"]),
        call(["C", "D"]),
        call(["E"]),
    ]
    assert sleep.call_args_list == [
        call(0, 24, default_interval=2.0),
        call(1, 24, default_interval=2.0),
        call(2, 24, default_interval=2.0),
    ]


def test_custom_depth_failure_does_not_fall_back_to_tickflow(monkeypatch):
    provider = SimpleNamespace(
        get_depth_batch=MagicMock(side_effect=RuntimeError("custom source down"))
    )
    monkeypatch.setattr(
        "app.services.preferences.get_depth5_data_provider",
        lambda: "custom_depth",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, dataset: True,
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    monkeypatch.setattr(
        "app.tickflow.client.get_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not fall back to TickFlow")),
    )

    assert _service()._call_depth_batch(["A"]) == {}
    provider.get_depth_batch.assert_called_once_with(["A"])


def test_custom_depth_failure_isolated_per_batch(monkeypatch):
    provider = SimpleNamespace(
        get_depth_batch=MagicMock(
            side_effect=[
                RuntimeError("first batch down"),
                {"C": {"ask_volumes": [0]}},
            ]
        )
    )
    monkeypatch.setattr(
        "app.services.preferences.get_depth5_data_provider",
        lambda: "custom_depth",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, dataset: True,
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    monkeypatch.setattr(depth_module, "sleep_between_batches", MagicMock())

    assert _service()._call_depth_batch(["A", "B", "C"]) == {
        "C": {"ask_volumes": [0]},
    }
    assert provider.get_depth_batch.call_count == 2


def test_tickflow_depth_uses_provider_contract_and_shared_batching(monkeypatch):
    batch = MagicMock(
        side_effect=lambda symbols: {symbol: {"ask_volumes": [0]} for symbol in symbols}
    )
    tickflow = SimpleNamespace(depth=SimpleNamespace(batch=batch))
    monkeypatch.setattr(
        "app.services.preferences.get_depth5_data_provider",
        lambda: "tickflow",
    )
    monkeypatch.setattr("app.data_providers.tickflow_provider.get_client", lambda: tickflow)
    monkeypatch.setattr(depth_module, "sleep_between_batches", MagicMock())

    result = _service()._call_depth_batch(["A", "B", "C"])

    assert set(result) == {"A", "B", "C"}
    assert batch.call_args_list == [call(["A", "B"]), call(["C"])]


def test_invalid_custom_depth_contract_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_depth5_data_provider",
        lambda: "broken_depth",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, dataset: False,
    )
    get_provider = MagicMock()
    monkeypatch.setattr("app.data_providers.custom.get_provider", get_provider)

    assert _service()._call_depth_batch(["A"]) == {}
    get_provider.assert_not_called()


def test_custom_depth_provider_maps_asks_and_bids(monkeypatch):
    from app.data_providers.custom.config import config_from_dict
    from app.data_providers.custom.provider import GenericHTTPProvider

    provider = GenericHTTPProvider(config_from_dict({
        "name": "eltdx",
        "datasets": {
            "depth5": {
                "url": "http://example.test/depth5",
                "response_path": "data",
                "field_map": {
                    "symbol": "symbol", "asks": "asks", "bids": "bids",
                },
            },
        },
    }))
    monkeypatch.setattr(provider, "_request_rows", lambda cfg, **kwargs: [{
        "symbol": "000001.SZ",
        "asks": [[11.33, 1256]],
        "bids": [[11.32, 857]],
    }])

    assert provider.get_depth_batch(["000001.SZ"]) == {
        "000001.SZ": {
            "ask_volumes": [1256],
            "bid_volumes": [857],
            "asks": [[11.33, 1256]],
            "bids": [[11.32, 857]],
        },
    }


def test_depth5_config_is_accepted_and_does_not_require_dates(monkeypatch):
    from app.data_providers.custom.config import config_from_dict
    from app.data_providers.custom.provider import GenericHTTPProvider

    provider = GenericHTTPProvider(config_from_dict({
        "name": "eltdx",
        "datasets": {
            "depth5": {
                "url": "http://example.test/depth5",
                "field_map": {"symbol": "symbol", "asks": "asks", "bids": "bids"},
            },
        },
    }))

    assert provider.validate() == []
    monkeypatch.setattr(provider, "_request_rows", lambda cfg, **kwargs: [])
    assert provider.test_dataset("depth5")["dataset"] == "depth5"
