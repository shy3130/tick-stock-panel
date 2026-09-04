"""dataquery_client 的 v2 公共契约测试:注入假 httpx 客户端,零网络。

覆盖:v2 序列点查/有界区间序列化、日期与缓存校验、七类公共错误码映射、
错误信封 fail-closed、版本元数据解析与 schema 校验、status 元数据校验、
observed_versions,以及"绝不发出 /api/v1 或回退请求"。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.data_providers.fquant.dataquery_client import (
    MAX_DATAQUERY_ROWS,
    DataQueryClient,
    DataQueryError,
    DataVersion,
)


# ---------------------------------------------------------------------------
# fakes


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeClient:
    """Records every request; never touches the network."""

    def __init__(
        self,
        response: FakeResponse | list[FakeResponse] | None = None,
        *,
        transport_error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse({})
        self.transport_error = transport_error
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> FakeResponse:
        self.requests.append((path, dict(params) if params else None))
        if self.transport_error is not None:
            raise self.transport_error
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


def make_client(
    payload: Any,
    *,
    status_code: int = 200,
    transport_error: Exception | None = None,
    json_error: Exception | None = None,
) -> tuple[DataQueryClient, FakeClient]:
    fake = FakeClient(
        FakeResponse(payload, status_code=status_code, json_error=json_error),
        transport_error=transport_error,
    )
    client = DataQueryClient("http://dataquery.test", client=fake)
    return client, fake


# ---------------------------------------------------------------------------
# payload builders


def version_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": "tdx-local",
        "generation": "g-20260903",
        "schema_version": "legacy_csv/v1",
        "checksum": "abc123",
        "source_watermark": "20260903",
        "coverage": "full-a",
        "stage": "published",
        "reconciled": True,
        "degraded": False,
        "freshness": "2026-09-03T15:00:00+08:00",
    }
    payload.update(overrides)
    for key, value in list(payload.items()):
        if value is _MISSING:
            del payload[key]
    return payload


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<MISSING>"


_MISSING = _Missing()

def series_payload(
    dataset: str = "tdx_day/a",
    cache_id: str = "sh600519",
    *,
    rows: list[dict[str, Any]] | Any | None = None,
    version: Any = None,
    **overrides: Any,
) -> dict[str, Any]:
    if rows is None:
        rows = [{"code": "sh600519", "close": 1.0}]
    payload: dict[str, Any] = {
        "dataset": dataset,
        "cache_id": cache_id,
        "rows": rows,
        "count": len(rows) if isinstance(rows, list) else 0,
        "truncated": False,
        "version": version if version is not None else version_payload(),
    }
    payload.update(overrides)
    for key, value in list(payload.items()):
        if value is _MISSING:
            del payload[key]
    return payload


def minutes_payload(cache_id: str = "sz000001") -> dict[str, Any]:
    return series_payload(
        dataset="tdx_minutes/a",
        cache_id=cache_id,
        version=version_payload(),
    )


def error_envelope(code: str, *, message: str = "boom", retryable: bool = False, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    body.update(extra)
    return {"error": body}


# ---------------------------------------------------------------------------
# serialization: point and bounded-range queries


def test_series_bounded_range_serializes_v2_params():
    client, fake = make_client(series_payload())
    result = client.series("day", "sh600519", start="2026-01-05", end="20260110", limit=50)

    assert len(fake.requests) == 1
    path, params = fake.requests[0]
    assert path == "/api/v2/dataquery/series/day/sh600519"
    assert params == {
        "asset": "stock",
        "market": "a",
        "fq": "none",
        "period": "1d",
        "limit": 50,
        "start": "20260105",
        "end": "20260110",
    }
    assert result.dataset == "tdx_day/a"
    assert result.rows == ({"code": "sh600519", "close": 1.0},)
    assert result.truncated is False
    assert result.version.backend == "tdx-local"


def test_series_point_query_serializes_tail_fields_and_date():
    client, fake = make_client(minutes_payload())
    client.series(
        "minutes",
        "sz000001",
        date="2026-09-03",
        tail=True,
        fields=["time", "price"],
        limit=100,
    )

def test_moneyflow_daily_point_serializes_v2_route():
    version = version_payload(schema_version="tdx_moneyflow/v1")
    payload = {
        "dataset": "tdx_moneyflow/a",
        "cache_id": "sh600519",
        "total": {"main": 1},
        "version": version,
    }
    client, fake = make_client(payload)
    result = client.daily_moneyflow_point("sh600519", "2026-09-03")

    path, params = fake.requests[0]
    assert path == "/api/v2/dataquery/moneyflow/daily/stocks/sh600519"
    assert params["date"] == "20260903"
    assert params["period"] == "1d"
    assert result.rows == ({"main": 1},)


def test_moneyflow_minute_point_serializes_v2_route():
    version = version_payload(schema_version="tdx_moneyflow_minute/v1")
    payload = {
        "dataset": "tdx_moneyflow_minute/a",
        "cache_id": "sh600519",
        "records": [{"time": "0930", "inflow": 5}],
        "count": 1,
        "version": version,
    }
    client, fake = make_client(payload)
    result = client.minute_moneyflow_point("sh600519", "20260903")

    path, params = fake.requests[0]
    assert path == "/api/v2/dataquery/moneyflow/minute/stocks/sh600519"
    assert params["date"] == "20260903"
    assert params["period"] == "1m"
    assert result.rows == ({"time": "0930", "inflow": 5},)


# ---------------------------------------------------------------------------
# client-side validation


def test_invalid_dates_rejected_before_any_request():
    client, fake = make_client(series_payload())
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519", start="2026/01/05")
    assert exc.value.code == "invalid_query"
    with pytest.raises(DataQueryError):
        client.series("day", "sh600519", end="20260230")  # not a calendar date
    with pytest.raises(DataQueryError):
        client.series("day", "sh600519", start="20260110", end="20260101")  # start > end
    with pytest.raises(DataQueryError):
        client.series("day", "sh600519", date="20260101")  # day series rejects date
    with pytest.raises(DataQueryError):
        client.series("minutes", "sh600519", date="20260101", end="20260102")
    with pytest.raises(DataQueryError):
        client.series("minutes", "sh600519")  # point series require exactly one date
    assert fake.requests == []


def test_cache_id_and_limit_validation():
    client, fake = make_client(series_payload())
    for cache_id in ("600519", "xx600519", "sh60051", "SH600519", ""):
        with pytest.raises(DataQueryError) as exc:
            client.series("day", cache_id)
        assert exc.value.code == "invalid_query"
        assert exc.value.http_status == 400
    for limit in (0, -1, MAX_DATAQUERY_ROWS + 1, True, 1.5, "10"):
        with pytest.raises(DataQueryError):
            client.series("day", "sh600519", limit=limit)
    assert fake.requests == []


def test_fields_projection_validation():
    client, fake = make_client(series_payload())
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519", fields=[f"f{i}" for i in range(33)])
    assert exc.value.code == "invalid_query"
    with pytest.raises(DataQueryError):
        client.series("day", "sh600519", fields=["close", ""])
    assert fake.requests == []


def test_unsupported_series_name_is_value_error():
    client, fake = make_client(series_payload())
    with pytest.raises(ValueError):
        client.series("tick", "sh600519")
    assert fake.requests == []


# ---------------------------------------------------------------------------
# typed HTTP error mapping: every public code


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_query", 400),
        ("not_found", 404),
        ("stale", 503),
        ("incomplete", 503),
        ("schema_mismatch", 409),
        ("version_pinned_unavailable", 409),
        ("unavailable", 503),
    ],
)
def test_every_public_error_code_maps_typed(code, status):
    body_retryable = code in {"stale", "incomplete", "unavailable"}
    client, fake = make_client(
        error_envelope(code, message=f"server says {code}", retryable=body_retryable, dataset="tdx_day/a"),
        status_code=status,
    )
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519")
    err = exc.value
    assert err.code == code
    assert err.message == f"server says {code}"
    assert err.dataset == "tdx_day/a"
    assert err.http_status == status
    assert err.retryable is body_retryable
    assert fake.paths == ["/api/v2/dataquery/series/day/sh600519"]


# ---------------------------------------------------------------------------
# invalid error envelope: fail closed


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"message": "no error key"},
        {"error": "not-a-mapping"},
        {"error": {"code": "mystery_code", "message": "x", "retryable": True}},
        {"error": {"code": "stale", "retryable": True}},  # message missing
        {"error": {"code": "stale", "message": "", "retryable": True}},  # empty message
        {"error": {"code": "stale", "message": "x", "retryable": "yes"}},  # non-bool retryable
    ],
)
def test_invalid_error_envelopes_fail_closed(payload):
    client, _ = make_client(payload, status_code=503)
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519")
    assert exc.value.code == "unavailable"
    assert exc.value.retryable is True


def test_transport_and_malformed_json_map_to_unavailable():
    client, _ = make_client(
        {},
        transport_error=httpx.ConnectError("refused"),
    )
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519")
    assert exc.value.code == "unavailable"
    assert exc.value.retryable is True
    assert isinstance(exc.value.__cause__, httpx.ConnectError)

    client2, _ = make_client({}, json_error=ValueError("not json"))
    with pytest.raises(DataQueryError) as exc2:
        client2.series("day", "sh600519")
    assert exc2.value.code == "unavailable"
    assert exc2.value.retryable is True


# ---------------------------------------------------------------------------
# response contract


def test_series_response_contract_mismatches_are_schema_mismatch():
    good = series_payload()
    cases = [
        series_payload(dataset="tdx_wide/a"),  # identity mismatch
        series_payload(cache_id="sz000001"),
        series_payload(count=99),
        series_payload(truncated="no"),
        series_payload(rows={"not": "a list"}),
        series_payload(rows=[["not-a-mapping"]]),
    ]
    for payload in cases:
        client, _ = make_client(payload)
        with pytest.raises(DataQueryError) as exc:
            client.series("day", "sh600519")
        assert exc.value.code == "schema_mismatch"
        assert exc.value.retryable is False
    # sanity: the good payload itself passes
    client, _ = make_client(good)
    client.series("day", "sh600519")


# ---------------------------------------------------------------------------
# version metadata


def test_version_metadata_parses_all_fields():
    client, _ = make_client(series_payload())
    result = client.series("day", "sh600519")
    v = result.version
    assert isinstance(v, DataVersion)
    assert v.backend == "tdx-local"
    assert v.generation == "g-20260903"
    assert v.schema_version == "legacy_csv/v1"
    assert v.checksum == "abc123"
    assert v.source_watermark == "20260903"
    assert v.coverage == "full-a"
    assert v.stage == "published"
    assert v.reconciled is True
    assert v.degraded is False
    assert v.freshness == "2026-09-03T15:00:00+08:00"
    assert v.to_dict()["schema_version"] == "legacy_csv/v1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": _MISSING},
        {"version": "not-a-mapping"},
        {"schema_version": _MISSING},
        {"coverage": _MISSING},
        {"stage": _MISSING},
        {"reconciled": _MISSING},
        {"degraded": _MISSING},
        {"backend": ""},
        {"backend": None},
        {"schema_version": "legacy_csv/v2"},  # incompatible schema
        {"schema_version": None},
        {"generation": _MISSING, "source_watermark": _MISSING},  # no immutable identity
        {"coverage": ""},
        {"stage": ""},
        {"reconciled": 1},
        {"degraded": "false"},
    ],
)
def test_version_metadata_fail_closed(overrides):
    if "version" in overrides:
        payload = series_payload(version=overrides["version"])
    else:
        payload = series_payload(version=version_payload(**overrides))
    client, _ = make_client(payload)
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519")
    assert exc.value.code == "schema_mismatch"
    assert exc.value.retryable is False

def test_version_schema_must_match_dataset():
    client, _ = make_client(
        {
            "dataset": "tdx_moneyflow/a",
            "cache_id": "sh600519",
            "total": {"main": 1},
            "version": version_payload(schema_version="tdx_moneyflow/v1"),
        }
    )
    client.daily_moneyflow_point("sh600519", "20260903")

    client2, _ = make_client(
        {
            "dataset": "tdx_moneyflow/a",
            "cache_id": "sh600519",
            "total": {"main": 1},
            "version": version_payload(schema_version="legacy_csv/v1"),
        }
    )
    with pytest.raises(DataQueryError) as exc:
        client2.daily_moneyflow_point("sh600519", "20260903")
    assert exc.value.code == "schema_mismatch"


# ---------------------------------------------------------------------------
# status metadata


def test_status_ok_returns_route_metadata():
    routes = [
        {
            "dataset": "tdx_day/a",
            "backend": "tdx-local",
            "schema_version": "legacy_csv/v1",
            "generation": "g1",
            "source_watermark": "20260903",
            "coverage": "full-a",
            "stage": "published",
            "reconciled": True,
            "degraded": False,
            "error_code": None,
            "version": {"generation": "g1"},
        }
    ]
    client, fake = make_client({"status": "ok", "routes": routes})
    result = client.status()
    assert result["status"] == "ok"
    assert result["routes"][0]["dataset"] == "tdx_day/a"
    assert fake.paths == ["/api/v2/dataquery/status"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "degraded", "routes": []},
        {"status": "ok", "routes": "not-a-list"},
        {"status": "ok", "routes": ["not-a-mapping"]},
        {"status": "ok", "routes": [{"no": "dataset"}]},
        {
            "status": "ok",
            "routes": [
                {
                    "dataset": "tdx_day/a",
                    # missing: backend, schema_version, ... every required field
                    "error_code": None,
                    "version": {},
                }
            ],
        },
    ],
)
def test_status_metadata_fail_closed(payload):
    client, _ = make_client(payload)
    with pytest.raises(DataQueryError) as exc:
        client.status()
    assert exc.value.code == "schema_mismatch"
    assert exc.value.retryable is False

def test_observed_versions_tracks_only_successful_queries():
    fake = FakeClient(
        [
            FakeResponse(series_payload()),
            FakeResponse(minutes_payload()),
        ]
    )
    client = DataQueryClient("http://dataquery.test", client=fake)
    assert client.observed_versions() == {}

    client.series("day", "sh600519")
    client.series("minutes", "sz000001", date="20260903")

    observed = client.observed_versions()
    assert set(observed) == {"tdx_day/a", "tdx_minutes/a"}
    assert observed["tdx_day/a"]["schema_version"] == "legacy_csv/v1"
    assert observed["tdx_minutes/a"]["generation"] == "g-20260903"

    # a failed query must not poison or add observed versions
    client_broken, _ = make_client(series_payload(version=version_payload(schema_version="bad")))
    with pytest.raises(DataQueryError):
        client_broken.series("day", "sh600519")
    assert client_broken.observed_versions() == {}

    # returned mapping is a snapshot copy
    observed["tdx_day/a"]["backend"] = "mutated"
    assert client.observed_versions()["tdx_day/a"]["backend"] == "tdx-local"


def test_all_requests_stay_on_v2_with_no_fallback():
    version_moneyflow = version_payload(schema_version="tdx_moneyflow/v1")
    fake = FakeClient(
        [
            FakeResponse(series_payload()),
            FakeResponse(minutes_payload()),
            FakeResponse({"status": "ok", "routes": []}),
        ]
    )
    client = DataQueryClient("http://dataquery.test", client=fake)
    # exercises every public read path
    client.series("day", "sh600519", start="20260101", end="20260131")
    client.series("minutes", "sz000001", date="20260903")
    client.status()

    mf_client, mf_fake = make_client(
        {
            "dataset": "tdx_moneyflow/a",
            "cache_id": "sh600519",
            "total": {"main": 1},
            "version": version_moneyflow,
        }
    )
    mf_client.daily_moneyflow_point("sh600519", "20260903")

    for recorded in (fake, mf_fake):
        assert recorded.requests, "expected at least one recorded request"
        for path, _ in recorded.requests:
            assert path.startswith("/api/v2/"), path
            assert "api/v1" not in path, path
        # one request per public call: no retries, no dual-read fallbacks
        assert len(recorded.requests) == (3 if recorded is fake else 1)


def test_failed_queries_issue_exactly_one_request():
    # server-side typed error: client must not retry or fall back
    client, fake = make_client(error_envelope("stale", message="later", retryable=True), status_code=503)
    with pytest.raises(DataQueryError):
        client.series("day", "sh600519")
    assert len(fake.requests) == 1

    # transport failure: still exactly one request
    client2, fake2 = make_client({}, transport_error=httpx.ConnectError("down"))
    with pytest.raises(DataQueryError):
        client2.series("day", "sh600519")
    assert len(fake2.requests) == 1


# ---------------------------------------------------------------------------
# end-to-end smoke over httpx.MockTransport: wire-format parity, no processes


def _mock_transport(routes: dict[tuple[str, str], tuple[int, dict[str, Any]]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json=error_envelope("not_found", message=f"no route {key}"))
        status, payload = routes[key]
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _wire_client(routes: dict[tuple[str, str], tuple[int, dict[str, Any]]]) -> DataQueryClient:
    transport = _mock_transport(routes)
    return DataQueryClient(
        base_url="http://dataquery.test",
        client=httpx.Client(
            base_url="http://dataquery.test",
            transport=transport,
            trust_env=False,
            timeout=5.0,
        ),
    )


def test_truncated_series_raises_typed_incomplete_not_partial_success():
    client, fake = make_client(series_payload(truncated=True))
    with pytest.raises(DataQueryError) as exc:
        client.series("day", "sh600519", start="20260101", end="20260902", limit=1000)
    assert exc.value.code == "incomplete"
    assert exc.value.http_status == 503
    assert exc.value.retryable is True
    assert "engine #11" in exc.value.message


def test_wire_smoke_success_409_404_and_version_propagation():
    version = version_payload(
        backend="legacy_raw",
        schema_version="legacy_csv/v1",
        coverage="2026-09-02",
        stage="legacy",
        reconciled=False,
        degraded=True,
    )
    routes: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {
        ("GET", "/api/v2/dataquery/series/wide/sh600519"): (
            200,
            series_payload(
                "tdx_wide/a",
                "sh600519",
                rows=[{"date": "20260901", "close": 1500.0}],
                version=version,
            ),
        ),
        ("GET", "/api/v2/dataquery/moneyflow/daily/stocks/sh600519"): (
            200,
            {
                "dataset": "tdx_moneyflow/a",
                "cache_id": "sh600519",
                "date": "20260901",
                "total": {
                    "total_amount": 100.0,
                    "inflow_amount": 70.0,
                    "outflow_amount": 30.0,
                    "net_amount": 40.0,
                },
                "version": version_payload(
                    backend="duckdb_published_snapshot",
                    schema_version="tdx_moneyflow/v1",
                ),
            },
        ),
        ("GET", "/api/v2/dataquery/series/minutes/sz000001"): (
            409,
            error_envelope(
                "version_pinned_unavailable",
                message="pinned bundle not published",
                retryable=False,
                dataset="tdx_minutes/a",
            ),
        ),
        ("GET", "/api/v2/dataquery/series/trans/sh600519"): (
            404,
            error_envelope("not_found", message="no trans for date", retryable=False),
        ),
    }
    client = _wire_client(routes)

    wide = client.series("wide", "sh600519", start="20260901", end="20260902")
    assert wide.dataset == "tdx_wide/a"
    assert wide.rows[0]["date"] == "20260901"
    assert wide.version.backend == "legacy_raw"

    flow = client.daily_moneyflow_point("sh600519", "2026-09-01")
    assert flow.rows[0]["net_amount"] == 40.0
    assert flow.version.schema_version == "tdx_moneyflow/v1"

    with pytest.raises(DataQueryError) as exc:
        client.series("minutes", "sz000001", date="20260901")
    assert exc.value.http_status == 409
    assert exc.value.code == "version_pinned_unavailable"
    assert exc.value.retryable is False

    with pytest.raises(DataQueryError) as exc404:
        client.series("trans", "sh600519", date="20260901")
    assert exc404.value.http_status == 404
    assert exc404.value.code == "not_found"

    observed = client.observed_versions()
    # only the two successful queries contribute version metadata
    assert observed["tdx_wide/a"]["backend"] == "legacy_raw"
    assert observed["tdx_moneyflow/a"]["schema_version"] == "tdx_moneyflow/v1"
    assert "tdx_minutes/a" not in observed and "tdx_trans/a" not in observed
