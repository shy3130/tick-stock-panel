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


def test_full_minute_dataset_survives_config_and_registration(tmp_path: Path, monkeypatch):
    """#451 回归: loader 清洗白名单含 full_minute, 而 config_from_dict 的白名单
    漏了它 —— YAML 里的 full_minute 在配置模型构建阶段被静默丢弃, 表现为
    provider_has_dataset 为 False、设置页试拉报 "does not configure dataset
    'full_minute'"、分钟全量批量同步链路 (_resolve_full_minute_provider) 静默失效。
    """
    from app.data_providers.custom import loader as custom_loader

    full_minute_fields = (
        "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
    )
    raw = {
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {
            "full_minute": {
                "url": "https://example.test/full_minute",
                "method": "POST",
                "field_map": {name: name for name in full_minute_fields},
            },
        },
    }
    # 编辑器保存路径的前半段: 清洗白名单本来就保留 full_minute
    cleaned = _sanitize_for_yaml(raw)
    assert "full_minute" in cleaned["datasets"]

    # 启动加载路径: YAML 文件 → load_config → config_from_dict
    path = tmp_path / "test_source.yaml"
    path.write_text(
        "name: test_source\n"
        "display_name: Test Source\n"
        "datasets:\n"
        "  full_minute:\n"
        "    url: https://example.test/full_minute\n"
        "    method: POST\n"
        "    field_map:\n"
        + "".join(f"      {n}: {n}\n" for n in full_minute_fields),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert "full_minute" in cfg.datasets

    # 注册表: 真实 load_all 后 provider_has_dataset 必须为 True
    saved_providers = dict(custom_loader._PROVIDERS)
    saved_errors = list(custom_loader._LOAD_ERRORS)
    monkeypatch.setattr(custom_loader, "_load_builtin_plugins", lambda: None)
    try:
        custom_loader.load_all(tmp_path)
        assert not custom_loader._LOAD_ERRORS, custom_loader._LOAD_ERRORS
        assert custom_loader.provider_has_dataset("test_source", "full_minute")
    finally:
        custom_loader._PROVIDERS.clear()
        custom_loader._PROVIDERS.update(saved_providers)
        custom_loader._LOAD_ERRORS[:] = saved_errors


# ================================================================
# amount 可空契约 + 保存先验后存 (用户反馈: 自定义源配全量分钟"保存不好使")
# ================================================================

def _minute_dataset(*, drop_targets: tuple[str, ...] = ()) -> dict:
    """minute/full_minute 形态的数据集配置; drop_targets 指定不映射的内部字段。"""
    field_map = {
        "code": "symbol", "ts": "datetime",
        "o": "open", "h": "high", "lo": "low", "c": "close", "v": "volume",
    }
    field_map = {k: v for k, v in field_map.items() if v not in drop_targets}
    return {
        "url": "https://example.test/minutes",
        "method": "POST",
        "response_path": "data",
        "field_map": field_map,
        "symbols_param": "codes",
    }


def test_full_minute_without_amount_is_valid():
    """契约: 分钟 amount(成交额)可空 — 缺成交额的源合法, 均价线降级显示 —。

    修复前: _REQUIRED 把 amount 列为必填 → validate 报 missing mapped fields,
    整个源被 load_all 拒绝, 用户保存后源从列表消失("保存不好使")。
    """
    from app.data_providers.custom import loader

    provider = loader.create_provider({
        "name": "fm_no_amount", "display_name": "fm",
        "datasets": {"full_minute": _minute_dataset(drop_targets=("amount",))},
    })
    try:
        assert not provider.validate(), provider.validate()
        assert "full_minute" in provider.config.datasets
    finally:
        provider.close()


def test_minute_without_amount_is_valid():
    """minute 数据集同契约: amount 不映射也合法。"""
    from app.data_providers.custom import loader

    provider = loader.create_provider({
        "name": "m_no_amount", "display_name": "m",
        "datasets": {"minute": _minute_dataset(drop_targets=("amount",))},
    })
    try:
        assert not provider.validate(), provider.validate()
    finally:
        provider.close()


def test_minute_missing_core_field_still_rejected():
    """必填集仍生效: 缺 volume(核心量字段)依旧拒绝, 不能矫枉过正。"""
    from app.data_providers.custom import loader

    with pytest.raises(ValueError, match="volume"):
        loader.create_provider({
            "name": "m_no_volume", "display_name": "m",
            "datasets": {"minute": _minute_dataset(drop_targets=("volume",))},
        })


def test_save_data_source_validates_before_write(monkeypatch, tmp_path):
    """保存先验后存: 校验不过 400 + 不落盘; 合法配置才写 YAML 并注册。

    修复前: save_config 先写 YAML, load_all 校验失败仅记 _LOAD_ERRORS,
    源从列表静默消失, 前端还弹"已保存"成功提示。
    """
    from app.api.settings import save_data_source
    from app.data_providers.custom import loader

    monkeypatch.setattr(loader, "data_sources_dir", lambda: tmp_path)
    saved_providers = dict(loader._PROVIDERS)
    saved_status = dict(loader._PLUGIN_STATUS)
    monkeypatch.setattr(loader, "_load_builtin_plugins", lambda: None)
    try:
        # 合法: full_minute 缺 amount 映射 → 保存成功且源注册
        ok = CustomSourceIn(
            name="fm_ok",
            datasets={"full_minute": DatasetConfigIn(**_minute_dataset())},
        )
        save_data_source(ok)
        assert (tmp_path / "fm_ok.yaml").exists()
        assert loader.provider_has_dataset("fm_ok", "full_minute")

        # 非法: 缺 volume → HTTP 400 带原因, 且不写 YAML
        bad = CustomSourceIn(
            name="fm_bad",
            datasets={"full_minute": DatasetConfigIn(**_minute_dataset(drop_targets=("volume",)))},
        )
        with pytest.raises(HTTPException) as ei:
            save_data_source(bad)
        assert ei.value.status_code == 400
        assert "volume" in str(ei.value.detail)
        assert not (tmp_path / "fm_bad.yaml").exists()
    finally:
        loader._PROVIDERS.clear()
        loader._PROVIDERS.update(saved_providers)
        loader._PLUGIN_STATUS.clear()
        loader._PLUGIN_STATUS.update(saved_status)
