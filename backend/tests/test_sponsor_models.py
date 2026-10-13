"""sponsor-models 代理端点的精简字段契约测试。

上游 /v1/models 原始体量大 (144KB, 含分层 tiers/dimensions 等细粒度定价);
端点只透传前端展示需要的 id / context_length / discount_rate / 首档单价,
并以 models 纯 id 列表保持向后兼容。
"""
from __future__ import annotations

import httpx
import pytest

from app.api import settings as settings_api


def _fake_upstream(payload: dict):
    """把 httpx.AsyncClient 换成返回固定 /models 响应的伪实现。"""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            assert url == "https://llm.runninghub.ai/v1/models"
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_sponsor_models_returns_trimmed_pricing(monkeypatch):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "deepseek/deepseek-v4.1-flash",
                "object": "model",
                "owned_by": "runninghub",
                "context_length": 1048576,
                "pricing": {
                    "billingType": "TOKEN_INPUT_TIERED",
                    "discountRate": 0.6,
                    "input": {"amount": 0.000295, "unit": "USD/1K", "tiers": []},
                    "output": {"amount": 0.001177, "unit": "USD/1K", "tiers": []},
                    "dimensions": {"cache_read": {"amount": 0.00001}},
                },
            },
            {
                "id": "openai/gpt-6.1-sol",
                "object": "model",
                "context_length": 1050000,
                "pricing": {"discountRate": 1.0, "input": {"amount": 0.002}, "output": {"amount": 0.01}},
            },
            {"id": "", "object": "model"},          # 空 id 剔除
            {"object": "model"},                     # 无 id 剔除
            "garbage",                               # 非 dict 剔除
        ],
    }
    monkeypatch.setattr(httpx, "AsyncClient", _fake_upstream(payload))

    result = await settings_api.list_sponsor_models()

    assert result["models"] == ["deepseek/deepseek-v4.1-flash", "openai/gpt-6.1-sol"]
    assert result["items"] == [
        {
            "id": "deepseek/deepseek-v4.1-flash",
            "context_length": 1048576,
            "discount_rate": 0.6,
            "input_price": 0.000295,
            "output_price": 0.001177,
        },
        {
            "id": "openai/gpt-6.1-sol",
            "context_length": 1050000,
            "discount_rate": 1.0,
            "input_price": 0.002,
            "output_price": 0.01,
        },
    ]


@pytest.mark.asyncio
async def test_sponsor_models_tolerates_missing_pricing(monkeypatch):
    """个别模型无 pricing 块: discount_rate 回退 1.0(无折扣), 价格为 None。"""
    payload = {"data": [{"id": "vendor/plain-model", "context_length": None}]}
    monkeypatch.setattr(httpx, "AsyncClient", _fake_upstream(payload))

    result = await settings_api.list_sponsor_models()

    assert result["items"] == [{
        "id": "vendor/plain-model",
        "context_length": None,
        "discount_rate": 1.0,
        "input_price": None,
        "output_price": None,
    }]


@pytest.mark.asyncio
async def test_sponsor_models_maps_upstream_error_to_502(monkeypatch):
    from fastapi import HTTPException

    class _Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _Broken)

    with pytest.raises(HTTPException) as exc_info:
        await settings_api.list_sponsor_models()
    assert exc_info.value.status_code == 502
