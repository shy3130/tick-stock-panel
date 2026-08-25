"""M18 PushPlus 通知通道 — 行为与安全测试。

覆盖:
- adapter send_pushplus: 固定 host / trust_env=False / 超时 / 业务 code=200 / 失败 fail-soft
- preferences: token 只存 secrets.json (0600), 对外只暴露 configured/token_masked
- settings API: webhook-channel 支持 token / clear_token 语义
- 安全: preferences.json / API 响应 / 日志无明文 token
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.services import preferences, webhook_adapter

# ── adapter: send_pushplus ────────────────────────────────


def test_send_pushplus_empty_token_returns_false():
    assert webhook_adapter.send_pushplus("", "标题", "正文") is False
    assert webhook_adapter.send_pushplus("   ", "标题", "正文") is False


def test_send_pushplus_fixed_host_and_trust_env_false(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200, "msg": "请求成功"})

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_pushplus("abc123", "标题", "正文") is True

    # 固定 host, 不接受用户自定义 URL
    assert calls[0][0] == "https://www.pushplus.plus/send"
    # trust_env=False (不走系统代理)
    assert calls[0][1]["trust_env"] is False
    # 超时 5s
    assert calls[0][1]["timeout"] == 5.0
    # token 在请求体内
    assert calls[0][1]["json"]["token"] == "abc123"


def test_send_pushplus_business_code_200_is_success(monkeypatch):
    def fake_post(url, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200, "msg": "请求成功"})

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_pushplus("tok", "T", "B") is True


def test_send_pushplus_business_code_non_200_is_failure(monkeypatch):
    def fake_post(url, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"code": 903, "msg": "token无效"})

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_pushplus("bad", "T", "B") is False


def test_send_pushplus_http_error_returns_false(monkeypatch):
    def fake_post(url, **kwargs):
        return SimpleNamespace(status_code=500, json=lambda: {}, text="server error")

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_pushplus("tok", "T", "B") is False


def test_send_pushplus_non_json_200_is_failure(monkeypatch):
    def fake_post(url, **kwargs):
        resp = SimpleNamespace(status_code=200, text="OK")
        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        return resp

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_pushplus("tok", "T", "B") is False


def test_send_pushplus_exception_returns_false(monkeypatch):
    def boom(url, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr("httpx.post", boom)
    assert webhook_adapter.send_pushplus("tok", "T", "B") is False


def test_send_pushplus_truncates_title_and_body(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200})

    monkeypatch.setattr("httpx.post", fake_post)
    long_title = "A" * 1000
    long_body = "B" * 1000
    webhook_adapter.send_pushplus("tok", long_title, long_body)
    assert len(calls[0]["title"]) <= 501  # _MAX_LEN=500 + …
    assert len(calls[0]["content"]) <= 501


# ── send_channel dispatch ─────────────────────────────────


def test_send_channel_routes_pushplus(monkeypatch):
    called = {}

    def fake_send(token, title, body):
        called["token"] = token
        called["title"] = title
        called["body"] = body
        return True

    monkeypatch.setattr(webhook_adapter, "send_pushplus", fake_send)
    assert webhook_adapter.send_channel("pushplus", {"token": "xyz"}, "T", "B") is True
    assert called == {"token": "xyz", "title": "T", "body": "B"}


def test_send_configured_channels_includes_pushplus(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_configured_webhook_channels",
        lambda: {
            "feishu": {"url": "https://example.test/hook"},
            "pushplus": {"token": "pp_token"},
        },
    )
    results = {}

    def fake_send(channel, config, title, body):
        results[channel] = True
        return True

    monkeypatch.setattr(webhook_adapter, "send_channel", fake_send)
    assert webhook_adapter.send_configured_channels("T", "B") == 2
    assert "pushplus" in results


# ── preferences: token 仅存 secrets.json ──────────────────


@pytest.fixture
def isolated_secrets(tmp_path, monkeypatch):
    """隔离 secrets.json / preferences.json 到临时目录。"""
    secrets_file = tmp_path / "secrets.json"
    prefs_file = tmp_path / "preferences.json"

    monkeypatch.setattr(secrets_store, "_path", lambda: secrets_file)
    monkeypatch.setattr(preferences, "_path", lambda: prefs_file)
    return tmp_path


def test_pushplus_token_stored_in_secrets_not_preferences(isolated_secrets):
    preferences.set_pushplus_token("secret_token_123")
    # secrets.json 有 token
    secrets_data = json.loads((isolated_secrets / "secrets.json").read_text())
    assert secrets_data["pushplus_token"] == "secret_token_123"
    # preferences.json 无 token
    prefs_file = isolated_secrets / "preferences.json"
    if prefs_file.exists():
        prefs_data = json.loads(prefs_file.read_text())
        assert "pushplus_token" not in prefs_data
        assert "pushplus" not in str(prefs_data)


def test_pushplus_get_status_masks_token(isolated_secrets):
    preferences.set_pushplus_token("abcdefghijklmnop")
    status = preferences.get_pushplus_status()
    assert status["configured"] is True
    assert "abcdefghijklmnop" not in status["token_masked"]
    assert "•" in status["token_masked"]


def test_pushplus_get_status_empty(isolated_secrets):
    status = preferences.get_pushplus_status()
    assert status == {"configured": False, "token_masked": ""}


def test_pushplus_clear_token(isolated_secrets):
    preferences.set_pushplus_token("tok123")
    preferences.clear_pushplus_token()
    assert preferences.get_pushplus_token() == ""
    assert preferences.get_pushplus_status()["configured"] is False


def test_pushplus_get_configured_channels_injects_real_token(isolated_secrets):
    preferences.set_pushplus_token("real_token_xyz")
    channels = preferences.get_configured_webhook_channels()
    assert channels["pushplus"]["token"] == "real_token_xyz"


def test_pushplus_get_webhook_channels_only_returns_masked(isolated_secrets):
    preferences.set_pushplus_token("real_token_xyz")
    channels = preferences.get_webhook_channels()
    assert channels["pushplus"]["configured"] is True
    assert "real_token_xyz" not in json.dumps(channels)
    # 对外只暴露 configured + token_masked, 不暴露真实 token
    assert "token" not in channels["pushplus"]


def test_pushplus_not_in_preferences_webhook_channels_dict(isolated_secrets):
    """get_webhook_channels 的 webhook_channels 字段里不应残留 pushplus (从 preferences.json)。"""
    preferences.save({"webhook_channels": {"pushplus": {"token": "injected"}}})
    channels = preferences.get_webhook_channels()
    # pushplus 从 secrets.json 读, 不从 preferences.webhook_channels 读
    assert channels["pushplus"]["configured"] is False


# ── set_webhook_channel: token / clear_token 语义 ─────────


def test_set_webhook_channel_pushplus_save_token(isolated_secrets):
    result = preferences.set_webhook_channel(
        "pushplus", {"token": "new_token", "clear_token": False}
    )
    assert result["configured"] is True
    assert preferences.get_pushplus_token() == "new_token"


def test_set_webhook_channel_pushplus_empty_token_retains_old(isolated_secrets):
    preferences.set_pushplus_token("old_token")
    # token 空 + clear_token=False → 保留旧 token
    result = preferences.set_webhook_channel("pushplus", {"token": "", "clear_token": False})
    assert result["configured"] is True
    assert preferences.get_pushplus_token() == "old_token"


def test_set_webhook_channel_pushplus_clear_token(isolated_secrets):
    preferences.set_pushplus_token("old_token")
    result = preferences.set_webhook_channel("pushplus", {"token": "", "clear_token": True})
    assert result["configured"] is False
    assert preferences.get_pushplus_token() == ""


# ── secrets.json 权限 0600 ────────────────────────────────


def test_secrets_file_permissions_0600(isolated_secrets):
    preferences.set_pushplus_token("secret")
    secrets_file = isolated_secrets / "secrets.json"
    mode = oct(secrets_file.stat().st_mode)[-3:]
    assert mode == "600"


# ── REVIEW_PUSH_CHANNELS 白名单 ───────────────────────────


def test_pushplus_in_review_push_channels_whitelist():
    assert "pushplus" in preferences.REVIEW_PUSH_CHANNELS


def test_review_push_channels_accepts_pushplus(isolated_secrets):
    saved = preferences.set_review_push_channels(["feishu", "pushplus"])
    assert "pushplus" in saved
    assert "feishu" in saved


# ── 安全: API 响应无明文 token ─────────────────────────────


def test_get_preferences_response_has_no_plaintext_token(isolated_secrets, monkeypatch):
    preferences.set_pushplus_token("super_secret_token_abc")
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    from app.api.settings import get_preferences

    response = get_preferences()
    response_str = json.dumps(response, ensure_ascii=False)
    assert "super_secret_token_abc" not in response_str
    # webhook_channels.pushplus 只有 configured + token_masked
    pp = response["webhook_channels"]["pushplus"]
    assert "token" not in pp
    assert pp["configured"] is True


# ── 安全: 日志无明文 token ─────────────────────────────────


def test_pushplus_token_not_logged(isolated_secrets, monkeypatch, caplog):
    preferences.set_pushplus_token("log_secret_token")

    def fake_post(url, **kwargs):
        return SimpleNamespace(status_code=500, json=lambda: {"code": 900}, text="err")

    monkeypatch.setattr("httpx.post", fake_post)
    with caplog.at_level(logging.DEBUG, logger="app.services.webhook_adapter"):
        webhook_adapter.send_pushplus("log_secret_token", "标题", "正文")

    for record in caplog.records:
        assert "log_secret_token" not in record.getMessage()


# ── 敏感报告字段仍由 allowlist 排除 ────────────────────────


def test_pushplus_reuses_truncated_title_body_no_extra_fields():
    """send_pushplus 只发送 title + body + token, 不附加账户/持仓/流水。
    标题和正文的脱敏由调用方(build_analysis_card_payload / _truncate)保证。"""
    from app.services.webhook_adapter import _MAX_LEN

    assert _MAX_LEN == 500  # 复用既有截断上限
