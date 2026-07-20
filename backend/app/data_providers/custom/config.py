"""Custom HTTP data source configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

DatasetName = Literal["daily", "adj_factor", "realtime", "minute", "financial"]


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
    timeout: float = 30.0
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


@dataclass(frozen=True)
class CustomSourceConfig:
    name: str
    display_name: str
    auth: AuthConfig = field(default_factory=AuthConfig)
    datasets: dict[str, DatasetConfig] = field(default_factory=dict)
    path: Path | None = None

    def has_dataset(self, name: DatasetName) -> bool:
        return name in self.datasets


def _auth_from_dict(raw: dict[str, Any] | None) -> AuthConfig:
    raw = raw or {}
    return AuthConfig(
        type=str(raw.get("type", "none") or "none").lower(),
        token_env=raw.get("token_env"),
        header=str(raw.get("header", "Authorization") or "Authorization"),
        param=str(raw.get("param", "token") or "token"),
    )


def _dataset_from_dict(raw: dict[str, Any]) -> DatasetConfig:
    return DatasetConfig(
        url=str(raw.get("url", "") or ""),
        method=str(raw.get("method", "GET") or "GET").upper(),
        batch=int(raw["batch"]) if raw.get("batch") is not None else None,
        rpm=int(raw["rpm"]) if raw.get("rpm") is not None else None,
        timeout=float(raw.get("timeout", 30.0) or 30.0),
        response_path=str(raw.get("response_path", "") or ""),
        field_map={str(k): str(v) for k, v in (raw.get("field_map") or {}).items()},
        transforms={str(k): str(v) for k, v in (raw.get("transforms") or {}).items()},
        params=dict(raw.get("params") or {}),
        body=dict(raw.get("body") or {}),
        symbols_param=str(raw.get("symbols_param", "symbols") or "symbols"),
        start_param=str(raw.get("start_param", "start_time") or "start_time"),
        end_param=str(raw.get("end_param", "end_time") or "end_time"),
        asset_type_param=str(raw.get("asset_type_param")) if raw.get("asset_type_param") else None,
        freq_param=str(raw.get("freq_param")) if raw.get("freq_param") else None,
    )


def load_config(path: Path) -> CustomSourceConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    datasets = {
        name: _dataset_from_dict(cfg)
        for name, cfg in (raw.get("datasets") or {}).items()
        if name in {"daily", "adj_factor", "realtime", "minute", "financial"} and isinstance(cfg, dict)
    }
    name = str(raw.get("name", path.stem) or path.stem).lower()
    return CustomSourceConfig(
        name=name,
        display_name=str(raw.get("display_name", name) or name),
        auth=_auth_from_dict(raw.get("auth")),
        datasets=datasets,
        path=path,
    )
