"""扩展数据拉取的每配置超时 (PullConfig.timeout_seconds)。

历史行为是三处硬编码 30s: 大响应接口 (如全量集合竞价 /day, 实测 ~77s) 会超时。
现在超时进配置, 默认 30 与旧行为完全一致; 拉取 (_request_json) 与
URL 探测 (detect-url) 都读它。老 config.json 无该字段 → 默认 30, 手改非法值 → 归一 30。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import ext_pull
from app.services.ext_data import PullConfig

# ── 模型: 默认值 / 序列化 / 非法值归一 ──────────────────────────────

def test_timeout_defaults_to_30_and_roundtrips():
    assert PullConfig().timeout_seconds == 30
    assert PullConfig(timeout_seconds=90).timeout_seconds == 90

    d = PullConfig(url="https://x", timeout_seconds=120).to_dict()
    assert d["timeout_seconds"] == 120
    assert PullConfig.from_dict(d).timeout_seconds == 120


def test_timeout_missing_in_old_config_falls_back_to_30():
    legacy = PullConfig(url="https://x").to_dict()
    del legacy["timeout_seconds"]  # 模拟旧版本 config.json
    assert PullConfig.from_dict(legacy).timeout_seconds == 30


@pytest.mark.parametrize("bad", [0, -5, 3, 301, 9999, "abc", None])
def test_timeout_invalid_values_normalized_to_30(bad):
    assert PullConfig(timeout_seconds=bad).timeout_seconds == 30


# ── 拉取路径: _request_json 把配置超时传给 httpx ─────────────────────

class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"items": [{"symbol": "600000.SH"}]}


class _FakeClient:
    """捕获每次请求的 timeout。"""

    last_timeout: object = None
    request_timeout: object = None

    def __init__(self, timeout=None, **kwargs):
        _FakeClient.last_timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        _FakeClient.request_timeout = kwargs.get("timeout")
        return _FakeResponse()


async def test_request_json_uses_configured_timeout(monkeypatch):
    monkeypatch.setattr(ext_pull.httpx, "AsyncClient", _FakeClient)

    pull = PullConfig(url="https://x", timeout_seconds=90)
    await ext_pull._request_json(pull, "cfg1")
    assert _FakeClient.request_timeout == 90


async def test_request_json_defaults_to_30_without_attr(monkeypatch):
    monkeypatch.setattr(ext_pull.httpx, "AsyncClient", _FakeClient)

    # 测试用的简化 pull 对象没有 timeout_seconds 属性 → 兜底 30
    pull = SimpleNamespace(
        url="https://x", date_param=None, date_format="iso", headers=None,
        method="GET", body=None, auth=None,
    )
    await ext_pull._request_json(pull, "cfg1")
    assert _FakeClient.request_timeout == 30


# ── 探测路径: detect-url 请求体带 timeout_seconds ───────────────────

async def test_detect_url_uses_request_timeout(monkeypatch):
    import httpx

    from app.api import ext_data as ext_api

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    resp = await ext_api.detect_url(ext_api.DetectUrlReq(
        url="https://x", response_path="items", timeout_seconds=120,
    ))
    assert _FakeClient.last_timeout == 120
    assert resp["status"] == "ok"


async def test_detect_url_timeout_defaults_to_30(monkeypatch):
    import httpx

    from app.api import ext_data as ext_api

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await ext_api.detect_url(ext_api.DetectUrlReq(url="https://x", response_path="items"))
    assert _FakeClient.last_timeout == 30
