from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app.api import auth as auth_api
from app.config import Settings
from app.main import app as main_app


def _preflight(origin: str):
    return TestClient(main_app).options(
        "/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:3011", "http://localhost:3011"],
)
def test_cors_preflight_allows_each_default_dev_origin(origin: str) -> None:
    response = _preflight(origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_cors_preflight_rejects_unconfigured_origin() -> None:
    response = _preflight("https://evil.example")

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_origin_config_parses_csv_and_rejects_wildcard(tmp_path) -> None:
    configured = Settings(
        _env_file=None,
        data_dir=tmp_path,
        cors_allow_origins=(
            " http://127.0.0.1:3011, ,http://localhost:3011,"
            "http://127.0.0.1:3011 "
        ),
    )
    assert configured.cors_origins == [
        "http://127.0.0.1:3011",
        "http://localhost:3011",
    ]

    with pytest.raises(ValueError, match="wildcard"):
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            cors_allow_origins="*",
        )


def _auth_client(monkeypatch, *, base_url: str) -> TestClient:
    monkeypatch.setattr(auth_api.auth, "is_configured", lambda: True)
    monkeypatch.setattr(
        auth_api.auth,
        "verify_and_create_session",
        lambda password: "test-session-token" if password == "correct-password" else None,
    )
    monkeypatch.setattr(auth_api.auth, "revoke_session", lambda _token: None)
    app = FastAPI()
    app.include_router(auth_api.router)
    return TestClient(app, base_url=base_url)


def test_login_over_https_sets_secure_httponly_lax_session_cookie(monkeypatch) -> None:
    client = _auth_client(monkeypatch, base_url="https://panel.example")

    response = client.post("/api/auth/login", json={"password": "correct-password"})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "tf_session=test-session-token" in cookie
    assert "; secure" in cookie
    assert "; httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=2592000" in cookie


def test_login_over_direct_http_omits_secure_session_cookie(monkeypatch) -> None:
    client = _auth_client(monkeypatch, base_url="http://127.0.0.1:3018")

    response = client.post("/api/auth/login", json={"password": "correct-password"})

    assert response.status_code == 200
    assert "; secure" not in response.headers["set-cookie"].lower()


def test_login_does_not_trust_raw_forwarded_proto_header(monkeypatch) -> None:
    client = _auth_client(monkeypatch, base_url="http://127.0.0.1:3018")

    response = client.post(
        "/api/auth/login",
        json={"password": "correct-password"},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 200
    assert "; secure" not in response.headers["set-cookie"].lower()


def test_logout_matches_https_cookie_policy(monkeypatch) -> None:
    client = _auth_client(monkeypatch, base_url="https://panel.example")

    response = client.post(
        "/api/auth/logout",
        headers={"Cookie": "tf_session=test-session-token"},
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "tf_session=" in cookie
    assert "max-age=0" in cookie
    assert "; secure" in cookie
    assert "; httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie


def test_client_ip_uses_only_server_resolved_peer() -> None:
    request = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"x-forwarded-for", b"100.64.0.10")],
            "client": ("172.21.0.1", 43210),
            "server": ("127.0.0.1", 3018),
        }
    )

    assert auth_api._client_ip(request) == "172.21.0.1"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.23.4.5", True),
        ("172.16.0.1", True),
        ("172.31.255.254", True),
        ("192.168.1.8", True),
        ("172.32.0.1", False),
        ("100.64.0.10", False),
        ("203.0.113.8", False),
        ("10.not-an-ip", False),
    ],
)
def test_local_network_check_uses_exact_ip_ranges(host: str, expected: bool) -> None:
    assert auth_api._is_local_network(host) is expected
