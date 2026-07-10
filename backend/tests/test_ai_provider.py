from __future__ import annotations

import httpx
import openai

from app.services.ai_provider import _format_openai_error, normalize_openai_base_url


def test_normalize_openai_base_url_adds_v1_for_root_gateway():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_preserves_v1_base():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_strips_chat_completions_path():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1/chat/completions") == "http://ai.zedbox.cn:8080/v1"


def test_format_openai_error_hides_html_gateway_body():
    response = httpx.Response(
        504,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!DOCTYPE html><html><body><h1>Gateway Timeout</h1></body></html>",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.InternalServerError("gateway timeout", response=response, body=response.text)

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(504): AI 上游服务超时, 请稍后重试或检查 AI Base URL / 网络"
    assert "html" not in message.lower()
    assert "Gateway Timeout" not in message


def test_format_openai_error_uses_status_message_when_available():
    response = httpx.Response(
        400,
        json={"error": {"message": "model context length exceeded"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request",
        response=response,
        body={"error": {"message": "model context length exceeded"}},
    )

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(400): 请求参数无效, 请检查模型名称和上下文长度"
