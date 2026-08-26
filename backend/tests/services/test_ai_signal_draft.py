"""行为测试：ai_signal_draft 服务 + 纯 helper。

覆盖 contract 验收：
- 合法返回带 ai_meta
- 未知 field/op/right、超8、非法 id 拒绝（通过 validate 强不变量）
- provider 不可用映射
- 冲突 id 安全后缀（仅返回）
- 注入 generate deterministic
- 服务/API 路径无写盘、无执行
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services.ai_signal_draft import (
    CustomSignalDraftError,
    _ensure_unique_draft_id,
    generate_custom_signal_draft,
)
from app.strategy import custom_signals


def _ok_generate(messages, **kwargs):
    # 模拟合法结构化返回
    class R:
        text = '{"id":"my_sig","name":"测试","kind":"entry","conditions":[{"left":"close","op":">","right":10}],"rationale":"ok"}'
        provider = "test"
        model = "test"
        profile_id = "p1"
        primary_profile_id = "p1"
        fallback_used = False
        fallback_reason = None
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cached_prompt_tokens": 0,
        }

    return R()


def _bad_field_generate(messages, **kwargs):
    class R:
        text = '{"id":"bad","name":"bad","kind":"entry","conditions":[{"left":"no_such_field","op":">","right":1}]}'

    return R()


def _bad_op_generate(messages, **kwargs):
    class R:
        text = '{"id":"b","name":"b","kind":"exit","conditions":[{"left":"close","op":"foo","right":1}]}'

    return R()


def _bad_right_generate(messages, **kwargs):
    class R:
        text = '{"id":"b","name":"b","kind":"both","conditions":[{"left":"close","op":">","right":"field:nope"}]}'

    return R()


def _non_finite_right_generate(messages, **kwargs):
    class R:
        text = '{"id":"nf","name":"nf","kind":"entry","conditions":[{"left":"close","op":">","right":NaN}]}'

    return R()


def _excess_generate(messages, **kwargs):
    cond = {"left": "close", "op": ">", "right": 1}
    payload = {
        "id": "e",
        "name": "e",
        "kind": "entry",
        "conditions": [cond] * 9,
    }

    class R:
        text = json.dumps(payload)

    return R()


def _provider_fail_generate(messages, **kwargs):
    raise RuntimeError("quota exceeded")


def _bad_id_generate(messages, **kwargs):
    class R:
        text = '{"id":"Bad-ID!","name":"bad","kind":"entry","conditions":[{"left":"close","op":">","right":1}]}'

    return R()


@pytest.mark.asyncio
async def test_valid_draft_returns_with_ai_meta_and_validated():
    res = await generate_custom_signal_draft(
        "收盘价大于10", profile_id="p-test", generate=_ok_generate
    )
    assert "draft" in res
    assert "ai_meta" in res
    d = res["draft"]
    assert d["id"] == "my_sig"
    assert d["kind"] == "entry"
    assert len(d["conditions"]) == 1
    assert res["rationale"] == "ok"
    # ai_meta shape
    assert res["ai_meta"]["profile_id"] == "p1"


@pytest.mark.asyncio
async def test_reject_unknown_field_via_validate_invariant():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_bad_field_generate)
    assert "invalid_structure" in str(exc.value) or "不在白名单" in str(exc.value)


@pytest.mark.asyncio
async def test_reject_bad_op_via_validate():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_bad_op_generate)
    assert "invalid_structure" in str(exc.value) or "运算符" in str(exc.value)


@pytest.mark.asyncio
async def test_reject_bad_right_via_validate():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_bad_right_generate)
    assert (
        "invalid_structure" in str(exc.value)
        or "右值" in str(exc.value)
        or "白名单" in str(exc.value)
    )


@pytest.mark.asyncio
async def test_reject_non_finite_right_value():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_non_finite_right_generate)
    assert "invalid_structure" in str(exc.value) or "有限" in str(exc.value)


@pytest.mark.asyncio
async def test_reject_excess_conditions():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_excess_generate)
    assert "invalid_structure" in str(exc.value) or "最多 8" in str(exc.value)


@pytest.mark.asyncio
async def test_reject_illegal_id_via_validate():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_bad_id_generate)
    assert "invalid_structure" in str(exc.value) or "id" in str(exc.value)


def test_pure_suffix_helper_for_conflict_id():
    existing = {"my_sig", "foo"}
    draft = {
        "id": "my_sig",
        "name": "x",
        "kind": "entry",
        "conditions": [{"left": "close", "op": ">", "right": 1}],
    }
    out = _ensure_unique_draft_id(draft, existing)
    assert out["id"] != "my_sig"
    assert out["id"].startswith("my_sig_")
    assert out["id"] not in existing
    # 再次调用幂等
    out2 = _ensure_unique_draft_id(out, existing | {out["id"]})
    assert out2["id"] != out["id"]


@pytest.mark.asyncio
async def test_injection_generate_used_for_deterministic():
    called = {}

    async def fake_gen(messages, **kw):
        called["yes"] = True

        class R:
            text = '{"id":"inj","name":"inj","kind":"both","conditions":[{"left":"volume","op":">","right":100}]}'

        return R()

    res = await generate_custom_signal_draft("vol high", generate=fake_gen)
    assert called.get("yes")
    assert res["draft"]["id"] == "inj"


@pytest.mark.asyncio
async def test_provider_unavailable_raises_for_503_mapping():
    with pytest.raises(CustomSignalDraftError) as exc:
        await generate_custom_signal_draft("xx", generate=_provider_fail_generate)
    assert "provider_unavailable" in str(exc.value) or "quota" in str(exc.value).lower()


def test_service_does_not_touch_fs_or_execute(monkeypatch, tmp_path):
    # 确保服务层无写盘调用；validate 是只读检查
    orig_save = custom_signals.save_one
    calls = []

    def spy_save(*a, **k):
        calls.append("save")
        return orig_save(*a, **k)

    monkeypatch.setattr(custom_signals, "save_one", spy_save)
    # 运行一个合法（mock）
    asyncio.run(generate_custom_signal_draft("close>ma", generate=_ok_generate))
    assert not calls, "service must not call save"
    # 目录检查由 api 层保证（load only），这里只验证服务纯


def test_helper_id_suffix_safety():
    # 非法 id 也后缀
    d = {
        "id": "Bad-ID!",
        "name": "x",
        "kind": "entry",
        "conditions": [{"left": "close", "op": ">", "right": 0}],
    }
    out = _ensure_unique_draft_id(d, set())
    assert custom_signals.ID_RE.match(out["id"])
    assert out["id"].startswith("ai_sig_") or "ai_sig" in out["id"]
