"""策略风险声明 profile 读写 + 结构校验测试 (tmp_path 隔离)。"""
from __future__ import annotations
import json

from app.services import strategy_profile as sp


def _valid(strategy_id: str = "trend_a") -> dict:
    return {
        "schemaVersion": 1,
        "strategyId": strategy_id,
        "invalidation": [
            {"name": "跌破年线", "observable": "收盘价跌破 MA250", "action": "全部清仓"}
        ],
        "risk": {"positionLimitPct": 20, "lossBudgetPct": 5, "thesisHorizonMonths": 6},
        "cadence": {"review": "monthly"},
    }


# ── validate_profile ────────────────────────────────────
def test_validate_profile_passes_valid():
    assert sp.validate_profile(_valid()) == []


def test_invalidation_missing_action():
    p = _valid()
    p["invalidation"][0] = {"name": "跌破年线", "observable": "收盘价跌破 MA250"}  # 缺 action
    problems = sp.validate_profile(p)
    assert any("action" in m for m in problems)


def test_invalidation_missing_observable():
    p = _valid()
    p["invalidation"][0] = {"name": "跌破年线", "action": "清仓"}  # 缺 observable
    assert any("observable" in m for m in sp.validate_profile(p))


def test_invalidation_missing_name():
    p = _valid()
    p["invalidation"][0] = {"observable": "o", "action": "a"}  # 缺 name
    assert any("name" in m for m in sp.validate_profile(p))


def test_invalidation_blank_string_counts_as_missing():
    p = _valid()
    p["invalidation"][0]["action"] = "   "  # 空白字符串
    assert any("action" in m for m in sp.validate_profile(p))


def test_invalidation_empty_array():
    p = _valid()
    p["invalidation"] = []
    assert sp.validate_profile(p)


def test_invalidation_missing_key():
    p = _valid()
    del p["invalidation"]
    assert sp.validate_profile(p)


def test_position_limit_pct_out_of_range():
    for bad in (0, -5, 100.1, 150):
        p = _valid()
        p["risk"]["positionLimitPct"] = bad
        assert any("positionLimitPct" in m for m in sp.validate_profile(p)), bad


def test_position_limit_pct_boundary_ok():
    p = _valid()
    p["risk"]["positionLimitPct"] = 100  # 上界闭区间
    assert sp.validate_profile(p) == []


def test_loss_budget_pct_out_of_range():
    for bad in (0, -3, 101):
        p = _valid()
        p["risk"]["lossBudgetPct"] = bad
        assert any("lossBudgetPct" in m for m in sp.validate_profile(p)), bad


def test_horizon_must_be_positive_int():
    for bad in (0, -1, 2.5, True):  # 0 / 负 / 小数 / bool 均非法
        p = _valid()
        p["risk"]["thesisHorizonMonths"] = bad
        assert any("thesisHorizonMonths" in m for m in sp.validate_profile(p)), bad


def test_risk_missing():
    p = _valid()
    del p["risk"]
    assert any("risk" in m for m in sp.validate_profile(p))


# ── read / write / delete ───────────────────────────────
def test_write_read_delete_roundtrip(tmp_path):
    p = _valid("trend_a")
    sp.write_profile(tmp_path, p)
    # 文件名带 .profile 后缀, 与 overrides 同目录
    assert (tmp_path / "user_data" / "strategy_overrides" / "trend_a.profile.json").exists()
    got = sp.read_profile(tmp_path, "trend_a")
    assert got is not None
    assert got["strategyId"] == "trend_a"
    assert got["invalidation"][0]["action"] == "全部清仓"
    assert sp.delete_profile(tmp_path, "trend_a") is True
    assert sp.read_profile(tmp_path, "trend_a") is None
    assert sp.delete_profile(tmp_path, "trend_a") is False  # 已删


def test_read_missing_returns_none(tmp_path):
    assert sp.read_profile(tmp_path, "nope") is None


def test_path_traversal_sanitized(tmp_path):
    # strategy_id 含路径分隔符不应逃逸出 strategy_overrides 目录
    sp.write_profile(tmp_path, {**_valid("../evil"), "strategyId": "../evil"})
    # 写到了 ../evil.profile.json (已 sanitize), 不在上级目录
    assert not (tmp_path / "..evil").exists()
    assert (tmp_path / "user_data" / "strategy_overrides" / "__evil.profile.json").exists()



# ── P6.3 family / familyMix / playbook ──────────────────
def test_family_each_valid_value_passes():
    for fam in ("value", "growth", "trend", "event", "short_horizon", "relative_value"):
        p = _valid()
        p["family"] = fam
        assert sp.validate_profile(p) == [], fam


def test_family_invalid_value_problem():
    p = _valid()
    p["family"] = "banana"
    problems = sp.validate_profile(p)
    assert any("family" in m for m in problems)


def test_family_mixed_requires_family_mix():
    p = _valid()
    p["family"] = "mixed"  # 缺 familyMix
    problems = sp.validate_profile(p)
    assert any("familyMix" in m for m in problems)


def test_family_mixed_missing_one_key_problem():
    p = _valid()
    p["family"] = "mixed"
    p["familyMix"] = {  # 缺 conflictResolution
        "entryJudge": "趋势", "invalidationAuthority": "破位",
        "sizingHorizon": "ATR",
    }
    problems = sp.validate_profile(p)
    assert any("conflictResolution" in m for m in problems)


def test_family_mixed_blank_value_problem():
    p = _valid()
    p["family"] = "mixed"
    p["familyMix"] = {
        "entryJudge": "趋势", "invalidationAuthority": "破位",
        "sizingHorizon": "   ", "conflictResolution": "趋势优先",
    }
    assert any("sizingHorizon" in m for m in sp.validate_profile(p))


def test_family_mixed_complete_passes():
    p = _valid()
    p["family"] = "mixed"
    p["familyMix"] = {
        "entryJudge": "趋势", "invalidationAuthority": "破位",
        "sizingHorizon": "ATR", "conflictResolution": "趋势优先",
    }
    assert sp.validate_profile(p) == []


def test_playbook_valid_strings_pass():
    p = _valid()
    p["playbook"] = {"scope": "A股", "entry": "突破", "exit": "破位"}
    assert sp.validate_profile(p) == []


def test_playbook_partial_keys_pass():
    # playbook 字段均可缺省; 只给 scope 也合法
    p = _valid()
    p["playbook"] = {"scope": "A股趋势"}
    assert sp.validate_profile(p) == []


def test_playbook_non_string_value_problem():
    p = _valid()
    p["playbook"] = {"scope": 123}  # 非字符串
    assert any("playbook.scope" in m for m in sp.validate_profile(p))


def test_playbook_not_dict_problem():
    p = _valid()
    p["playbook"] = ["scope", "entry"]
    assert any("playbook" in m for m in sp.validate_profile(p))


def test_old_profile_without_new_fields_passes():
    # 无 family / familyMix / playbook 的旧 profile 必须通过 (向后兼容)
    assert sp.validate_profile(_valid()) == []


# ── AI 深度体检降级路径 (P6.2; 直接测 helper, 不依赖 FastAPI) ──
def test_ai_deep_review_not_configured(monkeypatch):
    import app.api.strategy_profile as api
    import asyncio

    monkeypatch.setattr(api, "ai_configured", lambda *a, **k: False)
    report, error = asyncio.run(api._ai_deep_review("s1", None, None))
    assert report is None
    assert error is not None  # "AI 未配置"


def test_ai_deep_review_degrades_when_generate_raises(monkeypatch):
    import app.api.strategy_profile as api
    import asyncio

    monkeypatch.setattr(api, "ai_configured", lambda *a, **k: True)

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "generate_ai_text", _boom)
    report, error = asyncio.run(api._ai_deep_review("s1", _valid(), {"summary": {}}))
    assert report is None
    assert "boom" in error


def test_ai_deep_review_success(monkeypatch):
    import app.api.strategy_profile as api
    import asyncio

    monkeypatch.setattr(api, "ai_configured", lambda *a, **k: True)

    async def _ok(*args, **kwargs):
        return json.dumps(
            {
                "items": [
                    {
                        "index": index,
                        "name": f"不变量{index}",
                        "conclusion": "满足",
                        "reason": "结构完整",
                    }
                    for index in range(1, 8)
                ],
                "falsifiability": "可证伪",
                "overall": "结构完整",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(api, "generate_ai_text", _ok)
    report, error = asyncio.run(api._ai_deep_review("s1", _valid(), None))
    assert report is not None
    assert "1. 不变量1：满足" in report
    assert "整体可证伪性：可证伪" in report
    assert error is None