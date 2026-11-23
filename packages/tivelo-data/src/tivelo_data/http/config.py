"""Custom HTTP data source configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DatasetName = Literal[
    "daily", "adj_factor", "realtime", "minute", "depth5", "full_minute", "financial"
]
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
CN_RAW_DAILY_PROFILE = "cn.equity.regular.raw.lot100.v1"
CN_EX_FACTOR_PROFILE = "cn.equity.ex-factor.events.v1"
CN_RAW_MINUTE_PROFILES = {
    "cn.equity.regular.1m.raw.lot100.start.v1": "start",
    "cn.equity.regular.1m.raw.lot100.end.v1": "end",
}
MINUTE_PRICES = ("open", "high", "low", "close")
MINUTE_NUMERIC_SCALES = {"value * 100": 2, "value / 100": -2, "value / 10000": -4}


@dataclass(frozen=True)
class AuthConfig:
    type: str = "none"
    token_env: str | None = None
    header: str = "Authorization"
    param: str = "token"


@dataclass(frozen=True)
class DatasetConfig:
    url: str
    method: str = "GET"
    batch: int | None = None
    rpm: int | None = None
    timeout: float = DEFAULT_TIMEOUT
    response_path: str = ""
    field_map: dict[str, str] = field(default_factory=dict)
    transforms: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    symbols_param: str = "symbols"
    start_param: str = "start_time"
    end_param: str = "end_time"
    asset_type_param: str | None = None
    freq_param: str | None = None
    # realtime 比例字段(change_pct/amplitude/turnover_rate)的单位声明:
    # "percent"(返回 3.66 表示 3.66%)或 "decimal"(返回 0.0366 表示 3.66%)。
    pct_unit: str | None = None
    research_profile: str | None = None


@dataclass(frozen=True)
class CustomSourceConfig:
    name: str
    display_name: str
    auth: AuthConfig = field(default_factory=AuthConfig)
    datasets: dict[str, DatasetConfig] = field(default_factory=dict)
    path: Path | None = None

    def has_dataset(self, name: DatasetName) -> bool:
        return name in self.datasets


def minute_mapping_error(dataset: DatasetConfig, auth: AuthConfig) -> str | None:
    """Validate the declared minute endpoint before the first request."""
    fields = set(dataset.field_map.values())
    if not {"symbol", "datetime", "volume", *MINUTE_PRICES} <= fields:
        return "Minute source requires symbol, datetime, OHLC and volume"
    if len(fields) != len(dataset.field_map):
        return "Minute source has duplicate mapped fields"
    if not dataset.url or dataset.method not in ("GET", "POST"):
        return "Minute source requires a GET or POST endpoint"
    if any(limit is not None and limit < 1 for limit in (dataset.batch, dataset.rpm)):
        return "Minute batch and rate limits must be positive"
    names = [dataset.symbols_param, dataset.start_param, dataset.end_param]
    names.extend(key for key in (dataset.asset_type_param, dataset.freq_param) if key)
    if auth.type == "query":
        names.append(auth.param)
    if len(names) != len(set(names)):
        return "Minute scope, frequency, asset and authentication parameters must be distinct"
    for field_name, expression in dataset.transforms.items():
        if (field_name not in fields or field_name not in (*MINUTE_PRICES, "volume", "amount")
                or expression.strip() not in MINUTE_NUMERIC_SCALES):
            return "Minute research transforms support numeric units only; supply local ISO timestamps"
    return None


def _auth_from_dict(raw: dict[str, Any] | None) -> AuthConfig:
    raw = raw or {}
    return AuthConfig(
        type=str(raw.get("type", "none") or "none").lower(),
        token_env=raw.get("token_env"),
        header=str(raw.get("header", "Authorization") or "Authorization"),
        param=str(raw.get("param", "token") or "token"),
    )


def _dataset_from_dict(raw: dict[str, Any]) -> DatasetConfig:
    timeout_raw = raw.get("timeout")
    if timeout_raw is None:
        timeout = DEFAULT_TIMEOUT
    else:
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"timeout must be a number between 0 and {MAX_TIMEOUT:g} seconds"
            ) from e
        if not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT:g} seconds")

    pct_unit = str(raw.get("pct_unit") or "").strip().lower() or None
    if pct_unit not in (None, "percent", "decimal"):
        raise ValueError(f"pct_unit must be 'percent' or 'decimal', got {pct_unit!r}")

    return DatasetConfig(
        url=str(raw.get("url", "") or ""),
        method=str(raw.get("method", "GET") or "GET").upper(),
        batch=int(raw["batch"]) if raw.get("batch") is not None else None,
        rpm=int(raw["rpm"]) if raw.get("rpm") is not None else None,
        timeout=timeout,
        response_path=str(raw.get("response_path", "") or ""),
        field_map={str(k): str(v) for k, v in (raw.get("field_map") or {}).items()},
        transforms={str(k): str(v) for k, v in (raw.get("transforms") or {}).items()},
        params=dict(raw.get("params") or {}),
        body=dict(raw.get("body") or {}),
        symbols_param=str(raw.get("symbols_param", "symbols") or "symbols").strip() or "symbols",
        start_param=str(raw.get("start_param", "start_time") or "start_time").strip()
        or "start_time",
        end_param=str(raw.get("end_param", "end_time") or "end_time").strip() or "end_time",
        asset_type_param=(str(raw.get("asset_type_param") or "").strip() or None),
        freq_param=(str(raw.get("freq_param") or "").strip() or None),
        pct_unit=pct_unit,
        research_profile=raw.get("research_profile"),
    )


def config_from_dict(raw: dict[str, Any], path: Path | None = None) -> CustomSourceConfig:
    datasets = {
        name: _dataset_from_dict(cfg)
        for name, cfg in (raw.get("datasets") or {}).items()
        if name
        in {"daily", "adj_factor", "realtime", "minute", "depth5", "full_minute", "financial"}
        and isinstance(cfg, dict)
    }
    default_name = path.stem if path else "preview"
    name = str(raw.get("name", default_name) or default_name).lower()
    return CustomSourceConfig(
        name=name,
        display_name=str(raw.get("display_name", name) or name),
        auth=_auth_from_dict(raw.get("auth")),
        datasets=datasets,
        path=path,
    )


def load_config(path: Path) -> CustomSourceConfig:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return config_from_dict(raw, path)
