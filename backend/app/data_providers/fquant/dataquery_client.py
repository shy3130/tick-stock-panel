"""Typed client for qx-data-server's versioned, bounded dataquery API.

This module knows only the public HTTP contract. It never resolves engine files,
catalog layouts, table names, SQL, or fallback storage.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

DATAQUERY_BASE_URL = os.getenv(
    "FQUANT_DATAQUERY_BASE_URL", "http://127.0.0.1:8099"
).rstrip("/")
DATAQUERY_TIMEOUT_SECONDS = float(os.getenv("FQUANT_DATAQUERY_TIMEOUT_S", "5"))
MAX_DATAQUERY_ROWS = 2500
MAX_POINT_SYMBOLS = 16

DataQueryErrorCode = Literal[
    "invalid_query",
    "not_found",
    "stale",
    "incomplete",
    "schema_mismatch",
    "version_pinned_unavailable",
    "unavailable",
]

_ERROR_STATUS: dict[str, int] = {
    "invalid_query": 400,
    "not_found": 404,
    "schema_mismatch": 409,
    "version_pinned_unavailable": 409,
    "stale": 503,
    "incomplete": 503,
    "unavailable": 503,
}
_RETRYABLE_ERRORS = frozenset({"stale", "incomplete", "unavailable"})
_SERIES_DATASETS = {
    "day": ("tdx_day/a", "1d"),
    "wide": ("tdx_wide/a", "1d"),
    "minutes": ("tdx_minutes/a", "1m"),
    "trans": ("tdx_trans/a", "tick"),
    "xdxr": ("tdx_xdxr/a", "event"),
}
_EXPECTED_SCHEMAS = {
    "tdx_day/a": "legacy_csv/v1",
    "tdx_wide/a": "legacy_csv/v1",
    "tdx_minutes/a": "legacy_csv/v1",
    "tdx_trans/a": "legacy_csv/v1",
    "tdx_xdxr/a": "legacy_csv/v1",
    "tdx_moneyflow/a": "tdx_moneyflow/v1",
    "tdx_moneyflow_minute/a": "tdx_moneyflow_minute/v1",
}
_CACHE_ID_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-?\d{2}-?\d{2}$")


class DataQueryError(RuntimeError):
    """Stable dataquery failure exposed without backend implementation details."""

    def __init__(
        self,
        code: DataQueryErrorCode,
        *,
        dataset: str = "",
        message: str,
        retryable: bool | None = None,
        http_status: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.dataset = dataset
        self.message = message
        self.retryable = code in _RETRYABLE_ERRORS if retryable is None else retryable
        self.http_status = http_status or _ERROR_STATUS[code]
        self.__cause__ = cause

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "dataset": self.dataset,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


class DataQueryBlockedError(DataQueryError):
    """A bulk or consistency-sensitive read awaiting engine #9/#11."""

    def __init__(self, operation: str, *, dataset: str = "") -> None:
        super().__init__(
            "version_pinned_unavailable",
            dataset=dataset,
            message=(
                f"{operation} is blocked until engine #9/#11 provide the required "
                "pinned Parquet bundle; dataquery v2 currently supports only point "
                "and bounded narrow-range reads"
            ),
            retryable=False,
        )


@dataclass(frozen=True)
class DataVersion:
    backend: str
    generation: str
    schema_version: str
    checksum: str
    source_watermark: str
    coverage: str
    stage: str
    reconciled: bool
    degraded: bool
    freshness: str

    @classmethod
    def parse(cls, payload: object, *, dataset: str) -> DataVersion:
        if not isinstance(payload, Mapping):
            raise _contract_error(dataset, "dataquery response omits version metadata")
        required = {"backend", "schema_version", "coverage", "stage", "reconciled", "degraded"}
        if not required.issubset(payload):
            raise _contract_error(dataset, "dataquery version metadata is incomplete")
        backend = payload.get("backend")
        schema_version = payload.get("schema_version")
        if not isinstance(backend, str) or not backend:
            raise _contract_error(dataset, "dataquery version backend is missing")
        expected_schema = _EXPECTED_SCHEMAS.get(dataset)
        if not isinstance(schema_version, str) or schema_version != expected_schema:
            raise _contract_error(dataset, "dataquery schema version is incompatible")
        generation = _string(payload.get("generation"))
        source_watermark = _string(payload.get("source_watermark"))
        if not generation and not source_watermark:
            raise _contract_error(dataset, "dataquery version has no immutable identity or watermark")
        coverage = _string(payload.get("coverage"))
        stage = _string(payload.get("stage"))
        if not coverage or not stage:
            raise _contract_error(dataset, "dataquery version coverage or stage is missing")
        if type(payload.get("reconciled")) is not bool or type(payload.get("degraded")) is not bool:
            raise _contract_error(dataset, "dataquery version flags are invalid")
        return cls(
            backend=backend,
            generation=generation,
            schema_version=schema_version,
            checksum=_string(payload.get("checksum")),
            source_watermark=source_watermark,
            coverage=coverage,
            stage=stage,
            reconciled=payload["reconciled"],
            degraded=payload["degraded"],
            freshness=_string(payload.get("freshness")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataQueryResult:
    dataset: str
    rows: tuple[dict[str, Any], ...]
    version: DataVersion
    truncated: bool = False


class DataQueryClient:
    """Synchronous, no-retry client for the bounded v2 query surface."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DATAQUERY_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or DATAQUERY_BASE_URL).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("FQUANT_DATAQUERY_BASE_URL must be an http(s) origin")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=False,
        )
        self._owns_client = client is None
        self._versions: dict[str, DataVersion] = {}
        self._versions_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def observed_versions(self) -> dict[str, dict[str, Any]]:
        with self._versions_lock:
            return {dataset: version.to_dict() for dataset, version in self._versions.items()}

    def status(self) -> dict[str, Any]:
        payload = self._get("/api/v2/dataquery/status")
        routes = payload.get("routes")
        if payload.get("status") != "ok" or not isinstance(routes, list):
            raise _contract_error("", "dataquery status response is invalid")
        for route in routes:
            if not isinstance(route, Mapping) or not isinstance(route.get("dataset"), str):
                raise _contract_error("", "dataquery status route metadata is invalid")
            for field in (
                "backend",
                "schema_version",
                "generation",
                "source_watermark",
                "coverage",
                "stage",
                "reconciled",
                "degraded",
                "error_code",
                "version",
            ):
                if field not in route:
                    raise _contract_error(str(route.get("dataset") or ""), "dataquery status metadata is incomplete")
        return {"status": "ok", "routes": [dict(route) for route in routes]}

    def series(
        self,
        name: Literal["day", "wide", "minutes", "trans", "xdxr"],
        cache_id: str,
        *,
        date: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        tail: bool | None = None,
        fields: Sequence[str] | None = None,
    ) -> DataQueryResult:
        if name not in _SERIES_DATASETS:
            raise ValueError(f"unsupported dataquery series: {name}")
        dataset, period = _SERIES_DATASETS[name]
        _validate_cache_id(cache_id, dataset)
        _validate_limit(limit, dataset)
        params: dict[str, Any] = {
            "asset": "stock",
            "market": "a",
            "fq": "none",
            "period": period,
            "limit": limit,
        }
        if tail is not None:
            params["tail"] = str(tail).lower()
        if fields:
            if len(fields) > 32 or any(not isinstance(field, str) or not field for field in fields):
                raise _invalid_query(dataset, "dataquery fields projection is invalid")
            params["fields"] = ",".join(fields)
        if name in {"minutes", "trans"}:
            if date is None or start is not None or end is not None:
                raise _invalid_query(dataset, f"{name} requires exactly one date")
            params["date"] = _validate_date(date, dataset)
        else:
            if date is not None:
                raise _invalid_query(dataset, f"{name} accepts start/end, not date")
            if start is not None:
                params["start"] = _validate_date(start, dataset)
            if end is not None:
                params["end"] = _validate_date(end, dataset)
            if start is not None and end is not None and _compact_date(start) > _compact_date(end):
                raise _invalid_query(dataset, "start must be on or before end")
        payload = self._get(f"/api/v2/dataquery/series/{name}/{cache_id}", params=params)
        if payload.get("dataset") != dataset or payload.get("cache_id") != cache_id:
            raise _contract_error(dataset, "dataquery series identity does not match the request")
        rows = _rows(payload.get("rows"), dataset)
        if payload.get("count") != len(rows) or type(payload.get("truncated")) is not bool:
            raise _contract_error(dataset, "dataquery series count metadata is invalid")
        if payload["truncated"]:
            # v2 has no cursor paging; a capped series is incomplete coverage and
            # must surface as a typed error, never as a complete-looking result.
            raise DataQueryError(
                "incomplete",
                dataset=dataset,
                message=(
                    f"dataquery series {name} hit the {limit}-row cap without paging; "
                    "narrow the date window or wait for the pinned bundle (engine #11)"
                ),
                retryable=True,
            )
        version = DataVersion.parse(payload.get("version"), dataset=dataset)
        self._observe(dataset, version)
        return DataQueryResult(dataset, tuple(rows), version, payload["truncated"])

    def daily_moneyflow_point(self, cache_id: str, date: str) -> DataQueryResult:
        dataset = "tdx_moneyflow/a"
        _validate_cache_id(cache_id, dataset)
        payload = self._get(
            f"/api/v2/dataquery/moneyflow/daily/stocks/{cache_id}",
            params={
                "date": _validate_date(date, dataset),
                "asset": "stock",
                "market": "a",
                "fq": "none",
                "period": "1d",
            },
        )
        if payload.get("dataset") != dataset or payload.get("cache_id") != cache_id:
            raise _contract_error(dataset, "dataquery moneyflow identity does not match the request")
        total = payload.get("total")
        if not isinstance(total, Mapping):
            raise _contract_error(dataset, "dataquery daily moneyflow payload is invalid")
        version = DataVersion.parse(payload.get("version"), dataset=dataset)
        self._observe(dataset, version)
        return DataQueryResult(dataset, (dict(total),), version)

    def minute_moneyflow_point(self, cache_id: str, date: str) -> DataQueryResult:
        dataset = "tdx_moneyflow_minute/a"
        _validate_cache_id(cache_id, dataset)
        payload = self._get(
            f"/api/v2/dataquery/moneyflow/minute/stocks/{cache_id}",
            params={
                "date": _validate_date(date, dataset),
                "asset": "stock",
                "market": "a",
                "fq": "none",
                "period": "1m",
            },
        )
        if payload.get("dataset") != dataset or payload.get("cache_id") != cache_id:
            raise _contract_error(dataset, "dataquery moneyflow identity does not match the request")
        rows = _rows(payload.get("records"), dataset)
        if payload.get("count") != len(rows):
            raise _contract_error(dataset, "dataquery minute moneyflow count is invalid")
        version = DataVersion.parse(payload.get("version"), dataset=dataset)
        self._observe(dataset, version)
        return DataQueryResult(dataset, tuple(rows), version)

    def _observe(self, dataset: str, version: DataVersion) -> None:
        with self._versions_lock:
            self._versions[dataset] = version

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DataQueryError(
                "unavailable",
                message=(
                    "dataquery backend is unreachable; check FQUANT_DATAQUERY_BASE_URL "
                    "or set FQUANT_DATAQUERY_ENABLED=0 to fall back to the legacy chain"
                ),
                retryable=True,
                cause=exc,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataQueryError(
                "unavailable",
                message="dataquery backend returned an invalid response",
                retryable=True,
                cause=exc,
            ) from exc
        if response.status_code >= 400:
            raise _server_error(payload, response.status_code)
        if not isinstance(payload, dict):
            raise _contract_error("", "dataquery response must be a JSON object")
        return payload


def _server_error(payload: object, status_code: int) -> DataQueryError:
    body = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(body, Mapping):
        return DataQueryError(
            "unavailable",
            message="dataquery backend returned an invalid error response",
            retryable=True,
        )
    code = body.get("code")
    if code not in _ERROR_STATUS:
        return DataQueryError(
            "unavailable",
            message="dataquery backend returned an unknown error code",
            retryable=True,
        )
    message = body.get("message")
    dataset = body.get("dataset")
    retryable = body.get("retryable")
    if not isinstance(message, str) or not message or not isinstance(retryable, bool):
        return DataQueryError(
            "unavailable",
            message="dataquery backend returned an invalid error response",
            retryable=True,
        )
    return DataQueryError(
        code,
        dataset=dataset if isinstance(dataset, str) else "",
        message=message,
        retryable=retryable,
        http_status=_ERROR_STATUS[code],
    )


def _rows(value: object, dataset: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise _contract_error(dataset, "dataquery rows payload is invalid")
    return [dict(row) for row in value]


def _contract_error(dataset: str, message: str) -> DataQueryError:
    return DataQueryError(
        "schema_mismatch",
        dataset=dataset,
        message=message,
        retryable=False,
    )


def _invalid_query(dataset: str, message: str) -> DataQueryError:
    return DataQueryError(
        "invalid_query",
        dataset=dataset,
        message=message,
        retryable=False,
    )


def _validate_cache_id(cache_id: str, dataset: str) -> None:
    if not _CACHE_ID_RE.fullmatch(cache_id):
        raise _invalid_query(dataset, "cache_id must be a canonical A-share cache id")


def _validate_limit(limit: int, dataset: str) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_DATAQUERY_ROWS:
        raise _invalid_query(dataset, f"limit must be between 1 and {MAX_DATAQUERY_ROWS}")


def _validate_date(value: str, dataset: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise _invalid_query(dataset, "date must be YYYYMMDD or YYYY-MM-DD")
    compact = _compact_date(value)
    from datetime import datetime

    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise _invalid_query(dataset, "date must be a valid calendar date") from exc
    return compact


def _compact_date(value: str) -> str:
    return value.replace("-", "")


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
