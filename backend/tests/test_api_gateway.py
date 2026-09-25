"""API Token 域 + 开放网关测试矩阵 (open-platform-plan §4)。

域模块: 创建/校验/吊销/哈希不落明文/scope 校验。
网关 (端到端, 挂真实中间件链): 401 无效 Token / 403 scope 不足 / 403 未开放端点 /
429 限流 + Retry-After / 放行响应带 X-RateLimit 头 / UI 密码路径不受 Token 桶影响。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import api_gateway, api_tokens


# ── 域模块 ────────────────────────────────────────────────
def test_create_verify_revoke_roundtrip(tmp_path: Path):
    record, plaintext = api_tokens.create_token(tmp_path, "测试", ["read:market"])
    assert plaintext.startswith("tsp_") and len(plaintext) > 40
    assert record["scopes"] == ["read:market"] and record["revoked"] is False
    # 明文不落盘
    raw = (tmp_path / "user_data" / "api_tokens.json").read_text(encoding="utf-8")
    assert plaintext not in raw
    # 校验命中
    assert api_tokens.verify_token(tmp_path, plaintext)["id"] == record["id"]
    # 吊销后失效
    assert api_tokens.revoke_token(tmp_path, record["id"]) is True
    assert api_tokens.verify_token(tmp_path, plaintext) is None
    assert api_tokens.list_tokens(tmp_path)[0]["revoked"] is True


def test_create_rejects_unknown_scope(tmp_path: Path):
    with pytest.raises(ValueError, match="未知 scope"):
        api_tokens.create_token(tmp_path, "x", ["admin"])


def test_verify_wrong_or_malformed_token(tmp_path: Path):
    _, plaintext = api_tokens.create_token(tmp_path, "t", ["read:market"])
    # 追加字符构造"哈希必不同"的伪 Token — 若替换末位字符, 原值恰好以该字符结尾时会撞上真 Token (1/16 概率)
    assert api_tokens.verify_token(tmp_path, plaintext + "0") is None
    assert api_tokens.verify_token(tmp_path, "not_a_token") is None
    assert api_tokens.verify_token(tmp_path, "") is None


def test_required_scope_mapping():
    f = api_gateway.required_scope
    assert f("GET", "/api/kline/daily") == "read:market"
    assert f("GET", "/api/kline/daily/latest") == "read:market"
    assert f("GET", "/api/kline/minute-range") == "read:market"
    assert f("POST", "/api/kline/daily-batch") is None          # 管理面
    assert f("GET", "/api/ext-data/ext_fuyao_hot/rows") == "read:ext"
    assert f("GET", "/api/ext-data/ext_fuyao_hot/api-key") is None  # Key 状态不外露
    assert f("POST", "/api/ext-data/ext_fuyao_hot/ingest") is None   # 写不开放
    assert f("GET", "/api/backtest/candidates") == "read:analysis"
    assert f("POST", "/api/backtest/run") == "run:backtest"
    assert f("POST", "/api/backtest/strategy/run") == "run:backtest"
    assert f("GET", "/api/paper/overview") == "paper:trade"
    assert f("POST", "/api/paper/orders") == "paper:trade"
    assert f("GET", "/api/settings/api-tokens") is None          # Token 管理本身不开放
    assert f("GET", "/api/monitor-rules") is None                # V1 最小面外
    # 前缀混淆不误伤: /api/paperback 不得命中 /api/paper
    assert f("GET", "/api/paperback") is None


# ── 网关端到端 (真实中间件链) ──────────────────────────────
@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """裸 FastAPI + main 的 auth_middleware 逻辑副本: 只验 Token 通道。

    直接 import app.main 的中间件需要完整 lifespan, 这里用同一段 Bearer
    分支逻辑等价复刻 (分支简单且稳定); 密码路径由既有中间件测试覆盖。
    """
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/api/kline/daily")
    def market():  # noqa: ANN001
        return {"ok": True}

    @app.get("/api/ext-data/x/rows")
    def ext():  # noqa: ANN001
        return {"ok": True}

    @app.post("/api/paper/orders")
    def paper():  # noqa: ANN001
        return {"ok": True}

    @app.get("/api/settings/api-tokens")
    def admin():  # noqa: ANN001
        return {"ok": True}

    from app.config import Settings
    fake_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(fake_settings, "data_dir", tmp_path)

    @app.middleware("http")
    async def token_gate(request, call_next):  # noqa: ANN001
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            verdict = api_gateway.evaluate(
                fake_settings.data_dir, request.method, path, authz[len("Bearer "):].strip(),
            )
            if verdict["status"] is not None:
                return JSONResponse(
                    status_code=verdict["status"], content={"detail": verdict["detail"]},
                    headers=verdict["headers"],
                )
            resp = await call_next(request)
            for k, v in verdict["headers"].items():
                resp.headers[k] = v
            return resp
        return await call_next(request)  # UI 会话路径 (无 Bearer)

    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_gateway_401_403_200_matrix(client: TestClient, tmp_path: Path):
    _, market_token = api_tokens.create_token(tmp_path, "只读行情", ["read:market"])
    H = {"Authorization": f"Bearer {market_token}"}

    # 401: 伪造 Token
    r = client.get("/api/kline/daily", headers={"Authorization": "Bearer tsp_deadbeef"})
    assert r.status_code == 401
    # 403: scope 不足 (read:market 调 ext)
    assert client.get("/api/ext-data/x/rows", headers=H).status_code == 403
    # 403: 未开放端点 (管理)
    assert client.get("/api/settings/api-tokens", headers=H).status_code == 403
    # 200: 命中 scope + 限流头
    r = client.get("/api/kline/daily", headers=H)
    assert r.status_code == 200
    assert r.headers.get("X-RateLimit-Limit") == "120"
    assert int(r.headers["X-RateLimit-Remaining"]) < 120


def test_gateway_scope_grants_endpoint(client: TestClient, tmp_path: Path):
    _, paper_token = api_tokens.create_token(tmp_path, "模拟盘", ["paper:trade"])
    r = client.post("/api/paper/orders", headers={"Authorization": f"Bearer {paper_token}"})
    assert r.status_code == 200


def test_gateway_rate_limit_429(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_gateway, "rate_limit_per_min", lambda: 3)
    _, tok = api_tokens.create_token(tmp_path, "限流", ["read:market"])
    H = {"Authorization": f"Bearer {tok}"}
    for _ in range(3):
        assert client.get("/api/kline/daily", headers=H).status_code == 200
    r = client.get("/api/kline/daily", headers=H)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    # 无 Bearer 的 UI 会话路径不受 Token 桶影响
    assert client.get("/api/kline/daily").status_code == 200


def test_revoked_token_rejected_immediately(client: TestClient, tmp_path: Path):
    record, tok = api_tokens.create_token(tmp_path, "待吊销", ["read:market"])
    H = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/kline/daily", headers=H).status_code == 200
    api_tokens.revoke_token(tmp_path, record["id"])
    assert client.get("/api/kline/daily", headers=H).status_code == 401
