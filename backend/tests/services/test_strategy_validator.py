"""策略结构诊断 validator 测试 (纯函数, 无 IO)。"""
from __future__ import annotations

from app.services.strategy_validator import validate_strategy


def _profile(horizon: int = 6, review: str = "monthly") -> dict:
    return {
        "schemaVersion": 1,
        "strategyId": "s1",
        "invalidation": [{"name": "n", "observable": "o", "action": "a"}],
        "risk": {
            "positionLimitPct": 20,
            "lossBudgetPct": 5,
            "thesisHorizonMonths": horizon,
        },
        "cadence": {"review": review},
    }


def _by_id(res: dict) -> dict:
    return {c["id"]: c for c in res["checks"]}


def test_all_pass_when_ledger_empty():
    res = validate_strategy("s1", _profile(horizon=6, review="monthly"), ledger=None)
    by = _by_id(res)
    assert by["field_completeness"]["status"] == "pass"
    assert by["cadence_horizon_match"]["status"] == "pass"
    # ledger 为空 → 无持仓证据
    assert by["horizon_drift"]["status"] == "insufficient_evidence"
    assert len(res["checks"]) == 7


def test_no_profile_fails_completeness():
    res = validate_strategy("s1", None, None)
    by = _by_id(res)
    assert by["field_completeness"]["status"] == "fail"
    assert by["horizon_drift"]["status"] == "insufficient_evidence"


def test_field_completeness_fails_on_invalid_profile():
    p = _profile()
    p["risk"]["thesisHorizonMonths"] = 0  # 非法
    res = validate_strategy("s1", p, None)
    assert _by_id(res)["field_completeness"]["status"] == "fail"


def test_cadence_horizon_mismatch_weekly_long_horizon():
    # 周频复盘 + >12 月论点 → partial
    res = validate_strategy("s1", _profile(horizon=18, review="weekly"), None)
    assert _by_id(res)["cadence_horizon_match"]["status"] == "partial"


def test_cadence_horizon_match_ok_monthly_short():
    res = validate_strategy("s1", _profile(horizon=6, review="monthly"), None)
    assert _by_id(res)["cadence_horizon_match"]["status"] == "pass"


def test_cadence_horizon_insufficient_when_missing_cadence():
    p = _profile()
    del p["cadence"]
    res = validate_strategy("s1", p, None)
    assert _by_id(res)["cadence_horizon_match"]["status"] == "insufficient_evidence"


def test_horizon_drift_fail_when_holding_far_exceeds():
    # declared = 6*30 = 180 天; 中位数 420 → ratio 2.33 > 2 → fail
    ledger = {"trips": [{"holding_days": 400}, {"holding_days": 500}, {"holding_days": 420}]}
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "fail"


def test_horizon_drift_fail_when_holding_far_shorter():
    # declared = 180; 中位数 3 → ratio 0.017 < 0.5 → fail
    ledger = {"trips": [{"holding_days": 2}, {"holding_days": 3}, {"holding_days": 4}]}
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "fail"


def test_horizon_drift_pass_when_close():
    ledger = {"trips": [{"holding_days": 170}, {"holding_days": 190}]}  # 中位数 180
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "pass"


def test_horizon_drift_boundary_2x_not_fail():
    # ratio 恰好 2.0 → 不算 > 2 倍, 应 pass (边界不含)
    ledger = {"trips": [{"holding_days": 360}, {"holding_days": 360}]}  # 360/180 = 2.0
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "pass"


def test_horizon_drift_accepts_roundtrips_alias():
    # ledger 也可能用 roundtrips 键名
    ledger = {"roundtrips": [{"holding_days": 400}]}
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "fail"


def test_horizon_drift_falls_back_to_summary_avg():
    ledger = {"summary": {"avg_holding_days": 400}}  # 无 trips, 退化用 summary
    res = validate_strategy("s1", _profile(horizon=6), ledger)
    assert _by_id(res)["horizon_drift"]["status"] == "fail"


def test_horizon_drift_insufficient_when_ledger_empty():
    res = validate_strategy("s1", _profile(horizon=6), ledger={})
    assert _by_id(res)["horizon_drift"]["status"] == "insufficient_evidence"



# ── P6.2 新增检查项 ─────────────────────────────────────
def test_playbook_declared_pass_when_complete():
    p = _profile()
    p["playbook"] = {"scope": "A股趋势股", "entry": "突破20日新高", "exit": "跌破10日线"}
    assert _by_id(validate_strategy("s1", p, None))["playbook_declared"]["status"] == "pass"


def test_playbook_declared_warn_when_missing_text():
    p = _profile()
    p["playbook"] = {"scope": "A股趋势股", "entry": "", "exit": "跌破10日线"}  # entry 空
    assert _by_id(validate_strategy("s1", p, None))["playbook_declared"]["status"] == "partial"


def test_playbook_declared_warn_when_absent():
    # 无 playbook 键 → warn (不是 fail)
    assert _by_id(validate_strategy("s1", _profile(), None))["playbook_declared"]["status"] == "partial"


def test_playbook_declared_skip_when_no_profile():
    assert _by_id(validate_strategy("s1", None, None))["playbook_declared"]["status"] == "insufficient_evidence"


def test_family_conflict_fail_when_mixed_missing_mix():
    p = _profile()
    p["family"] = "mixed"  # 缺 familyMix
    assert _by_id(validate_strategy("s1", p, None))["family_conflict"]["status"] == "fail"


def test_family_conflict_fail_when_mixed_missing_one_key():
    p = _profile()
    p["family"] = "mixed"
    p["familyMix"] = {  # 缺 conflictResolution
        "entryJudge": "趋势突破", "invalidationAuthority": "趋势破位",
        "sizingHorizon": "ATR 定仓", "conflictResolution": "",
    }
    assert _by_id(validate_strategy("s1", p, None))["family_conflict"]["status"] == "fail"


def test_family_conflict_pass_when_mixed_complete():
    p = _profile()
    p["family"] = "mixed"
    p["familyMix"] = {
        "entryJudge": "趋势突破", "invalidationAuthority": "趋势破位",
        "sizingHorizon": "ATR 定仓", "conflictResolution": "趋势优先",
    }
    assert _by_id(validate_strategy("s1", p, None))["family_conflict"]["status"] == "pass"


def test_family_conflict_skip_when_not_mixed():
    p = _profile()
    p["family"] = "value"
    assert _by_id(validate_strategy("s1", p, None))["family_conflict"]["status"] == "insufficient_evidence"


def test_family_conflict_skip_when_no_family():
    assert _by_id(validate_strategy("s1", _profile(), None))["family_conflict"]["status"] == "insufficient_evidence"


def test_family_behavior_warn_trend_held_too_long():
    # trend 高频, 中位数 35 天 > 20 → warn
    p = _profile()
    p["family"] = "trend"
    ledger = {"trips": [{"holding_days": 30}, {"holding_days": 35}, {"holding_days": 40}]}
    assert _by_id(validate_strategy("s1", p, ledger))["family_behavior_conflict"]["status"] == "partial"


def test_family_behavior_warn_value_held_too_short():
    # value 低频, 中位数 3 天 < 5 → warn
    p = _profile()
    p["family"] = "value"
    ledger = {"trips": [{"holding_days": 2}, {"holding_days": 3}, {"holding_days": 4}]}
    assert _by_id(validate_strategy("s1", p, ledger))["family_behavior_conflict"]["status"] == "partial"


def test_family_behavior_pass_trend_short_hold():
    # trend 高频, 中位数 10 天 <= 20 → pass
    p = _profile()
    p["family"] = "trend"
    ledger = {"trips": [{"holding_days": 8}, {"holding_days": 10}, {"holding_days": 12}]}
    assert _by_id(validate_strategy("s1", p, ledger))["family_behavior_conflict"]["status"] == "pass"


def test_family_behavior_pass_value_long_hold():
    # value 低频, 中位数 60 天 >= 5 → pass
    p = _profile()
    p["family"] = "growth"
    ledger = {"trips": [{"holding_days": 50}, {"holding_days": 60}]}
    assert _by_id(validate_strategy("s1", p, ledger))["family_behavior_conflict"]["status"] == "pass"


def test_family_behavior_skip_when_cadence_undefined():
    # event 节奏倾向未机械定义 → skip
    p = _profile()
    p["family"] = "event"
    ledger = {"trips": [{"holding_days": 100}]}
    assert _by_id(validate_strategy("s1", p, ledger))["family_behavior_conflict"]["status"] == "insufficient_evidence"


def test_family_behavior_skip_when_no_ledger():
    p = _profile()
    p["family"] = "trend"
    assert _by_id(validate_strategy("s1", p, None))["family_behavior_conflict"]["status"] == "insufficient_evidence"


def test_proposal_governance_pass_when_complete():
    props = [{"id": "p1", "target": "strategy s1 调参", "falsifier": "若回撤超10%", "sampleSize": 12}]
    assert _by_id(validate_strategy("s1", _profile(), None, props))["proposal_governance"]["status"] == "pass"


def test_proposal_governance_warn_when_missing_falsifier():
    props = [{"id": "p1", "target": "s1", "sampleSize": 12}]  # 缺 falsifier
    assert _by_id(validate_strategy("s1", _profile(), None, props))["proposal_governance"]["status"] == "partial"


def test_proposal_governance_warn_when_sample_zero():
    props = [{"id": "p1", "target": "s1", "falsifier": "fx", "sampleSize": 0}]  # 复核样本=0
    assert _by_id(validate_strategy("s1", _profile(), None, props))["proposal_governance"]["status"] == "partial"


def test_proposal_governance_skip_when_no_proposals():
    # 默认 None → skip (向后兼容)
    assert _by_id(validate_strategy("s1", _profile(), None))["proposal_governance"]["status"] == "insufficient_evidence"


def test_proposal_governance_skip_when_unrelated():
    props = [{"id": "p1", "target": "别的策略", "falsifier": "fx", "sampleSize": 12}]
    assert _by_id(validate_strategy("s1", _profile(), None, props))["proposal_governance"]["status"] == "insufficient_evidence"


def test_old_three_checks_unchanged_with_new_fields():
    # 新增 4 检查不影响既有 3 项结果
    ledger = {"trips": [{"holding_days": 170}, {"holding_days": 190}]}
    by = _by_id(validate_strategy("s1", _profile(horizon=6), ledger))
    assert by["field_completeness"]["status"] == "pass"
    assert by["cadence_horizon_match"]["status"] == "pass"
    assert by["horizon_drift"]["status"] == "pass"