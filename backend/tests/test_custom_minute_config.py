import math
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.settings import (
    CustomSourceIn,
    CustomSourceTestIn,
    DatasetConfigIn,
)
from app.api.settings import (
    test_data_source as run_data_source_test,
)
from app.data_providers.custom.config import (
    MAX_TIMEOUT,
    CustomSourceConfig,
    DatasetConfig,
    _dataset_from_dict,
    load_config,
)
from app.data_providers.custom.loader import _config_to_dict, _sanitize_for_yaml
from app.data_providers.custom.provider import GenericHTTPProvider


def test_minute_request_parameter_names_survive_config_round_trip():
    dataset = DatasetConfigIn(
        url="https://example.test/minute",
        method="GET",
        asset_type_param="asset",
        freq_param="period",
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"minute": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"minute": parsed},
    ))

    assert parsed.asset_type_param == "asset"
    assert parsed.freq_param == "period"
    assert exposed["datasets"]["minute"]["asset_type_param"] == "asset"
    assert exposed["datasets"]["minute"]["freq_param"] == "period"


def test_timeout_survives_config_round_trip():
    """timeout 必须在 UI 保存往返中保留 (核心修复), 且默认 30 不污染 YAML。"""
    dataset = DatasetConfigIn(
        url="https://example.test/daily",
        method="POST",
        timeout=120.0,
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"daily": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["daily"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"daily": parsed},
    ))

    assert parsed.timeout == 120.0
    assert exposed["datasets"]["daily"]["timeout"] == 120.0

    # 默认 30 不 emit, 保持 YAML 干净
    default_dataset = DatasetConfigIn(url="https://example.test/realtime", method="GET").model_dump()
    cleaned2 = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"realtime": default_dataset},
    })
    parsed2 = _dataset_from_dict(cleaned2["datasets"]["realtime"])
    exposed2 = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"realtime": parsed2},
    ))
    assert parsed2.timeout == 30.0
    realtime = exposed2["datasets"]["realtime"]
    assert "timeout" not in realtime
    assert "symbols_param" not in realtime
    assert "start_param" not in realtime
    assert "end_param" not in realtime

    explicit_default = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "daily": {
                "url": "https://example.test/daily",
                "timeout": 30.0,
            },
        },
    })
    assert "timeout" not in explicit_default["datasets"]["daily"]


@pytest.mark.parametrize(
    "timeout",
    [0, -1, math.nan, math.inf, -math.inf, MAX_TIMEOUT + 1],
)
def test_timeout_api_rejects_out_of_range_or_non_finite_values(timeout):
    with pytest.raises(ValidationError):
        DatasetConfigIn(url="https://example.test/daily", timeout=timeout)


def test_invalid_yaml_timeout_is_a_load_error(tmp_path: Path):
    for index, timeout in enumerate((0, -1, math.nan, math.inf, -math.inf, "invalid")):
        path = tmp_path / f"invalid_{index}.yaml"
        path.write_text(
            "\n".join([
                "name: invalid",
                "datasets:",
                "  daily:",
                "    url: https://example.test/daily",
                f"    timeout: {timeout}",
            ]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="timeout must be"):
            load_config(path)


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf, "invalid"])
def test_sanitizer_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout must be"):
        _sanitize_for_yaml({
            "name": "test_source",
            "datasets": {
                "realtime": {
                    "url": "https://example.test/realtime",
                    "timeout": timeout,
                    "symbols_param": "codes",
                    "start_param": "from",
                    "end_param": "to",
                },
            },
        })


def test_sanitizer_drops_realtime_request_params():
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "realtime": {
                "url": "https://example.test/realtime",
                "symbols_param": "codes",
                "start_param": "from",
                "end_param": "to",
            },
        },
    })

    dataset = cleaned["datasets"]["realtime"]
    assert "symbols_param" not in dataset
    assert "start_param" not in dataset
    assert "end_param" not in dataset


def test_empty_request_parameter_names_restore_defaults():
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "minute": {
                "url": "https://example.test/minute",
                "symbols_param": " ",
                "start_param": "\t",
                "end_param": "",
            },
        },
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])

    assert parsed.symbols_param == "symbols"
    assert parsed.start_param == "start_time"
    assert parsed.end_param == "end_time"


def _dataset_config(dataset: str = "minute", **overrides) -> DatasetConfig:
    required = {
        "daily": ("symbol", "date", "open", "high", "low", "close", "volume", "amount"),
        "adj_factor": ("symbol", "trade_date", "ex_factor"),
        "realtime": ("symbol", "last_price", "prev_close", "open", "high", "low", "volume"),
        "minute": ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount"),
    }
    values = {
        "url": f"https://example.test/{dataset}",
        "field_map": {name: name for name in required[dataset]},
        **overrides,
    }
    return DatasetConfig(**values)


def _capture_test_request(dataset: str, **overrides):
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={dataset: _dataset_config(dataset, **overrides)},
    ))
    captured = {}

    def request_rows(cfg, **kwargs):
        captured.update(kwargs)
        return []

    provider._request_rows = request_rows
    provider.test_dataset(dataset, ["600000.SH"])
    provider.close()
    return captured


def test_test_dataset_realtime_omits_symbol_and_time_parameters():
    assert _capture_test_request("realtime") == {}


@pytest.mark.parametrize("dataset", ["daily", "adj_factor"])
def test_test_dataset_history_uses_symbol_and_short_time_range(dataset):
    captured = _capture_test_request(dataset)

    assert captured["symbols"] == ["600000.SH"]
    assert isinstance(captured["start_time"], datetime)
    assert isinstance(captured["end_time"], datetime)
    assert (captured["end_time"] - captured["start_time"]).days == 7


def test_test_dataset_minute_injects_production_overrides():
    captured = _capture_test_request(
        "minute",
        asset_type_param="asset",
        freq_param="period",
    )

    assert captured["symbols"] == ["600000.SH"]
    assert captured["override_params"] == {"asset": "stock", "period": "1m"}
    assert captured["override_body"] == {"asset": "stock", "period": "1m"}


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_duplicate_dynamic_parameter_names_are_rejected(dataset):
    config = _dataset_config(dataset, symbols_param="range", start_param="range")
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={dataset: config},
    ))

    try:
        assert provider.validate() == [
            f"{dataset}: duplicate request parameter names: range"
        ]
    finally:
        provider.close()

    with pytest.raises(ValueError, match="duplicate request parameter names: range"):
        _sanitize_for_yaml({
            "name": "test_source",
            "datasets": {
                dataset: {
                    "url": config.url,
                    "symbols_param": "range",
                    "start_param": "range",
                },
            },
        })


def test_data_source_trial_uses_unsaved_config_and_closes_provider(monkeypatch):
    from app.data_providers import custom as custom_sources

    provider = Mock()
    provider.test_dataset.return_value = {
        "provider": "draft",
        "dataset": "realtime",
        "rows": 0,
        "columns": [],
        "preview": [],
    }
    create_provider = Mock(return_value=provider)
    monkeypatch.setattr(custom_sources, "create_provider", create_provider)
    config = CustomSourceIn(
        name="draft",
        datasets={
            "daily": DatasetConfigIn(url="https://unfinished.test"),
            "realtime": DatasetConfigIn(url="https://example.test/realtime"),
        },
    )

    result = run_data_source_test(CustomSourceTestIn(
        provider="draft",
        dataset="realtime",
        config=config,
    ))

    assert result["provider"] == "draft"
    tested = create_provider.call_args.args[0]
    assert list(tested["datasets"]) == ["realtime"]
    provider.test_dataset.assert_called_once_with("realtime", None)
    provider.close.assert_called_once_with()


def test_data_source_trial_wraps_missing_saved_provider_as_http_400(monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(
        custom_sources,
        "get_provider",
        Mock(side_effect=ValueError("not found")),
    )

    with pytest.raises(HTTPException) as exc_info:
        run_data_source_test(CustomSourceTestIn(provider="missing", dataset="daily"))

    assert exc_info.value.status_code == 400
    assert "not found" in exc_info.value.detail
