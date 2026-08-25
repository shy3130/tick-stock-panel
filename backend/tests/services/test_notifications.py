"""多通道通知格式化 / 路由测试 — 纯函数行为验证。

覆盖:
  - 渠道选择 (有效 / 空 / 未知 / 去重)
  - 敏感字段脱敏 (mapping 递归 + 文本模式)
  - 长度截断 (每渠道上限)
  - 每渠道 payload 结构与确定性
  - fail-soft (单渠道异常不阻断其它)
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.services.notifications import (
    CHANNEL_LIMITS,
    SUPPORTED_CHANNELS,
    ChannelPayload,
    DispatchResult,
    NotificationEvent,
    format_email,
    format_feishu,
    format_gotify,
    format_ntfy,
    format_pushover,
    format_slack,
    parse_channels,
    redact_sensitive,
    redact_text,
    route_event,
    split_valid_channels,
)
from app.services.notifications import (
    EMAIL_BODY_MAX,
    EMAIL_SUBJECT_MAX,
    FEISHU_MESSAGE_MAX,
    FEISHU_TITLE_MAX,
    GOTIFY_MESSAGE_MAX,
    GOTIFY_TITLE_MAX,
    NTFY_MESSAGE_MAX,
    NTFY_TITLE_MAX,
    PUSHOVER_MESSAGE_MAX,
    PUSHOVER_TITLE_MAX,
    SLACK_BLOCK_TEXT_MAX,
    SLACK_TEXT_MAX,
)


# ── 测试 fixtures ─────────────────────────────────────────


def _event(
    title: str = "测试标题",
    message: str = "这是一条**测试**消息",
    **kw,
) -> NotificationEvent:
    return NotificationEvent(title=title, message=message, **kw)


# ── 渠道解析 / 选择 ────────────────────────────────────────


class TestParseChannels:
    def test_string_comma_separated(self):
        assert parse_channels("feishu, ntfy, slack") == ["feishu", "ntfy", "slack"]

    def test_list(self):
        assert parse_channels(["feishu", "slack"]) == ["feishu", "slack"]

    def test_none_returns_empty(self):
        assert parse_channels(None) == []

    def test_empty_string(self):
        assert parse_channels("") == []

    def test_whitespace_and_case_normalized(self):
        assert parse_channels("  Feishu , SLACK ") == ["feishu", "slack"]

    def test_strips_empty_tokens(self):
        assert parse_channels("feishu,, ,ntfy") == ["feishu", "ntfy"]

    def test_single_value_non_iterable(self):
        assert parse_channels(42) == ["42"]


class TestSplitValidChannels:
    def test_all_valid(self):
        valid, invalid = split_valid_channels(["feishu", "ntfy", "slack"])
        assert valid == ["feishu", "ntfy", "slack"]
        assert invalid == []

    def test_mix_valid_invalid(self):
        valid, invalid = split_valid_channels(["feishu", "wechat", "ntfy", "unknown_ch"])
        assert valid == ["feishu", "ntfy"]
        assert invalid == ["wechat", "unknown_ch"]

    def test_dedup_preserves_order(self):
        valid, invalid = split_valid_channels(["ntfy", "feishu", "ntfy", "feishu"])
        assert valid == ["ntfy", "feishu"]
        assert invalid == []

    def test_dedup_invalid_preserves_order(self):
        valid, invalid = split_valid_channels(["bad1", "bad1", "bad2"])
        assert valid == []
        assert invalid == ["bad1", "bad2"]

    def test_empty(self):
        valid, invalid = split_valid_channels([])
        assert valid == []
        assert invalid == []

    def test_all_unknown(self):
        valid, invalid = split_valid_channels(["foo", "bar"])
        assert valid == []
        assert invalid == ["foo", "bar"]

    def test_accepts_string_input(self):
        valid, invalid = split_valid_channels("feishu, foo")
        assert valid == ["feishu"]
        assert invalid == ["foo"]


# ── 敏感字段脱敏 ──────────────────────────────────────────


class TestRedactSensitive:
    def test_simple_sensitive_key(self):
        d = {"token": "abc123", "name": "visible"}
        out = redact_sensitive(d)
        assert out["token"] == "[REDACTED]"
        assert out["name"] == "visible"

    def test_nested_dict(self):
        d = {"outer": {"api_key": "sk-xxx", "visible": "ok"}}
        out = redact_sensitive(d)
        assert out["outer"]["api_key"] == "[REDACTED]"
        assert out["outer"]["visible"] == "ok"

    def test_list_values(self):
        d = [{"token": "t1"}, {"token": "t2"}]
        out = redact_sensitive(d)
        assert out[0]["token"] == "[REDACTED]"
        assert out[1]["token"] == "[REDACTED]"

    def test_tuple_values(self):
        d = ({"secret": "s1"},)
        out = redact_sensitive(d)
        assert out[0]["secret"] == "[REDACTED]"

    def test_camelcase_key(self):
        d = {"apiKey": "xxx", "userToken": "yyy"}
        out = redact_sensitive(d)
        assert out["apiKey"] == "[REDACTED]"
        assert out["userToken"] == "[REDACTED]"

    def test_hyphenated_key(self):
        d = {"api-key": "xxx", "webhook-url": "yyy"}
        out = redact_sensitive(d)
        assert out["api-key"] == "[REDACTED]"
        assert out["webhook-url"] == "[REDACTED]"

    def test_non_sensitive_passthrough(self):
        d = {"title": "report", "message": "body", "count": 42, "flag": True}
        out = redact_sensitive(d)
        assert out == d

    def test_scalars_passthrough(self):
        assert redact_sensitive("hello") == "hello"
        assert redact_sensitive(42) == 42
        assert redact_sensitive(None) is None

    def test_does_not_mutate_input(self):
        original = {"token": "secret", "data": {"key": "val"}}
        snapshot = copy.deepcopy(original)
        _ = redact_sensitive(original)
        assert original == snapshot

    def test_empty_dict(self):
        assert redact_sensitive({}) == {}

    def test_all_sensitive_variants(self):
        keys = [
            "authorization", "cookie", "password", "secret", "sendkey",
            "token", "webhook", "apikey", "api_token", "access_token",
            "refresh_token", "auth_token", "session_token", "app_secret",
            "user_key", "bot_token", "app_id", "chat_id", "sign",
        ]
        for k in keys:
            assert redact_sensitive({k: "val"})[k] == "[REDACTED]"

    def test_deeply_nested(self):
        d = {"a": {"b": {"c": {"password": "deep"}}}}
        out = redact_sensitive(d)
        assert out["a"]["b"]["c"]["password"] == "[REDACTED]"

    def test_mixed_sensitive_and_clean(self):
        d = {
            "title": "Report",
            "api_key": "sk-123",
            "data": {"name": "stock", "token": "abc", "items": [1, 2]},
        }
        out = redact_sensitive(d)
        assert out["title"] == "Report"
        assert out["api_key"] == "[REDACTED]"
        assert out["data"]["name"] == "stock"
        assert out["data"]["token"] == "[REDACTED]"
        assert out["data"]["items"] == [1, 2]


class TestRedactText:
    def test_key_value_assignment(self):
        assert "sk-123" not in redact_text("token=sk-123")
        assert "***" in redact_text("token=sk-123")

    def test_colon_assignment(self):
        redacted = redact_text("api_key: my-secret-value")
        assert "my-secret-value" not in redacted

    def test_bearer_token(self):
        redacted = redact_text("Authorization: Bearer eyJhbGciOi...")
        assert "eyJhbGciOi..." not in redacted
        assert "***" in redacted

    def test_token_like_pattern(self):
        redacted = redact_text("key is sk-1234567890abcdef1234")
        assert "sk-1234567890abcdef1234" not in redacted

    def test_clean_text_unchanged(self):
        assert redact_text("这是一条普通消息") == "这是一条普通消息"

    def test_none(self):
        assert redact_text(None) == ""

    def test_multiple_patterns(self):
        text = "token=abc123 secret=xyz789"
        redacted = redact_text(text)
        assert "abc123" not in redacted
        assert "xyz789" not in redacted


# ── 每渠道 payload 结构 ────────────────────────────────────


class TestFeishuStructure:
    def test_basic_structure(self):
        payload = format_feishu(_event())
        assert payload["msg_type"] == "interactive"
        assert "card" in payload
        assert payload["card"]["config"]["wide_screen_mode"] is True
        assert payload["card"]["header"]["title"]["tag"] == "plain_text"
        assert payload["card"]["header"]["title"]["content"] == "测试标题"
        elements = payload["card"]["elements"]
        assert len(elements) >= 1
        assert elements[0]["tag"] == "div"
        assert elements[0]["text"]["tag"] == "lark_md"
        assert "**测试**" in elements[0]["text"]["content"]

    def test_empty_title_defaults(self):
        payload = format_feishu(_event(title="", message="body"))
        assert payload["card"]["header"]["title"]["content"] == "通知"

    def test_truncation(self):
        long_msg = "x" * (FEISHU_MESSAGE_MAX + 500)
        payload = format_feishu(_event(message=long_msg))
        content = payload["card"]["elements"][0]["text"]["content"]
        assert len(content) <= FEISHU_MESSAGE_MAX
        assert content.endswith("…")

    def test_title_truncation(self):
        long_title = "T" * (FEISHU_TITLE_MAX + 50)
        payload = format_feishu(_event(title=long_title))
        title = payload["card"]["header"]["title"]["content"]
        assert len(title) <= FEISHU_TITLE_MAX

    def test_sensitive_in_message_redacted(self):
        payload = format_feishu(_event(message="token=leaked-secret"))
        content = payload["card"]["elements"][0]["text"]["content"]
        assert "leaked-secret" not in content


class TestEmailStructure:
    def test_basic_structure(self):
        payload = format_email(_event())
        assert "subject" in payload
        assert "body_text" in payload
        assert "body_html" in payload
        assert payload["subject"] == "测试标题"
        assert "**测试**" in payload["body_text"]
        assert "<div>" in payload["body_html"]

    def test_html_escapes_entities(self):
        payload = format_email(_event(message="<script>alert(1)</script>"))
        assert "<script>" not in payload["body_html"]
        assert "&lt;script&gt;" in payload["body_html"]

    def test_html_bold_conversion(self):
        payload = format_email(_event(message="**important**"))
        assert "<strong>important</strong>" in payload["body_html"]

    def test_truncation(self):
        long_msg = "y" * (EMAIL_BODY_MAX + 100)
        payload = format_email(_event(message=long_msg))
        assert len(payload["body_text"]) <= EMAIL_BODY_MAX

    def test_subject_truncation(self):
        long_sub = "S" * (EMAIL_SUBJECT_MAX + 50)
        payload = format_email(_event(title=long_sub))
        assert len(payload["subject"]) <= EMAIL_SUBJECT_MAX


class TestNtfyStructure:
    def test_basic_structure(self):
        payload = format_ntfy(_event())
        assert payload["topic"] == ""
        assert payload["title"] == "测试标题"
        assert "**测试**" in payload["message"]
        assert payload["markdown"] is True

    def test_truncation(self):
        long_msg = "z" * (NTFY_MESSAGE_MAX + 50)
        payload = format_ntfy(_event(message=long_msg))
        assert len(payload["message"]) <= NTFY_MESSAGE_MAX

    def test_title_truncation(self):
        long_title = "N" * (NTFY_TITLE_MAX + 20)
        payload = format_ntfy(_event(title=long_title))
        assert len(payload["title"]) <= NTFY_TITLE_MAX


class TestSlackStructure:
    def test_basic_structure(self):
        payload = format_slack(_event())
        assert "text" in payload
        assert payload["mrkdwn"] is True
        assert "blocks" in payload
        assert len(payload["blocks"]) >= 1
        block = payload["blocks"][0]
        assert block["type"] == "section"
        assert block["text"]["type"] == "mrkdwn"

    def test_block_splitting(self):
        long_msg = "a" * (SLACK_BLOCK_TEXT_MAX * 2 + 100)
        payload = format_slack(_event(message=long_msg))
        assert len(payload["blocks"]) == 3
        for block in payload["blocks"]:
            assert len(block["text"]["text"]) <= SLACK_BLOCK_TEXT_MAX

    def test_text_truncation(self):
        long_msg = "b" * (SLACK_TEXT_MAX + 1000)
        payload = format_slack(_event(message=long_msg))
        assert len(payload["text"]) <= SLACK_TEXT_MAX

    def test_empty_message_fallback(self):
        payload = format_slack(_event(title="仅标题", message=""))
        assert payload["text"] == "仅标题"
        assert len(payload["blocks"]) == 1


class TestPushoverStructure:
    def test_basic_structure(self):
        payload = format_pushover(_event())
        assert payload["title"] == "测试标题"
        assert "priority" in payload
        assert payload["priority"] == 0
        # markdown stripped
        assert "**" not in payload["message"]
        assert "测试" in payload["message"]

    def test_priority_clamp_high(self):
        payload = format_pushover(_event(priority=10))
        assert payload["priority"] == 2

    def test_priority_clamp_low(self):
        payload = format_pushover(_event(priority=-10))
        assert payload["priority"] == -2

    def test_no_secrets_in_payload(self):
        payload = format_pushover(_event())
        assert "token" not in payload
        assert "user" not in payload

    def test_truncation(self):
        long_msg = "p" * (PUSHOVER_MESSAGE_MAX + 50)
        payload = format_pushover(_event(message=long_msg))
        assert len(payload["message"]) <= PUSHOVER_MESSAGE_MAX

    def test_title_truncation(self):
        long_title = "P" * (PUSHOVER_TITLE_MAX + 30)
        payload = format_pushover(_event(title=long_title))
        assert len(payload["title"]) <= PUSHOVER_TITLE_MAX

    def test_markdown_links_stripped(self):
        payload = format_pushover(_event(message="[点击](https://example.com)"))
        assert "https://example.com" not in payload["message"]
        assert "点击" in payload["message"]


class TestGotifyStructure:
    def test_basic_structure(self):
        payload = format_gotify(_event())
        assert payload["title"] == "测试标题"
        assert "**测试**" in payload["message"]
        assert payload["extras"]["client::display"]["contentType"] == "text/markdown"

    def test_truncation(self):
        long_msg = "g" * (GOTIFY_MESSAGE_MAX + 50)
        payload = format_gotify(_event(message=long_msg))
        assert len(payload["message"]) <= GOTIFY_MESSAGE_MAX

    def test_title_truncation(self):
        long_title = "G" * (GOTIFY_TITLE_MAX + 20)
        payload = format_gotify(_event(title=long_title))
        assert len(payload["title"]) <= GOTIFY_TITLE_MAX


# ── 路由 / DispatchResult ─────────────────────────────────


class TestRouteEvent:
    def test_all_supported_channels(self):
        result = route_event(_event(), SUPPORTED_CHANNELS)
        assert isinstance(result, DispatchResult)
        assert len(result.payloads) == len(SUPPORTED_CHANNELS)
        assert result.errors == []
        assert result.skipped_unknown == []
        assert result.route_type == "report"
        assert result.dry_run is True

    def test_channel_subset(self):
        result = route_event(_event(), ["feishu", "ntfy"])
        assert set(result.channels) == {"feishu", "ntfy"}
        assert len(result.payloads) == 2

    def test_empty_channels(self):
        result = route_event(_event(), [])
        assert result.payloads == []
        assert result.errors == []
        assert result.skipped_unknown == []

    def test_none_channels(self):
        result = route_event(_event(), None)
        assert result.payloads == []
        assert result.skipped_unknown == []

    def test_unknown_channels_recorded(self):
        result = route_event(_event(), "feishu, wechat, unknown_ch, ntfy")
        assert set(result.channels) == {"feishu", "ntfy"}
        assert result.skipped_unknown == ["wechat", "unknown_ch"]

    def test_all_unknown(self):
        result = route_event(_event(), ["foo", "bar"])
        assert result.payloads == []
        assert result.skipped_unknown == ["foo", "bar"]

    def test_string_input(self):
        result = route_event(_event(), "feishu,slack")
        assert set(result.channels) == {"feishu", "slack"}

    def test_dry_run_always_true(self):
        result = route_event(_event(), "feishu", dry_run=True)
        assert result.dry_run is True

    def test_route_type_normalization(self):
        result = route_event(_event(route_type="alert"), "feishu")
        assert result.route_type == "alert"

    def test_route_type_unknown_defaults_report(self):
        result = route_event(_event(route_type="bogus"), "feishu")
        assert result.route_type == "report"

    def test_extra_redacted_in_meta(self):
        event = _event(extra={"token": "secret-val", "name": "visible"})
        result = route_event(event, "feishu")
        meta = result.payloads[0].meta
        assert meta["token"] == "[REDACTED]"
        assert meta["name"] == "visible"

    def test_extra_does_not_mutate_event(self):
        event = _event(extra={"api_key": "original"})
        _ = route_event(event, "feishu")
        assert event.extra["api_key"] == "original"

    def test_sensitive_text_in_message(self):
        event = _event(message="leaked token=sk-real-secret-here")
        result = route_event(event, "ntfy")
        msg = result.payloads[0].payload["message"]
        assert "sk-real-secret-here" not in msg

    def test_fail_soft_unknown_does_not_block_valid(self):
        result = route_event(_event(), "feishu, totally_invalid, ntfy")
        assert set(result.channels) == {"feishu", "ntfy"}
        assert "totally_invalid" in result.skipped_unknown


# ── 确定性 ────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_same_output(self):
        event = _event(title="确定性", message="**测试** 确定性")
        r1 = route_event(event, "feishu,ntfy,slack")
        r2 = route_event(event, "feishu,ntfy,slack")
        assert r1.payloads == r2.payloads
        assert r1.errors == r2.errors
        assert r1.skipped_unknown == r2.skipped_unknown

    def test_same_formatter_same_payload(self):
        event = _event()
        for _ in range(5):
            assert format_feishu(event) == format_feishu(event)
            assert format_email(event) == format_email(event)
            assert format_ntfy(event) == format_ntfy(event)
            assert format_slack(event) == format_slack(event)
            assert format_pushover(event) == format_pushover(event)
            assert format_gotify(event) == format_gotify(event)

    def test_payloads_json_serializable(self):
        event = _event(extra={"tags": ["a", "b"], "count": 42})
        result = route_event(event, SUPPORTED_CHANNELS)
        for cp in result.payloads:
            json.dumps(cp.payload)
            json.dumps(cp.meta)


# ── JSON 可序列化 ─────────────────────────────────────────


class TestJsonSerializable:
    @pytest.mark.parametrize("channel", list(SUPPORTED_CHANNELS))
    def test_each_channel_json_serializable(self, channel):
        event = _event(
            title="JSON 测试",
            message="# 标题\n\n正文 **加粗** `code`",
            extra={"symbol": "600519", "token": "should_redact"},
        )
        result = route_event(event, [channel])
        assert len(result.payloads) == 1
        cp = result.payloads[0]
        s = json.dumps(cp.payload, ensure_ascii=False)
        assert "should_redact" not in s
        s_meta = json.dumps(cp.meta, ensure_ascii=False)
        assert "should_redact" not in s_meta
        assert "[REDACTED]" in s_meta


# ── CHANNEL_LIMITS 常量一致性 ─────────────────────────────


class TestChannelLimits:
    def test_all_channels_have_limits(self):
        for ch in SUPPORTED_CHANNELS:
            assert ch in CHANNEL_LIMITS

    def test_limits_are_positive(self):
        for ch, limits in CHANNEL_LIMITS.items():
            for field_name, limit in limits.items():
                assert limit > 0, f"{ch}.{field_name} limit must be positive"
