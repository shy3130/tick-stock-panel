from types import SimpleNamespace

from app.services import webhook_adapter


def test_meow_encodes_path_and_disables_env_proxy(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("httpx.post", fake_post)

    assert webhook_adapter.send_meow("猫 用户", "标题 A", "内容 **md**")

    assert calls[0][0] == "https://api.chuckfang.com/%E7%8C%AB%20%E7%94%A8%E6%88%B7/%E6%A0%87%E9%A2%98%20A/%E5%86%85%E5%AE%B9%20%2A%2Amd%2A%2A"
    assert calls[0][1]["trust_env"] is False


def test_send_configured_channels_counts_success(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_configured_webhook_channels",
        lambda: {
            "dingtalk": {"url": "https://example.test/ding"},
            "meow": {"nickname": "me"},
        },
    )
    monkeypatch.setattr(webhook_adapter, "send_channel", lambda ch, cfg, title, body: ch == "meow")

    assert webhook_adapter.send_configured_channels("T", "B") == 1


def test_webhook_url_allowlists_block_ssrf():
    assert webhook_adapter.is_valid_dingtalk_url("https://oapi.dingtalk.com/robot/send?access_token=abc")
    assert webhook_adapter.is_valid_wecom_url("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
    assert not webhook_adapter.is_valid_dingtalk_url("http://127.0.0.1/robot/send?access_token=abc")
    assert not webhook_adapter.is_valid_wecom_url("https://169.254.169.254/cgi-bin/webhook/send?key=abc")
