import pytest

from app.services import nl_screener


def test_prompt_exposes_units_labels_and_change_pct_scaling():
    prompt = nl_screener._prompt("涨幅大于5%")[-1]["content"]
    assert '"label": "涨跌幅"' in prompt
    assert '"unit": "小数"' in prompt
    assert "5%" in prompt and "0.05" in prompt


@pytest.mark.asyncio
async def test_nl_parser_validates_candidates_without_running_query(monkeypatch):
    profiles = []

    async def fake_generate(messages, **kwargs):
        profiles.append(kwargs.get("profile_id"))
        return '[{"field":"change_pct","op":">","value":0.05,"raw":"涨幅大于5%"}]'

    monkeypatch.setattr(nl_screener, "generate_ai_text", fake_generate)
    result = await nl_screener.parse_nl("涨幅大于5%", profile_id="profile-a")
    assert result == {
        "recognized": [{"field": "change_pct", "op": ">", "value": 0.05}],
        "unrecognized": [],
    }
    assert profiles == ["profile-a"]


@pytest.mark.asyncio
async def test_nl_parser_retries_malformed_json_once(monkeypatch):
    responses = iter(["not json", '[{"field":"close","op":">","value":10}]'])

    async def fake_generate(messages, **kwargs):
        return next(responses)

    monkeypatch.setattr(nl_screener, "generate_ai_text", fake_generate)
    result = await nl_screener.parse_nl("收盘价大于10")
    assert result["recognized"] == [{"field": "close", "op": ">", "value": 10}]


@pytest.mark.asyncio
async def test_nl_parser_keeps_unresolved_candidates_sanitized(monkeypatch):
    async def fake_generate(messages, **kwargs):
        return '[{"field":"unknown","op":"=","value":1,"raw":"' + "x" * 500 + '"}]'

    monkeypatch.setattr(nl_screener, "generate_ai_text", fake_generate)
    result = await nl_screener.parse_nl("未知条件")
    assert result["recognized"] == []
    assert result["unrecognized"][0]["reason"] == "unknown_field"
    assert len(result["unrecognized"][0]["raw"]) == 200


@pytest.mark.asyncio
async def test_nl_parser_provider_failure_and_double_malformed_are_safe(monkeypatch):
    async def fail_generate(messages, **kwargs):
        raise RuntimeError("secret provider detail")

    monkeypatch.setattr(nl_screener, "generate_ai_text", fail_generate)
    with pytest.raises(nl_screener.NLScreenerError) as exc:
        await nl_screener.parse_nl("涨幅大于5%")
    assert str(exc.value) == "provider_unavailable"

    async def malformed_generate(messages, **kwargs):
        return "still not json"

    monkeypatch.setattr(nl_screener, "generate_ai_text", malformed_generate)
    result = await nl_screener.parse_nl("无法解析")
    assert result == {"recognized": [], "unrecognized": [{"raw": "无法解析", "reason": "malformed"}]}
