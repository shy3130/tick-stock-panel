"""P3 结构化入口 ai_meta / fallback / 预算 clamp 集成测试。

覆盖:
- NL 解析响应携带 ai_meta 且保留旧 recognized/unrecognized 字段;
- 交易归因 run_autopsy 接受 profile_id 且结果携带 ai_meta;
- 默认 profile 参与 fallback 链 (profile_id=None 时 default 不被排除);
- 流式 provider 不伪造 usage;
- profile_configured 检查实际 profile 而非默认。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.ai_structured import AIUsage, GenerateResponse
from app.services import nl_screener


def _meta_generate(profile_id_used: str | None = "profile-a", *, usage: AIUsage | None = None):
    """构造返回 GenerateResponse 的 generate, 模拟 metadata 路径。"""

    async def _generate(messages, **kwargs):
        return GenerateResponse(
            text='[{"field":"change_pct","op":">","value":0.05,"raw":"涨幅大于5%"}]',
            usage=usage or AIUsage(prompt_tokens=12, cached_prompt_tokens=4, completion_tokens=2, total_tokens=14),
            provider="openai_compat",
            profile_id=kwargs.get("profile_id", profile_id_used),
            model="gpt-test",
            primary_profile_id=kwargs.get("profile_id"),
            fallback_used=False,
            fallback_reason=None,
        )

    return _generate


@pytest.mark.asyncio
async def test_nl_parse_response_carries_ai_meta_and_legacy_fields():
    result = await nl_screener.parse_nl(
        "涨幅大于5%",
        profile_id="profile-a",
        generate=_meta_generate(),
    )
    # 旧字段保留
    assert result["recognized"] == [{"field": "change_pct", "op": ">", "value": 0.05}]
    assert result["unrecognized"] == []
    # 新增 ai_meta 精确反映实际 profile
    meta = result["ai_meta"]
    assert meta["profile_id"] == "profile-a"
    assert meta["primary_profile_id"] == "profile-a"
    assert meta["fallback_used"] is False
    assert meta["usage"]["cached_prompt_tokens"] == 4
    assert meta["usage"]["total_tokens"] == 14


@pytest.mark.asyncio
async def test_nl_parse_budget_clamps_max_tokens():
    captured: dict[str, Any] = {}

    async def _gen(messages, **kwargs):
        captured.update(kwargs)
        return GenerateResponse(
            text='[{"field":"close","op":">","value":10}]',
            profile_id=kwargs.get("profile_id"),
            primary_profile_id=kwargs.get("profile_id"),
            usage=AIUsage(),
        )

    await nl_screener.parse_nl("收盘价大于10", generate=_gen)
    # 预算被中央上限 clamp (不得放大超过 2000)
    assert captured["max_tokens"] == 2000
    assert captured["timeout"] == 60.0


@pytest.mark.asyncio
async def test_nl_parse_meta_usage_recorded_in_snapshot():
    from app.services.ai_usage_snapshot import get_usage_registry

    reg = get_usage_registry()
    reg.reset()
    await nl_screener.parse_nl(
        "涨幅大于5%",
        profile_id="profile-a",
        generate=_meta_generate(usage=AIUsage(prompt_tokens=20, completion_tokens=3, total_tokens=23)),
    )
    snap = reg.snapshot()
    assert snap["by_purpose"]["nl_screener"]["total_tokens"] == 23
    assert snap["by_purpose"]["nl_screener"]["calls"] == 1


# ── 交易归因 run_autopsy ──────────────────────────────────
def _autopsy_meta_generate():
    async def _generate(messages, **kwargs):
        return GenerateResponse(
            text='{"tradeId":"T1","classification":"A","reasoning":"正常","fix":"无需修复","patternIds":[]}',
            provider="openai_compat",
            profile_id=kwargs.get("profile_id", "profile-a"),
            model="gpt-test",
            primary_profile_id=kwargs.get("profile_id"),
            usage=AIUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
        )

    return _generate


@pytest.mark.asyncio
async def test_run_autopsy_accepts_profile_id_and_returns_ai_meta(monkeypatch, tmp_path: Path):
    from app.services.trading import autopsy

    monkeypatch.setattr(autopsy, "profile_configured", lambda pid=None: True)
    store = autopsy.store
    store.write_trade(tmp_path, {"tradeId": "T1", "symbol": "600519.SH"})
    # events / audit 为空 (read_events/read_audit 对缺失文件返回 [])

    result = await autopsy.run_autopsy(
        tmp_path, "T1", profile_id="profile-a", generate=_autopsy_meta_generate()
    )
    # 旧字段保留
    assert result["classification"] == "A"
    assert result["schemaVersion"] == 1
    assert "usage" in result and "provider" in result
    # 新增 ai_meta
    meta = result["ai_meta"]
    assert meta["profile_id"] == "profile-a"
    assert meta["usage"]["total_tokens"] == 60


# ── 默认 profile 参与 fallback 链 ─────────────────────────
def test_default_profile_included_when_profile_id_none(monkeypatch):
    """profile_id=None 时默认 profile 必须进入 fallback 链 (不被排除)。"""
    from app.services import ai_routing
    from app.services.ai_routing import RoutePolicy

    avail = {"profile-default", "profile-b"}
    policy = RoutePolicy(allow_profile_fallback=True, fallback_profile_ids=["profile-b"])
    monkeypatch.setattr(
        "app.services.ai_profiles.get_default_profile_id", lambda: "profile-default"
    )
    resolved_primary = "profile-default"  # 复刻 with_meta 的解析
    chain = ai_routing.build_fallback_chain(resolved_primary, policy, avail)
    assert "profile-default" in chain
    assert chain[0] == "profile-default"


# ── 流式不伪造 usage ──────────────────────────────────────
@pytest.mark.asyncio
async def test_stock_analysis_stream_does_not_fabricate_usage(monkeypatch, tmp_path):
    from datetime import date, timedelta

    import polars as pl

    from app.services import stock_analyzer

    async def _fake_stream(messages, **kwargs):
        yield "分析片段"

    days = [date.today() - timedelta(days=89 - i) for i in range(90)]
    frame = pl.DataFrame({
        "date": days,
        "open": [10.0] * 90,
        "high": [11.0] * 90,
        "low": [9.0] * 90,
        "close": [10.5] * 90,
        "volume": [1000] * 90,
        "ma20": [10.0] * 90,
        "atr_14": [0.5] * 90,
    })
    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda repo, symbol: frame)
    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_stream)

    chunks: list[dict] = []
    async for raw in stock_analyzer.analyze_stock_stream(object(), tmp_path, "600519.SH"):
        chunks.append(json.loads(raw))

    usage_chunks = [chunk for chunk in chunks if chunk.get("type") == "usage"]
    assert usage_chunks
    assert usage_chunks[0]["usage"] is None
    assert any("未伪造" in warning for warning in usage_chunks[0]["warnings"])


# ── 健康快照无敏感信息 ────────────────────────────────────
def test_health_snapshot_has_no_secrets():
    from app.services.ai_routing import get_health_registry

    reg = get_health_registry()
    reg.record_failure("profile-x", "quota", 100.0)
    snap = reg.get_health("profile-x")
    dumped = str(snap)
    assert "api_key" not in dumped
    assert set(snap.keys()) == {
        "profile_id",
        "consecutive_failures",
        "last_error_category",
        "in_cooldown",
        "cooldown_remaining_s",
        "latency_ewma_ms",
    }


# ── profile_configured 检查实际 profile ──────────────────
def test_profile_configured_checks_specific_profile(monkeypatch):
    """选了 profile 但默认未配时, profile_configured 应按实际 profile 判断。"""
    from app.services import ai_provider, ai_profiles

    fake = {"id": "p1", "provider": "openai_compat", "api_key": "sk-real", "model": "gpt"}
    monkeypatch.setattr(ai_profiles, "resolve_profile", lambda pid: fake if pid == "p1" else None)
    assert ai_provider.profile_configured("p1") is True
    assert ai_provider.profile_configured("missing") is False
