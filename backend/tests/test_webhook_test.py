"""Webhook 测试消息功能 — 向已保存的 Webhook 地址发送测试消息验证配置。

纯逻辑，不触网（monkeypatch webhook_adapter.send_*），直接调用 settings 端点函数。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.settings import (
    WebhookTestIn,
)
from app.api.settings import (
    test_webhook as run_webhook_test,
)

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


def test_feishu_sends_saved_url_and_secret(monkeypatch):
    calls = {}
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: FEISHU_URL)
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "my-secret")
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_feishu",
        lambda url, title, body, secret="", max_attempts=3: calls.update(
            url=url, title=title, body=body, secret=secret, max_attempts=max_attempts,
        ) or True,
    )

    result = run_webhook_test(WebhookTestIn(channel="feishu"))

    assert result["ok"] is True
    assert calls["url"] == FEISHU_URL
    assert calls["secret"] == "my-secret"
    # 诊断路径单次尝试, 不等生产退避重试
    assert calls["max_attempts"] == 1
    assert calls["title"] == "TickFlow Stock Panel 推送测试"
    assert "推送配置正确" in calls["body"]


def test_feishu_send_failure_returns_ok_false(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: FEISHU_URL)
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_secret", lambda: "")
    monkeypatch.setattr("app.services.webhook_adapter.send_feishu", lambda *a, **k: False)

    result = run_webhook_test(WebhookTestIn(channel="feishu"))

    assert result["ok"] is False
    assert "推送失败" in result["detail"]


@pytest.mark.parametrize("channel,url_getter", [
    ("feishu", "app.services.preferences.get_feishu_webhook_url"),
    ("wecom", "app.services.preferences.get_wecom_webhook_url"),
])
def test_not_configured_returns_ok_false(monkeypatch, channel, url_getter):
    monkeypatch.setattr(url_getter, lambda: "")

    result = run_webhook_test(WebhookTestIn(channel=channel))

    assert result["ok"] is False
    assert "尚未配置" in result["detail"]


def test_invalid_saved_feishu_url_returns_ok_false(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "https://evil.example/hook/xx")

    result = run_webhook_test(WebhookTestIn(channel="feishu"))

    assert result["ok"] is False
    assert "地址非法" in result["detail"]


def test_wecom_sends_saved_url(monkeypatch):
    calls = {}
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: WECOM_URL)
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_wecom",
        lambda url, title, body: calls.update(url=url, title=title, body=body) or True,
    )

    result = run_webhook_test(WebhookTestIn(channel="wecom"))

    assert result["ok"] is True
    assert calls["url"] == WECOM_URL
    assert calls["title"] == "TickFlow Stock Panel 推送测试"
    assert "推送配置正确" in calls["body"]


def test_unknown_channel_rejected_by_pydantic():
    with pytest.raises(ValidationError):
        WebhookTestIn(channel="wecom-bot")
