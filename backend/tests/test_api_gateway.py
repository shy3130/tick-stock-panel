"""API Token 域 + 开放网关测试矩阵 (open-platform-plan §4)。

域模块: 创建/校验/吊销/哈希不落明文/scope 校验。
网关 (端到端, 挂真实中间件链): 401 无效 Token / 403 scope 不足 / 403 未开放端点 /
429 限流 + Retry-After / 放行响应带 X-RateLimit 头 / UI 密码路径不受 Token 桶影响。
网络门禁: 公网 IP 403 / 内网放行 / API_TOKEN_LOCAL_ONLY 开关 / 不消耗限流额度。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api
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
    assert f("POST", "/api/ext-data/ext_fuyao_hot/ingest") == "write:ext"  # 程序化写入
    assert f("POST", "/api/ext-data/ext_fuyao_hot/upload") is None   # 文件上传仍属管理面
    assert f("POST", "/api/ext-data/ext_fuyao_hot/pull/test") is None
    assert f("POST", "/api/ext-data") is None                       # 建表不开放
    assert f("PUT", "/api/ext-data/x/ingest") is None               # 仅 POST
    assert f("GET", "/api/events/ticket") == "*"                    # 票据签发: 任意有效 Token
    assert f("POST", "/api/events/ticket") == "*"
    assert f("GET", "/api/events") == "*"                           # SSE 流本体 (票据认证)
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
    def market():
        return {"ok": True}

    @app.get("/api/ext-data/x/rows")
    def ext():
        return {"ok": True}

    @app.post("/api/ext-data/x/ingest")
    def ingest():
        return {"status": "ok", "rows": 1}

    @app.post("/api/events/ticket")
    def ticket():
        return {"ticket": "tse_x"}

    @app.post("/api/paper/orders")
    def paper():
        return {"ok": True}

    @app.get("/api/settings/api-tokens")
    def admin():
        return {"ok": True}

    from app.config import Settings
    fake_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(fake_settings, "data_dir", tmp_path)

    # TestClient 的 peer 固定为 "testclient"(非内网地址), 门禁按本机放行以聚焦
    # scope/限流矩阵; 公网 403 分支由 test_token_gate_* 用例显式覆盖
    monkeypatch.setattr(auth_api, "_client_ip", lambda request: "127.0.0.1")

    @app.middleware("http")
    async def token_gate(request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            gate = api_gateway.token_network_verdict(auth_api._client_ip(request))
            if gate is not None:
                return JSONResponse(
                    status_code=gate["status"], content={"detail": gate["detail"]},
                )
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
    hdr = {"Authorization": f"Bearer {market_token}"}

    # 401: 伪造 Token
    r = client.get("/api/kline/daily", headers={"Authorization": "Bearer tsp_deadbeef"})
    assert r.status_code == 401
    # 403: scope 不足 (read:market 调 ext)
    assert client.get("/api/ext-data/x/rows", headers=hdr).status_code == 403
    # 403: 未开放端点 (管理)
    assert client.get("/api/settings/api-tokens", headers=hdr).status_code == 403
    # 200: 命中 scope + 限流头
    r = client.get("/api/kline/daily", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("X-RateLimit-Limit") == "120"
    assert int(r.headers["X-RateLimit-Remaining"]) < 120


def test_gateway_scope_grants_endpoint(client: TestClient, tmp_path: Path):
    _, paper_token = api_tokens.create_token(tmp_path, "模拟盘", ["paper:trade"])
    r = client.post("/api/paper/orders", headers={"Authorization": f"Bearer {paper_token}"})
    assert r.status_code == 200


def test_gateway_write_ext_ingest(client: TestClient, tmp_path: Path):
    """write:ext — 程序化写入扩展表; 其余写端点仍不开放。"""
    _, w = api_tokens.create_token(tmp_path, "数据写入", ["write:ext"])
    _, r = api_tokens.create_token(tmp_path, "只读", ["read:ext", "read:market"])
    hdr_w = {"Authorization": f"Bearer {w}"}
    hdr_r = {"Authorization": f"Bearer {r}"}

    # 持有 write:ext → ingest 200
    assert client.post("/api/ext-data/x/ingest", headers=hdr_w).status_code == 200
    # 只读 Token → 403 (缺 scope)
    resp = client.post("/api/ext-data/x/ingest", headers=hdr_r)
    assert resp.status_code == 403
    assert "write:ext" in resp.json()["detail"]
    # write:ext 不能读 (rows 是 read:ext)
    assert client.get("/api/ext-data/x/rows", headers=hdr_w).status_code == 403
    # 管理面写端点对 write:ext 也不开 (结构/上传/拉取)
    assert client.post("/api/ext-data/x/upload", headers=hdr_w).status_code == 403


def test_gateway_ticket_any_token(client: TestClient, tmp_path: Path):
    """票据签发只要求 Token 有效, 不要求特定 scope (票据继承 scope, 不放大)。"""
    _, tok = api_tokens.create_token(tmp_path, "任意", ["read:market"])
    r = client.post("/api/events/ticket", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    # 无效 Token → 401
    assert client.post("/api/events/ticket", headers={"Authorization": "Bearer tsp_bad"}).status_code == 401


def test_gateway_rate_limit_429(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_gateway, "rate_limit_per_min", lambda: 3)
    _, tok = api_tokens.create_token(tmp_path, "限流", ["read:market"])
    hdr = {"Authorization": f"Bearer {tok}"}
    for _ in range(3):
        assert client.get("/api/kline/daily", headers=hdr).status_code == 200
    r = client.get("/api/kline/daily", headers=hdr)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    # 无 Bearer 的 UI 会话路径不受 Token 桶影响
    assert client.get("/api/kline/daily").status_code == 200


def test_revoked_token_rejected_immediately(client: TestClient, tmp_path: Path):
    record, tok = api_tokens.create_token(tmp_path, "待吊销", ["read:market"])
    hdr = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/kline/daily", headers=hdr).status_code == 200
    api_tokens.revoke_token(tmp_path, record["id"])
    assert client.get("/api/kline/daily", headers=hdr).status_code == 401


# ── 网络门禁: Token 通道默认仅本机/内网 (API_TOKEN_LOCAL_ONLY) ──────

def test_is_local_network_boundaries():
    f = api_gateway.is_local_network
    for local in ("127.0.0.1", "::1", "localhost", "10.0.0.1", "10.255.1.2",
                  "192.168.0.1", "172.16.0.1", "172.31.255.255"):
        assert f(local) is True, local
    # 172.16-31 之外 / 公网 / 空值 / TestClient 伪 peer 均非内网
    for public in ("172.32.0.1", "172.1.0.1", "8.8.8.8", "100.64.0.1",
                   None, "", "testclient"):
        assert f(public) is False, repr(public)


def test_token_network_verdict_local_passes_public_blocks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("API_TOKEN_LOCAL_ONLY", raising=False)  # 默认开
    for ip in ("127.0.0.1", "::1", "192.168.1.10", "172.20.0.9"):
        assert api_gateway.token_network_verdict(ip) is None
    v = api_gateway.token_network_verdict("203.0.113.7")
    assert v is not None and v["status"] == 403
    assert "API_TOKEN_LOCAL_ONLY" in v["detail"] and "SSH" in v["detail"]
    # 未知 peer(如缺 client)按非内网 fail-closed
    assert api_gateway.token_network_verdict("unknown") is not None


def test_token_network_verdict_env_disables_gate(monkeypatch: pytest.MonkeyPatch):
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("API_TOKEN_LOCAL_ONLY", off)
        assert api_gateway.token_network_verdict("203.0.113.7") is None
    # 显式开
    monkeypatch.setenv("API_TOKEN_LOCAL_ONLY", "1")
    assert api_gateway.token_network_verdict("203.0.113.7") is not None


def test_token_gate_public_ip_403_before_token_check(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """公网来源: 先于 evaluate 拦截 — 有效/无效 Token 一律同文案 403。

    有效性信号(401)不外露给公网探测者; 被拦请求不消耗限流额度。
    """
    monkeypatch.delenv("API_TOKEN_LOCAL_ONLY", raising=False)
    monkeypatch.setattr(auth_api, "_client_ip", lambda request: "203.0.113.7")
    _, tok = api_tokens.create_token(tmp_path, "公网调用方", ["read:market"])
    hdr = {"Authorization": f"Bearer {tok}"}

    r1 = client.get("/api/kline/daily", headers=hdr)
    r2 = client.get("/api/kline/daily", headers={"Authorization": "Bearer tsp_deadbeef"})
    assert r1.status_code == r2.status_code == 403
    assert "API_TOKEN_LOCAL_ONLY" in r1.json()["detail"]
    assert "API_TOKEN_LOCAL_ONLY" in r2.json()["detail"]

    # 同一 Token 回到内网: 正常放行且额度未被公网请求消耗 (首扣 → 119)
    monkeypatch.setattr(auth_api, "_client_ip", lambda request: "192.168.1.10")
    r3 = client.get("/api/kline/daily", headers=hdr)
    assert r3.status_code == 200
    assert r3.headers["X-RateLimit-Remaining"] == "119"


def test_token_gate_env_off_allows_public(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """显式放开 (API_TOKEN_LOCAL_ONLY=0): 公网 IP 走完整 evaluate 链路。"""
    monkeypatch.setenv("API_TOKEN_LOCAL_ONLY", "0")
    monkeypatch.setattr(auth_api, "_client_ip", lambda request: "203.0.113.7")
    _, tok = api_tokens.create_token(tmp_path, "公网放开", ["read:market"])
    r = client.get("/api/kline/daily", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    # scope/限流链路照常工作: 无效 Token 仍 401
    bad = client.get("/api/kline/daily", headers={"Authorization": "Bearer tsp_bad"})
    assert bad.status_code == 401
