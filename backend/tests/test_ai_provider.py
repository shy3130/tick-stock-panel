from __future__ import annotations

import tomllib

import httpx
import openai

from app.services import ai_provider
from app.services.ai_provider import (
    _format_openai_error,
    _is_temperature_rejected,
    normalize_openai_base_url,
)


def test_normalize_openai_base_url_adds_v1_for_root_gateway():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_preserves_v1_base():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_strips_chat_completions_path():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1/chat/completions") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_preserves_glm_v4():
    """智谱 GLM 用 /api/paas/v4, 不能强制补成 /v4/v1 (会 404)。"""
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4"


def test_normalize_openai_base_url_strips_chat_completions_from_glm_v4():
    """用户填完整 /v4/chat/completions 时, 去掉后缀归一化为 /v4。"""
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4/chat/completions") == "https://open.bigmodel.cn/api/paas/v4"


def test_normalize_openai_base_url_preserves_other_version_segments():
    """其它非 v1 版本号 (/v2 等) 也应保持原样。"""
    assert normalize_openai_base_url("https://example.com/api/v2") == "https://example.com/api/v2"


def test_normalize_openai_base_url_strips_trailing_slash():
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4/") == "https://open.bigmodel.cn/api/paas/v4"


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


def test_format_openai_error_prefers_upstream_detail_when_available():
    """有可读的上游 detail 时优先透出, 而不是用 400 通用文案吞掉。"""
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

    assert message == "AI 服务请求失败(400): model context length exceeded"


def test_format_openai_error_falls_back_to_status_message_without_detail():
    """上游无可读 detail (如 HTML 网关页) 时, 才回落到 400 通用文案。"""
    response = httpx.Response(
        400,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!DOCTYPE html><html></html>",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError("bad request", response=response, body=None)

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(400): 请求参数无效, 请检查模型名称和上下文长度"


def test_is_temperature_rejected_matches_moonshot_message():
    """Moonshot 对 reasoning 模型报 'only 1 is allowed for this model'。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request",
        response=response,
        body={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
    )
    assert _is_temperature_rejected(exc) is True


def test_is_temperature_rejected_matches_generic_temperature_hint():
    response = httpx.Response(
        400,
        json={"error": {"message": "unsupported parameter: temperature"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "unsupported parameter: temperature"}},
    )
    assert _is_temperature_rejected(exc) is True


def test_is_temperature_rejected_false_for_other_400():
    """非 temperature 相关的 400 (如 model not found) 不应触发去 temperature 重试。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "model not found"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "model not found"}},
    )
    assert _is_temperature_rejected(exc) is False


def test_is_temperature_rejected_false_for_non_400():
    response = httpx.Response(
        401,
        json={"error": {"message": "invalid api key"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.AuthenticationError("unauthorized", response=response, body=None)
    assert _is_temperature_rejected(exc) is False


def test_codex_process_env_excludes_application_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("TICKFLOW_API_KEY", "tickflow-secret")
    monkeypatch.setenv("AI_API_KEY", "ai-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("AUTH_PASSWORD", "password-secret")

    env = ai_provider._codex_process_env(tmp_path / "codex-home")

    assert env["PATH"] == "test-path"
    assert env["HTTPS_PROXY"] == "http://proxy.example"
    assert env["NO_COLOR"] == "1"
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "TICKFLOW_API_KEY" not in env
    assert "AI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AUTH_PASSWORD" not in env


def test_codex_config_adapts_local_access_provider_for_docker(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DOCKER_HOST", "host.docker.internal")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "")
    monkeypatch.setattr(ai_provider, "current_codex_reasoning_effort", lambda: "")
    monkeypatch.setattr(
        ai_provider,
        "_read_codex_config",
        lambda: {
            "model_provider": "codex_local_access",
            "model": "gpt-5.6-sol",
            "model_providers": {
                "codex_local_access": {
                    "name": "Codex API Service",
                    "base_url": "http://localhost:62678/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "supports_websockets": False,
                    "experimental_bearer_token": "local-secret",
                }
            },
        },
    )
    path = tmp_path / "config.toml"

    ai_provider._write_compatible_codex_config(path)

    with path.open("rb") as f:
        config = tomllib.load(f)
    assert config["model_provider"] == "codex_local_access"
    provider = config["model_providers"]["codex_local_access"]
    assert provider["base_url"] == "http://host.docker.internal:62678/v1"
    assert provider["experimental_bearer_token"] == "local-secret"
    assert provider["requires_openai_auth"] is True
    assert provider["supports_websockets"] is False


def test_clamp_ai_max_tokens_bounds(monkeypatch):
    monkeypatch.setattr(ai_provider.settings, "ai_max_tokens", 4500)
    assert ai_provider.clamp_ai_max_tokens(100) == 500
    assert ai_provider.clamp_ai_max_tokens(99999) == 32000
    assert ai_provider.clamp_ai_max_tokens(8000) == 8000
    assert ai_provider.clamp_ai_max_tokens("abc") == 4500  # 坏值回退默认


def test_current_ai_max_tokens_uses_config_default(monkeypatch):
    monkeypatch.setattr(ai_provider.settings, "ai_max_tokens", 4500)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_config", lambda key, default: default)
    assert ai_provider.current_ai_max_tokens() == 4500


def test_current_ai_max_tokens_prefers_secrets_override(monkeypatch):
    monkeypatch.setattr(ai_provider.settings, "ai_max_tokens", 4500)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_config", lambda key, default: "8000")
    assert ai_provider.current_ai_max_tokens() == 8000


def test_current_ai_max_tokens_clamps_override(monkeypatch):
    monkeypatch.setattr(ai_provider.settings, "ai_max_tokens", 4500)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_config", lambda key, default: "99999")
    assert ai_provider.current_ai_max_tokens() == 32000


def test_codex_config_does_not_copy_provider_without_docker_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_DOCKER_HOST", raising=False)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "")
    monkeypatch.setattr(ai_provider, "current_codex_reasoning_effort", lambda: "")
    monkeypatch.setattr(
        ai_provider,
        "_read_codex_config",
        lambda: {
            "model_provider": "codex_local_access",
            "model_providers": {
                "codex_local_access": {
                    "base_url": "http://localhost:62678/v1",
                    "experimental_bearer_token": "must-not-leak",
                }
            },
        },
    )
    path = tmp_path / "config.toml"

    ai_provider._write_compatible_codex_config(path)

    text = path.read_text(encoding="utf-8")
    assert "model_provider" not in text
    assert "model_providers" not in text
    assert "must-not-leak" not in text
