"""安全边界回归测试 — XFF 防伪造、IP 校验、CORS 收紧与高危管理端点统一鉴权。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import config_backup as config_backup_api
from app.api import ext_pages as ext_pages_api
from app.api import pages_admin as pages_admin_api
from app.api import position_policies as position_policies_api
from app.api import settings as settings_api
from app.api import strategy as strategy_api
from app.config import settings
from app.services import api_gateway
from app.services import auth as auth_service


def _fake_request(
    client_host: str,
    *,
    headers: dict[str, str] | None = None,
    hostname: str = "localhost",
) -> SimpleNamespace:
    normalized = {k.lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(
        client=SimpleNamespace(host=client_host) if client_host else None,
        headers=normalized,
        url=SimpleNamespace(hostname=hostname),
        cookies={},
    )


# ── 1. is_local_network / is_loopback 严格 IP 校验 ──────────


def test_is_local_network_rejects_malformed_and_link_local_accepts_ipv4_mapped():
    f = api_gateway.is_local_network
    for valid_local in (
        "127.0.0.1",
        "127.0.0.2",
        "::1",
        "localhost",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.5",
        "::ffff:10.0.0.2",
        "::ffff:172.16.0.1",
        "fc00::1",
        "fd12:3456::1",
    ):
        assert f(valid_local) is True, valid_local

    for invalid_or_public in (
        "10.evil",
        "192.168.foo",
        "172.16.bad",
        "169.254.169.254",
        "::ffff:8.8.8.8",
        "100.64.0.1",
        "203.0.113.7",
        None,
        "",
        "testclient",
    ):
        assert f(invalid_or_public) is False, repr(invalid_or_public)


def test_client_ip_ignores_attacker_spoofed_leftmost_xff():
    # 公网客户端 203.0.113.7 经本机反代 (127.0.0.1) 发送伪造 X-Forwarded-For: 127.0.0.1
    req = _fake_request("127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.7"})
    assert auth_api._client_ip(req) == "203.0.113.7"

    # 多层内网反代 (Nginx -> Docker bridge) 仍提取真实公网客户端 IP
    req_multi = _fake_request(
        "172.17.0.1",
        headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.7, 192.168.1.10"},
    )
    assert auth_api._client_ip(req_multi) == "203.0.113.7"

    # 局域网客户端经本机反代伪造 127.0.0.1, 应解析为真实局域网 IP (不可冒充本机回环)
    req_lan = _fake_request("127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1, 192.168.1.50"})
    assert auth_api._client_ip(req_lan) == "192.168.1.50"

    # 真实本机经本机反代仍保持 127.0.0.1
    req_local = _fake_request("127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1"})
    assert auth_api._client_ip(req_local) == "127.0.0.1"

    # 公网直连发送伪造 XFF 时完全忽略 XFF
    req_direct_public = _fake_request(
        "198.51.100.2", headers={"X-Forwarded-For": "127.0.0.1"}
    )
    assert auth_api._client_ip(req_direct_public) == "198.51.100.2"


def test_spoofed_xff_cannot_hijack_setup_or_bypass_login_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    auth_service._sessions.clear()
    auth_service._configured_cache = None
    with auth_api._fail_lock:
        auth_api._fail_counter.clear()

    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))

    # 1) 公网攻击者通过本机反代伪造 XFF=127.0.0.1 试图抢先设密码 -> 必须 403
    r_hijack = client.post(
        "/api/auth/setup",
        json={"password": "attacker-password"},
        headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"},
    )
    assert r_hijack.status_code == 403
    assert not auth_service.is_configured()

    # 2) 本机跨站网页试图设密码 -> 必须 403
    r_cross = client.post(
        "/api/auth/setup",
        json={"password": "attacker-password"},
        headers={"Origin": "https://evil.example"},
    )
    assert r_cross.status_code == 403
    assert not auth_service.is_configured()

    # 3) 正常本机同源设密码 -> 200
    r_ok = client.post(
        "/api/auth/setup",
        json={"password": "real-password-123"},
        headers={"Origin": "http://localhost"},
    )
    assert r_ok.status_code == 200

    # 4) 攻击者轮换最左端伪造 IP 试图绕过登录 5 次限流 -> 第 6 次必须 429
    for i in range(5):
        resp = client.post(
            "/api/auth/login",
            json={"password": "wrong-password"},
            headers={"X-Forwarded-For": f"1.2.3.{i}, 203.0.113.9"},
        )
        assert resp.status_code == 401
    blocked = client.post(
        "/api/auth/login",
        json={"password": "real-password-123"},
        headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9"},
    )
    assert blocked.status_code == 429

    auth_service._sessions.clear()
    auth_service._configured_cache = None
    with auth_api._fail_lock:
        auth_api._fail_counter.clear()


# ── 2. require_management 归位与高危路由统一防护 ──────────


def test_require_management_reexport_backward_compatible():
    from app.custom.source_packages.routes import require_management as legacy_ref

    assert legacy_ref is auth_api.require_management


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        (
            "POST",
            "/api/ext-pages/upload",
            {
                "data": {"id": "p1"},
                "files": {"file": ("p.js", b"export default {}", "text/javascript")},
            },
        ),
        (
            "POST",
            "/api/ext-pages/import-source",
            {
                "data": {"id": "p1"},
                "files": {"file": ("p.tsx", b"export default {}", "text/plain")},
            },
        ),
        ("POST", "/api/ext-pages/import-source/from-project", {"json": {"namespace": "x"}}),
        ("PUT", "/api/ext-pages/p1/enabled", {"json": {"enabled": True}}),
        ("DELETE", "/api/ext-pages/p1", {}),
        ("GET", "/api/config-backup/export", {"params": {"categories": "secrets"}}),
        ("POST", "/api/config-backup/apply", {"files": {"file": ("b.zip", b"x", "application/zip")}}),
        ("DELETE", "/api/pages-admin/source-extensions/demo", {}),
        ("POST", "/api/settings/plugins/fuyao/install", {}),
        ("DELETE", "/api/settings/plugins/fuyao/install", {}),
        ("GET", "/api/settings/api-tokens", {}),
        ("POST", "/api/settings/api-tokens", {"json": {"name": "t", "scopes": ["read:market"]}}),
        ("DELETE", "/api/settings/api-tokens/tok1", {}),
        ("POST", "/api/strategies/code/save", {"json": {"code": "x"}}),
        ("POST", "/api/strategies/ai/save", {"json": {"code": "x"}}),
        ("POST", "/api/strategies/bundle/apply", {"files": {"file": ("s.zip", b"x", "application/zip")}}),
        ("DELETE", "/api/strategies/custom_x", {}),
        ("POST", "/api/backtest/position-policies/validate", {"json": {"label": "p", "code": "def compute(ctx): return 1"}}),
        ("POST", "/api/backtest/position-policies", {"json": {"label": "p", "code": "def compute(ctx): return 1"}}),
        ("DELETE", "/api/backtest/position-policies/up_demo", {}),
    ],
)
def test_high_risk_endpoints_enforce_require_management(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    kwargs: dict,
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(auth_service, "is_configured", lambda: False)

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.data_dir = tmp_path
    for r in (
        ext_pages_api.router,
        config_backup_api.router,
        pages_admin_api.router,
        settings_api.router,
        strategy_api.router,
        position_policies_api.router,
    ):
        app.include_router(r)

    # 1) 本机跨站 Origin -> 403
    local_client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))
    r_origin = local_client.request(
        method, path, headers={"Origin": "https://evil.example"}, **kwargs,
    )
    assert r_origin.status_code == 403, (method, path, r_origin.status_code, r_origin.text)

    # 2) Sec-Fetch-Site: cross-site -> 403
    r_fetch = local_client.request(
        method, path, headers={"Sec-Fetch-Site": "cross-site"}, **kwargs,
    )
    assert r_fetch.status_code == 403, (method, path, r_fetch.status_code)

    # 3) Bearer Token -> 403
    r_bearer = local_client.request(
        method, path, headers={"Authorization": "Bearer tsp_fake"}, **kwargs,
    )
    assert r_bearer.status_code == 403, (method, path, r_bearer.status_code)

    # 4) 未设密码时的局域网非回环访问 -> 403
    lan_client = TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.50", 12345))
    r_lan = lan_client.request(method, path, **kwargs)
    assert r_lan.status_code == 403, (method, path, r_lan.status_code)

    # 5) 已设密码但未登录 -> 401
    monkeypatch.setattr(auth_service, "is_configured", lambda: True)
    r_unauth = local_client.request(method, path, **kwargs)
    assert r_unauth.status_code == 401, (method, path, r_unauth.status_code)


# ── 3. 主应用 CORS 默认不放行通配来源 & 拒绝跨站写请求 ────


def test_main_app_does_not_allow_wildcard_cors_by_default():
    from app.main import app

    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))
    r = client.options(
        "/api/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-origin") != "*"
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"

    r_get = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert r_get.headers.get("access-control-allow-origin") is None


def test_main_app_middleware_blocks_cross_site_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(auth_service, "is_configured", lambda: False)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345))

    r1 = client.post(
        "/api/auth/setup",
        json={"password": "evil-password"},
        headers={"Origin": "https://evil.example"},
    )
    assert r1.status_code == 403

    r2 = client.post(
        "/api/auth/setup",
        json={"password": "evil-password"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r2.status_code == 403
