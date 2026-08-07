from types import SimpleNamespace

import pytest

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



# ── M17: 结构化分析报告卡片 builder ───────────────────────
import json

from app.services.webhook_adapter import (
    _CARD_ITEM_MAX,
    _REPORT_FIELDS,
    build_analysis_card_payload,
    send_feishu_analysis_card,
)

_FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"


def _full_report():
    return {
        "title": "个股深度分析",
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "data_as_of": "2026-08-05",
        "risk": "中",
        "gate": "通过",
        "evidence": ["放量突破 60 日均线", "北向连续 3 日净买入"],
        "invalidation": "跌破 1700 且周线 MACD 死叉",
        "warnings": ["短期 RSI 超买"],
        "panel_url": "https://tickflow.test/#/analysis/rpt_001",
        "attempt_id": "att_abc123",
        "request_id": "req_def456",
    }


def _contents(payload):
    """把 payload 里所有 lark_md / plain_text content 扁平化为一个字符串, 便于断言。"""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") in ("lark_md", "plain_text") and "content" in node:
                texts.append(node["content"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return texts


# ── 完整输入 ──────────────────────────────────────────────
def test_full_report_builds_all_sections():
    p = build_analysis_card_payload(_full_report())
    assert p["msg_type"] == "interactive"
    assert p["card"]["header"]["title"]["content"] == "个股深度分析"
    assert p["card"]["header"]["template"] == "orange"  # risk=中 → orange
    joined = " \n ".join(_contents(p))
    assert "600519.SH" in joined
    assert "贵州茅台" in joined
    assert "数据截止 2026-08-05" in joined
    assert "风险" in joined and "中" in joined
    assert "Gate" in joined and "通过" in joined
    assert "放量突破 60 日均线" in joined
    assert "北向连续 3 日净买入" in joined
    assert "失效条件" in joined and "跌破 1700" in joined
    assert "⚠ 注意" in joined and "短期 RSI 超买" in joined
    assert "rpt_001" in joined and "查看详情" in joined
    assert "attempt: att_abc123" in joined
    assert "request: req_def456" in joined


def test_full_report_elements_well_formed():
    p = build_analysis_card_payload(_full_report())
    els = p["card"]["elements"]
    tags = [e.get("tag") for e in els]
    # 至少有 div / hr / note 结构
    assert "div" in tags
    assert "hr" in tags
    assert tags[-1] == "note"
    # note 里是 attempt + request
    assert "att_abc123" in els[-1]["elements"][0]["content"]


# ── 最小输入 ──────────────────────────────────────────────
def test_minimal_title_only():
    p = build_analysis_card_payload({"title": "提示"})
    assert p["card"]["header"]["title"]["content"] == "提示"
    assert p["card"]["header"]["template"] == "blue"
    # 没有 symbol/data_as_of 等时, elements 为空 (header 本身承载标题)
    assert p["card"]["elements"] == []


def test_minimal_symbol_only_falls_back_to_header():
    p = build_analysis_card_payload({"symbol": "000001.SZ"})
    assert p["card"]["header"]["title"]["content"] == "000001.SZ"
    # symbol 会在副标题行出现
    assert any("000001.SZ" in c for c in _contents(p))


def test_empty_report_returns_empty():
    assert build_analysis_card_payload({}) == {}
    assert build_analysis_card_payload({"name": "x", "risk": "高"}) == {}
    assert build_analysis_card_payload("not a dict") == {}  # type: ignore[arg-type]


# ── 字段缺失干净省略 ───────────────────────────────────────
def test_optional_fields_omitted_when_absent():
    p = build_analysis_card_payload({"title": "T", "symbol": "S"})
    joined = " \n ".join(_contents(p))
    assert "关键证据" not in joined
    assert "失效条件" not in joined
    assert "⚠ 注意" not in joined
    assert "查看详情" not in joined
    assert "attempt" not in joined
    assert "request" not in joined
    assert "风险" not in joined


def test_attempt_without_request():
    p = build_analysis_card_payload({"title": "T", "attempt_id": "att_only"})
    joined = " \n ".join(_contents(p))
    assert "attempt: att_only" in joined
    assert "request" not in joined


# ── 敏感字段拒绝 ───────────────────────────────────────────
def test_sensitive_fields_ignored(caplog):
    import logging

    report = _full_report()
    report["account"] = "ACC-SECRET-8888"
    report["positions"] = [{"symbol": "600519.SH", "qty": 10000, "cost": 1680.5}]
    report["trades"] = [{"side": "buy", "price": 1701, "qty": 500}]
    report["持仓数量"] = 10000

    p = build_analysis_card_payload(report)
    blob = json.dumps(p, ensure_ascii=False)
    assert "ACC-SECRET-8888" not in blob
    assert "10000" not in blob
    assert "1680.5" not in blob
    assert "持仓数量" not in blob
    # 仍有正常字段
    assert "600519.SH" in blob

    # 敏感字段出现时应有 warning 日志
    with caplog.at_level(logging.WARNING):
        build_analysis_card_payload(report)
    assert any("敏感字段" in r.message for r in caplog.records)


def test_report_fields_allowlist_excludes_sensitive():
    # allowlist 与黑名单不相交 (设计不变量)
    assert _REPORT_FIELDS.isdisjoint(
        {"account", "positions", "trades", "持仓数量", "账户", "流水"}
    )


# ── 长度限制 ───────────────────────────────────────────────
def test_evidence_and_warnings_truncated_and_capped():
    report = {
        "title": "T",
        "evidence": [f"证据条目{i}" for i in range(20)],
        "warnings": ["x" * 500],
    }
    p = build_analysis_card_payload(report)
    els = p["card"]["elements"]
    ev_div = next(e for e in els if "关键证据" in e.get("text", {}).get("content", ""))
    ev_content = ev_div["text"]["content"]
    # 最多 8 条
    assert ev_content.count("证据条目") == 8
    # warning 单条被截断到 200 字符
    warn_div = next(e for e in els if "⚠ 注意" in e.get("text", {}).get("content", ""))
    warn_item = warn_div["text"]["content"].split("\n")[1]
    assert len(warn_item) <= _CARD_ITEM_MAX + len("- …")


def test_long_title_truncated():
    p = build_analysis_card_payload({"title": "A" * 500})
    assert len(p["card"]["header"]["title"]["content"]) <= 101  # 100 + …


def test_long_invalidation_truncated():
    p = build_analysis_card_payload({"title": "T", "invalidation": "B" * 1000})
    inv = next(e for e in p["card"]["elements"] if "失效条件" in e["text"]["content"])
    content = inv["text"]["content"]
    # "失效条件: " 前缀 + 截断后文本
    assert len(content) <= len("**失效条件**: ") + 400 + len("…")


# ── evidence / warnings 类型归一 ───────────────────────────
def test_evidence_string_coerced_to_list():
    p = build_analysis_card_payload({"title": "T", "evidence": "单条字符串证据"})
    assert "单条字符串证据" in " \n ".join(_contents(p))


def test_evidence_filters_blanks():
    p = build_analysis_card_payload({"title": "T", "evidence": ["有效", "", "  ", None]})
    ev_div = next(e for e in p["card"]["elements"] if "关键证据" in e["text"]["content"])
    assert ev_div["text"]["content"].count("- ") == 1


# ── 风险配色 ───────────────────────────────────────────────
@pytest.mark.parametrize("risk,expected", [
    ("高", "red"), ("high", "red"),
    ("中", "orange"), ("medium", "orange"),
    ("低", "green"), ("low", "green"),
    ("未知", "blue"), ("", "blue"),
])
def test_risk_template_mapping(risk, expected):
    p = build_analysis_card_payload({"title": "T", "risk": risk})
    assert p["card"]["header"]["template"] == expected


# ── send_feishu_analysis_card 发送层 ──────────────────────
def test_send_invalid_url_returns_false():
    assert send_feishu_analysis_card("https://evil.test/hook", _full_report()) is False


def test_send_empty_report_returns_false():
    assert send_feishu_analysis_card(_FEISHU_URL, {}) is False


def test_send_success_monkeypatch(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0})
    monkeypatch.setattr("httpx.post", fake_post)
    assert send_feishu_analysis_card(_FEISHU_URL, _full_report()) is True
    assert calls[0][0] == _FEISHU_URL
    assert calls[0][1]["trust_env"] is False
    assert calls[0][1]["json"]["msg_type"] == "interactive"


def test_send_with_secret_adds_signature(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0})
    monkeypatch.setattr("httpx.post", fake_post)
    send_feishu_analysis_card(_FEISHU_URL, {"title": "T"}, secret="my-secret")
    body = calls[0]["json"]
    assert "timestamp" in body
    assert "sign" in body


def test_send_exception_returns_false(monkeypatch):
    def boom(url, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr("httpx.post", boom)
    # 不抛异常, 返回 False
    assert send_feishu_analysis_card(_FEISHU_URL, {"title": "T"}) is False


def test_send_feishu_card_still_works(monkeypatch):
    """additive: 新增 send 函数不影响既有 send_feishu_card。"""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0})

    monkeypatch.setattr("httpx.post", fake_post)
    assert webhook_adapter.send_feishu_card(_FEISHU_URL, "标题", "副标题", "# 正文") is True
    assert calls[0]["json"]["msg_type"] == "interactive"
    assert calls[0]["json"]["card"]["header"]["title"]["content"] == "标题"