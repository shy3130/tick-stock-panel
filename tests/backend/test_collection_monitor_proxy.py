from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import collection_monitor


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: object | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = {"evidence": "available"} if payload is None else payload
        self._chunks = chunks

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream response included a credential",
                request=httpx.Request("GET", "http://upstream.invalid/private"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_bytes(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._chunks is not None:
            yield from self._chunks
            return
        yield json.dumps(self._payload).encode()


def _patch_upstream(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
) -> None:
    monkeypatch.setattr(collection_monitor.httpx, "stream", lambda *args, **kwargs: response)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(collection_monitor.router)
    return TestClient(app)


def test_tasks_uses_fixed_upstream_path_and_forwards_canonical_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_stream(method: str, url: str, **kwargs: object) -> _Response:
        assert method == "GET"
        calls.append((url, kwargs))
        return _Response(payload={"tasks": []})

    monkeypatch.setenv("LONGBRIDGE_API_URL", "http://monitor.internal:19912/")
    monkeypatch.setattr(collection_monitor.httpx, "stream", fake_stream)

    response = client.get(
        "/api/collection-monitor/tasks?date=2026-07-26&status=yellow&technology=rust"
        "&market=hk&dataset=capital_flow&mode=shadow&limit=500&offset=12"
    )

    assert response.status_code == 200
    assert response.json() == {"tasks": []}
    assert calls == [
        (
            "http://monitor.internal:19912/api/collection-monitor/tasks",
            {
                "params": {
                    "date": "2026-07-26",
                    "status": "yellow",
                    "technology": "rust",
                    "market": "hk",
                    "dataset": "capital_flow",
                    "mode": "shadow",
                    "limit": 500,
                    "offset": 12,
                },
                "timeout": 10.0,
            },
        )
    ]


def test_tasks_forwards_bounded_pagination_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_stream(method: str, url: str, **kwargs: object) -> _Response:
        assert method == "GET"
        calls.append(kwargs)
        return _Response(payload={"tasks": []})

    monkeypatch.setattr(collection_monitor.httpx, "stream", fake_stream)

    response = client.get("/api/collection-monitor/tasks")

    assert response.status_code == 200
    assert calls == [{"params": {"limit": 100, "offset": 0}, "timeout": 10.0}]


@pytest.mark.parametrize(
    ("path", "expected_path", "expected_params"),
    [
        (
            "/api/collection-monitor/overview?date=2026-07-26",
            "/api/collection-monitor/overview",
            {"date": "2026-07-26"},
        ),
        (
            "/api/collection-monitor/markets/us?date=2026-07-26",
            "/api/collection-monitor/markets/us",
            {"date": "2026-07-26"},
        ),
        (
            "/api/collection-monitor/gaps?market=cn&dataset=depth&symbol=600000.SH"
            "&recovered=true&limit=1&offset=0",
            "/api/collection-monitor/gaps",
            {
                "market": "cn",
                "dataset": "depth",
                "symbol": "600000.SH",
                "recovered": True,
                "limit": 1,
                "offset": 0,
            },
        ),
    ],
)
def test_routes_use_only_their_fixed_upstream_paths(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_path: str,
    expected_params: dict[str, object],
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_stream(method: str, url: str, **kwargs: object) -> _Response:
        assert method == "GET"
        calls.append((url, kwargs))
        if expected_path == "/api/collection-monitor/overview":
            return _Response(payload={})
        if expected_path.startswith("/api/collection-monitor/markets/"):
            return _Response(payload={"datasets": []})
        return _Response(payload={"gaps": []})

    monkeypatch.setattr(collection_monitor.httpx, "stream", fake_stream)

    response = client.get(path)

    assert response.status_code == 200
    assert calls == [
        (
            f"http://127.0.0.1:19912{expected_path}",
            {"params": expected_params, "timeout": 10.0},
        )
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/collection-monitor/markets/all",
        "/api/collection-monitor/overview?date=2026-02-30",
        "/api/collection-monitor/overview?date=2026-07-26T00:00:00",
        "/api/collection-monitor/tasks?status=queued",
        "/api/collection-monitor/tasks?technology=java",
        "/api/collection-monitor/tasks?mode=live",
        "/api/collection-monitor/tasks?dataset=quotes",
        "/api/collection-monitor/tasks?dataset=market_temperature",
        "/api/collection-monitor/gaps?market=hk&dataset=market_temperature",
        "/api/collection-monitor/gaps?market=hk&dataset=depth&symbol=bad-symbol",
        "/api/collection-monitor/tasks?limit=0",
        "/api/collection-monitor/tasks?limit=501",
        "/api/collection-monitor/tasks?offset=-1",
        "/api/collection-monitor/tasks?offset=100001",
        "/api/collection-monitor/overview?market=hk",
    ],
)
def test_rejects_invalid_or_unknown_query_values(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 422


def test_exposes_exactly_four_get_routes_and_no_post_routes() -> None:
    routes = {
        route.path: route.methods
        for route in collection_monitor.router.routes
        if hasattr(route, "methods")
    }

    assert routes == {
        "/api/collection-monitor/overview": {"GET"},
        "/api/collection-monitor/markets/{market}": {"GET"},
        "/api/collection-monitor/tasks": {"GET"},
        "/api/collection-monitor/gaps": {"GET"},
    }


def test_main_source_registers_collection_monitor_router() -> None:
    main_path = Path(__file__).resolve().parents[2] / "backend" / "app" / "main.py"
    module = ast.parse(main_path.read_text(encoding="utf-8"))

    imports_collection_monitor = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.api"
        and any(alias.name == "collection_monitor" for alias in node.names)
        for node in module.body
    )
    registers_collection_monitor = any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "app"
        and node.value.func.attr == "include_router"
        and len(node.value.args) == 1
        and isinstance(node.value.args[0], ast.Attribute)
        and isinstance(node.value.args[0].value, ast.Name)
        and node.value.args[0].value.id == "collection_monitor"
        and node.value.args[0].attr == "router"
        for node in module.body
    )

    assert imports_collection_monitor
    assert registers_collection_monitor


def test_full_application_registers_proxy_router_when_runtime_is_provisioned() -> None:
    pytest.importorskip("polars", reason="the application import requires the backend runtime dependency")
    from app.main import app

    route_methods = {
        route.path: route.methods
        for route in app.routes
        if hasattr(route, "methods") and route.path.startswith("/api/collection-monitor/")
    }

    assert route_methods == {
        "/api/collection-monitor/overview": {"GET"},
        "/api/collection-monitor/markets/{market}": {"GET"},
        "/api/collection-monitor/tasks": {"GET"},
        "/api/collection-monitor/gaps": {"GET"},
    }


def test_preserves_evidence_unavailable_503_without_upstream_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        collection_monitor.httpx,
        "stream",
        lambda *args, **kwargs: _Response(503, {"detail": "upstream secret"}),
    )

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 503
    assert response.json() == {"detail": "collection_monitoring_evidence_unavailable"}


def test_sanitizes_malformed_upstream_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGBRIDGE_API_URL", "http://[malformed")

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}
    assert "malformed" not in response.text


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_sanitizes_non_finite_upstream_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, value: float
) -> None:
    monkeypatch.setattr(
        collection_monitor.httpx,
        "stream",
        lambda *args, **kwargs: _Response(payload={"value": value}),
    )

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("http://user:password@upstream.invalid/secret-body"),
        _Response(500, {"detail": "http://user:password@upstream.invalid/secret-body"}),
        _Response(200, ValueError("http://user:password@upstream.invalid/secret-body")),
    ],
)
def test_sanitizes_network_http_and_non_json_upstream_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    def fake_stream(*args: object, **kwargs: object) -> _Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(collection_monitor.httpx, "stream", fake_stream)

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}
    assert "upstream.invalid" not in response.text
    assert "password" not in response.text
    assert "secret-body" not in response.text


def test_rejects_streamed_response_larger_than_two_mib(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _Response(
        payload={"eager": "buffering would incorrectly accept this"},
        chunks=[b" " * (2 * 1024 * 1024), b"x", b"credential=do-not-leak"],
    )
    _patch_upstream(monkeypatch, upstream)

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}
    assert "credential" not in response.text
    assert "do-not-leak" not in response.text


def test_accepts_a_valid_overview_at_the_exact_two_mib_boundary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = b'{"padding":"'
    suffix = b'"}'
    padding_size = (2 * 1024 * 1024) - len(prefix) - len(suffix)
    _patch_upstream(
        monkeypatch,
        _Response(
            chunks=[prefix, b"x" * padding_size, suffix],
        ),
    )

    response = client.get("/api/collection-monitor/overview")

    assert response.status_code == 200
    assert len(response.json()["padding"]) == padding_size


def test_accepts_six_market_response_datasets_including_market_temperature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets = [
        {"datasetKey": "capital_distribution"},
        {"datasetKey": "capital_flow"},
        {"datasetKey": "candlestick_1m"},
        {"datasetKey": "depth"},
        {"datasetKey": "market_temperature"},
        {"datasetKey": "trades"},
    ]
    _patch_upstream(
        monkeypatch,
        _Response(payload={"market": "hk", "datasets": datasets}),
    )

    response = client.get("/api/collection-monitor/markets/hk")

    assert response.status_code == 200
    assert response.json()["datasets"] == datasets


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/collection-monitor/tasks?limit=1",
            {"tasks": [{"id": "first"}, {"id": "secret-task"}], "total": 2},
        ),
        (
            "/api/collection-monitor/gaps?market=hk&dataset=depth&limit=1",
            {"gaps": [{"id": "first"}, {"id": "secret-gap"}], "total": 2},
        ),
        (
            "/api/collection-monitor/overview",
            [{"secret": "overview-must-be-a-mapping"}],
        ),
        (
            "/api/collection-monitor/tasks?limit=1",
            {"tasks": ["secret-task-must-be-a-mapping"], "total": 1},
        ),
        (
            "/api/collection-monitor/gaps?market=hk&dataset=depth&limit=1",
            {"gaps": ["secret-gap-must-be-a-mapping"], "total": 1},
        ),
    ],
)
def test_rejects_invalid_route_shapes_and_sanitizes_the_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: object,
) -> None:
    _patch_upstream(monkeypatch, _Response(payload=payload))

    response = client.get(path)

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "datasets",
    [
        [
            {"dataset": "capital_distribution"},
            {"dataset": "capital_flow"},
            {"dataset": "candlestick_1m"},
            {"dataset": "depth"},
            {"dataset": "market_temperature"},
            {"dataset": "trades"},
            {"dataset": "secret-seventh-dataset"},
        ],
        [{"dataset": "depth"}, {"dataset": "depth"}],
        [{"dataset": "secret-unknown-dataset"}],
        ["secret-dataset-must-be-a-mapping"],
    ],
)
def test_rejects_invalid_market_dataset_count_keys_and_duplicates(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    datasets: list[object],
) -> None:
    _patch_upstream(monkeypatch, _Response(payload={"market": "hk", "datasets": datasets}))

    response = client.get("/api/collection-monitor/markets/hk")

    assert response.status_code == 502
    assert response.json() == {"detail": "collection_monitoring_proxy_unavailable"}
    assert "secret" not in response.text
