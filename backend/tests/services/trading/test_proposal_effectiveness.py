"""提案生效验证测试 — 纯函数判定覆盖五维门禁与确定性。

覆盖 (acceptance):
- 批准/未批准、过期、版本冲突、反证命中、观察期未满、效果达标/失败、确定性。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.trading.proposal_effectiveness import (
    ACTION_AWAIT_HUMAN_APPROVAL,
    ACTION_COLLECT_DATA,
    ACTION_NONE,
    ACTION_REJECT,
    ACTION_RESOLVE_VERSION,
    ACTION_REVIEW,
    ACTION_ROLLBACK,
    ACTION_SCHEDULE,
    CurrentProfile,
    EffectivenessVerdict,
    ProposalContext,
    VERDICT_COUNTER_EVIDENCE_HIT,
    VERDICT_EFFECTIVE,
    VERDICT_EXPIRED,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_NOT_APPROVED,
    VERDICT_NOT_EFFECTIVE,
    VERDICT_NOT_YET_EFFECTIVE,
    VERDICT_PENDING_OBSERVATION,
    VERDICT_VERSION_CONFLICT,
    evaluate_proposal_effectiveness,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _ctx(**overrides) -> ProposalContext:
    """构造一个「全部门禁通过 → effective」的基础提案, 测试按需破坏单维。"""
    base = dict(
        proposal_id="prop_20260801_01",
        status="verified",
        falsifier="连续 3 笔同类执行偏离则证伪",
        human_approved=True,
        approved_at="2026-08-01 10:00",
        target_profile_id="user-profile",
        target_profile_version="v3",
        target_strategy_families=["trend", "momentum"],
        effective_from="2026-08-02 09:30",
        effective_until="2026-12-31 23:59",
        review_after="2026-08-10 23:59",
        min_observation_samples=10,
        observation_samples=15,
        outcome_met=True,
        falsifier_hits=[],
    )
    base.update(overrides)
    return ProposalContext(**base)


def _profile(**overrides) -> CurrentProfile:
    base = dict(
        profile_id="user-profile",
        version="v3",
        strategy_families=["trend", "momentum"],
        status="active",
    )
    base.update(overrides)
    return CurrentProfile(**base)


def _eval(proposal, profile="default", now=NOW):
    if profile == "default":
        profile = _profile()
    return evaluate_proposal_effectiveness(proposal, current_profile=profile, now=now)

# ── 1. 人工批准 / 未批准 ───────────────────────────────────
class TestHumanApproval:
    def test_draft_not_approved(self):
        v = _eval(_ctx(status="draft"))
        assert v.verdict == VERDICT_NOT_APPROVED
        assert v.required_action == ACTION_AWAIT_HUMAN_APPROVAL
        assert not v.is_effective
        assert any(c["check"] == "human_approval" and not c["passed"] for c in v.evidence)

    def test_rejected_not_approved(self):
        v = _eval(_ctx(status="rejected", human_approved=True))
        assert v.verdict == VERDICT_NOT_APPROVED

    def test_trial_without_human_approved_flag(self):
        # status=trial 但 human_approved=False → 不能自动批准
        v = _eval(_ctx(status="trial", human_approved=False,
                       observation_samples=15, outcome_met=True))
        assert v.verdict == VERDICT_NOT_APPROVED
        assert "不得自动批准" in v.reason

    def test_approved_with_human_flag_passes_approval_gate(self):
        # approved 但未进 trial → 通过批准门禁, 后续观察门禁会 pending
        v = _eval(_ctx(status="approved", human_approved=True))
        assert v.verdict == VERDICT_PENDING_OBSERVATION
        approval = [c for c in v.evidence if c["check"] == "human_approval"][0]
        assert approval["passed"] is True


# ── 2. insufficient_data (缺 falsifier) ────────────────────
class TestInsufficientData:
    def test_missing_falsifier_short_circuits(self):
        v = _eval(_ctx(falsifier=None))
        assert v.verdict == VERDICT_INSUFFICIENT_DATA
        assert v.required_action == ACTION_COLLECT_DATA
        # falsifier 是第一门禁, 后续门禁不应执行
        assert [c["check"] for c in v.evidence] == ["falsifier_defined"]

    def test_empty_falsifier_short_circuits(self):
        v = _eval(_ctx(falsifier="   "))
        assert v.verdict == VERDICT_INSUFFICIENT_DATA

    def test_falsifier_present_passes(self):
        v = _eval(_ctx(falsifier="论点失效信号出现"))
        assert v.verdict != VERDICT_INSUFFICIENT_DATA
        fd = [c for c in v.evidence if c["check"] == "falsifier_defined"][0]
        assert fd["passed"] is True


# ── 3. 版本/策略族冲突 ─────────────────────────────────────
class TestVersionConflict:
    def test_profile_id_mismatch(self):
        v = _eval(_ctx(), profile=_profile(profile_id="other-profile"))
        assert v.verdict == VERDICT_VERSION_CONFLICT
        assert v.required_action == ACTION_RESOLVE_VERSION
        assert not v.is_effective

    def test_profile_version_mismatch(self):
        v = _eval(_ctx(target_profile_version="v2"), profile=_profile(version="v3"))
        assert v.verdict == VERDICT_VERSION_CONFLICT
        assert "Profile 已演进" in v.reason

    def test_strategy_family_no_intersection(self):
        v = _eval(_ctx(target_strategy_families=["value", "mean_reversion"]),
                  profile=_profile(strategy_families=["trend"]))
        assert v.verdict == VERDICT_VERSION_CONFLICT
        assert "策略族无交集" in v.reason

    def test_strategy_family_intersection_passes(self):
        # 提案族与当前族有交集即通过 (混合族允许)
        v = _eval(_ctx(target_strategy_families=["trend", "value"]),
                  profile=_profile(strategy_families=["trend", "momentum"]))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_version_match_when_target_unspecified(self):
        # 提案未指定 target → 匹配门禁通过
        v = _eval(_ctx(target_profile_id=None, target_profile_version=None,
                       target_strategy_families=[]),
                  profile=_profile())
        assert v.verdict == VERDICT_EFFECTIVE

    def test_no_current_profile_skips_version_gate(self):
        v = _eval(_ctx(), profile=None)
        # 跳过版本门禁, 其余通过 → effective
        assert v.verdict == VERDICT_EFFECTIVE
        vm = [c for c in v.evidence if c["check"] == "version_match"][0]
        assert vm["passed"] is True
        assert "跳过" in vm["detail"]


# ── 4. 生效时间: 未到 / 过期 ───────────────────────────────
class TestEffectiveTime:
    def test_not_yet_effective(self):
        future = NOW + timedelta(days=5)
        v = _eval(_ctx(effective_from=future.isoformat()))
        assert v.verdict == VERDICT_NOT_YET_EFFECTIVE
        assert v.required_action == ACTION_SCHEDULE

    def test_expired(self):
        past_until = NOW - timedelta(days=1)
        v = _eval(_ctx(effective_until=past_until.isoformat()))
        assert v.verdict == VERDICT_EXPIRED
        assert not v.is_effective

    def test_within_window_passes(self):
        v = _eval(_ctx(effective_from=(NOW - timedelta(days=2)).isoformat(),
                       effective_until=(NOW + timedelta(days=30)).isoformat()))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_no_time_bounds_passes(self):
        v = _eval(_ctx(effective_from=None, effective_until=None))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_effective_from_only_not_yet(self):
        v = _eval(_ctx(effective_from=(NOW + timedelta(hours=1)).isoformat(),
                       effective_until=None))
        assert v.verdict == VERDICT_NOT_YET_EFFECTIVE


# ── 5. 反证命中 ────────────────────────────────────────────
class TestCounterEvidence:
    def test_falsifier_hit_invalidates(self):
        hits = [{"name": "连续3笔执行偏离", "observed_at": "2026-08-12"}]
        v = _eval(_ctx(falsifier_hits=hits))
        assert v.verdict == VERDICT_COUNTER_EVIDENCE_HIT
        assert v.required_action == ACTION_ROLLBACK
        assert not v.is_effective

    def test_falsifier_hit_invalidates_even_verified(self):
        # verified 提案命中反证也失效 (不变量 2)
        hits = [{"signal": "趋势彻底断裂"}]
        v = _eval(_ctx(status="verified", falsifier_hits=hits))
        assert v.verdict == VERDICT_COUNTER_EVIDENCE_HIT

    def test_no_hits_passes(self):
        v = _eval(_ctx(falsifier_hits=[]))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_hit_as_plain_string(self):
        v = _eval(_ctx(falsifier_hits=["论点失效"]))
        assert v.verdict == VERDICT_COUNTER_EVIDENCE_HIT


# ── 6. 观察窗口未满 ────────────────────────────────────────
class TestPendingObservation:
    def test_approved_not_yet_in_trial(self):
        v = _eval(_ctx(status="approved", human_approved=True))
        assert v.verdict == VERDICT_PENDING_OBSERVATION
        assert v.required_action == ACTION_REVIEW
        assert "进入试运行" in v.pending_conditions[0]

    def test_trial_samples_insufficient(self):
        v = _eval(_ctx(status="trial", observation_samples=3,
                       min_observation_samples=10, outcome_met=True))
        assert v.verdict == VERDICT_PENDING_OBSERVATION
        assert any("样本" in p for p in v.pending_conditions)

    def test_trial_review_period_not_elapsed(self):
        v = _eval(_ctx(status="trial", observation_samples=15,
                       min_observation_samples=10, outcome_met=True,
                       review_after=(NOW + timedelta(days=5)).isoformat()))
        assert v.verdict == VERDICT_PENDING_OBSERVATION
        assert any("观察期未满" in p for p in v.pending_conditions)

    def test_trial_outcome_not_evaluated(self):
        # 窗口已满但 outcome_met=None → 不得宣称有效
        v = _eval(_ctx(status="trial", observation_samples=15,
                       min_observation_samples=10, outcome_met=None,
                       review_after=(NOW - timedelta(days=1)).isoformat()))
        assert v.verdict == VERDICT_PENDING_OBSERVATION
        assert any("outcome_met" in p for p in v.pending_conditions)

    def test_trial_review_after_none_skips_period_check(self):
        # 无 review_after → 只看样本数
        v = _eval(_ctx(status="trial", observation_samples=15,
                       min_observation_samples=10, outcome_met=True,
                       review_after=None))
        assert v.verdict == VERDICT_EFFECTIVE


# ── 7. 效果达标 / 失败 ─────────────────────────────────────
class TestOutcome:
    def test_trial_outcome_met_is_effective(self):
        v = _eval(_ctx(status="trial", observation_samples=12,
                       min_observation_samples=10, outcome_met=True,
                       review_after=(NOW - timedelta(days=1)).isoformat()))
        assert v.verdict == VERDICT_EFFECTIVE
        assert v.is_effective

    def test_trial_outcome_not_met_is_not_effective(self):
        v = _eval(_ctx(status="trial", observation_samples=12,
                       min_observation_samples=10, outcome_met=False,
                       review_after=(NOW - timedelta(days=1)).isoformat()))
        assert v.verdict == VERDICT_NOT_EFFECTIVE
        assert v.required_action == ACTION_REJECT
        assert not v.is_effective

    def test_verified_status_skips_observation(self):
        # verified 不重复要求观察 (除非反证命中)
        v = _eval(_ctx(status="verified", observation_samples=0, outcome_met=None))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_exact_min_samples_passes(self):
        v = _eval(_ctx(status="trial", observation_samples=10,
                       min_observation_samples=10, outcome_met=True,
                       review_after=(NOW - timedelta(days=1)).isoformat()))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_one_below_min_samples_pending(self):
        v = _eval(_ctx(status="trial", observation_samples=9,
                       min_observation_samples=10, outcome_met=True))
        assert v.verdict == VERDICT_PENDING_OBSERVATION


# ── 8. 确定性 ──────────────────────────────────────────────
class TestDeterminism:
    def test_same_inputs_same_verdict(self):
        ctx = _ctx(status="trial", observation_samples=5, outcome_met=True)
        v1 = _eval(ctx)
        v2 = _eval(ctx)
        assert v1.verdict == v2.verdict
        assert v1.to_dict() == v2.to_dict()

    def test_to_dict_json_serializable(self):
        import json
        v = _eval(_ctx(status="trial", observation_samples=12, outcome_met=False,
                       review_after=(NOW - timedelta(days=1)).isoformat()))
        d = v.to_dict()
        # 必须可 JSON 序列化
        s = json.dumps(d, ensure_ascii=False)
        roundtrip = json.loads(s)
        assert roundtrip["verdict"] == VERDICT_NOT_EFFECTIVE
        assert roundtrip["proposal_id"] == "prop_20260801_01"
        assert isinstance(roundtrip["evidence"], list)
        assert isinstance(roundtrip["pending_conditions"], list)

    def test_explicit_now_overrides_wall_clock(self):
        # 不依赖真实墙钟: 注入不同 now 得到不同 verdict
        ctx = _ctx(effective_from="2026-08-20 09:30")
        before = _eval(ctx, now=datetime(2026, 8, 15, tzinfo=UTC))
        after = _eval(ctx, now=datetime(2026, 8, 25, tzinfo=UTC))
        assert before.verdict == VERDICT_NOT_YET_EFFECTIVE
        assert after.verdict == VERDICT_EFFECTIVE

    def test_default_now_used_when_none(self):
        # now=None 时取当前 UTC, 不报错 (冒烟)
        v = evaluate_proposal_effectiveness(_ctx())
        assert v.verdict in (VERDICT_EFFECTIVE,)
        assert v.verdict == VERDICT_EFFECTIVE


# ── 9. dict 输入契约 (camelCase 兼容) ──────────────────────
class TestDictInput:
    def test_dict_proposal_camel_case(self):
        proposal = {
            "id": "prop_x",
            "status": "verified",
            "falsifier": "反证",
            "humanApproved": True,
            "effectiveFrom": (NOW - timedelta(days=1)).isoformat(),
            "effectiveUntil": (NOW + timedelta(days=30)).isoformat(),
            "minObservationSamples": 5,
        }
        v = evaluate_proposal_effectiveness(proposal, now=NOW)
        assert v.verdict == VERDICT_EFFECTIVE
        assert v.proposal_id == "prop_x"

    def test_dict_profile(self):
        proposal = _ctx(target_profile_id="user-profile", target_profile_version="v3")
        profile = {"profileId": "user-profile", "version": "v3",
                   "strategyFamilies": ["trend"], "status": "active"}
        v = evaluate_proposal_effectiveness(proposal, current_profile=profile, now=NOW)
        assert v.verdict == VERDICT_EFFECTIVE


# ── 10. 门禁优先级 (短路顺序) ──────────────────────────────
class TestPrecedence:
    def test_falsifier_gate_runs_before_approval(self):
        # 缺 falsifier 时即使未批准也只报 insufficient_data
        v = _eval(_ctx(falsifier=None, status="draft", human_approved=False))
        assert v.verdict == VERDICT_INSUFFICIENT_DATA
        assert [c["check"] for c in v.evidence] == ["falsifier_defined"]

    def test_approval_before_version(self):
        # 未批准 + 版本冲突 → 报 not_approved (先命中)
        v = _eval(_ctx(status="draft", human_approved=False),
                  profile=_profile(profile_id="mismatch"))
        assert v.verdict == VERDICT_NOT_APPROVED

    def test_version_before_counter_evidence(self):
        # 版本冲突 + 反证命中 → 报 version_conflict (先命中)
        v = _eval(_ctx(falsifier_hits=[{"name": "x"}]),
                  profile=_profile(profile_id="mismatch"))
        assert v.verdict == VERDICT_VERSION_CONFLICT

    def test_counter_evidence_before_observation(self):
        # 反证命中 + 观察未满 → 报 counter_evidence_hit
        v = _eval(_ctx(status="trial", observation_samples=1, outcome_met=None,
                       falsifier_hits=[{"name": "x"}]))
        assert v.verdict == VERDICT_COUNTER_EVIDENCE_HIT

    def test_effective_emits_all_six_checks(self):
        v = _eval(_ctx())
        checks = [c["check"] for c in v.evidence]
        assert checks == [
            "falsifier_defined",
            "human_approval",
            "version_match",
            "effective_time",
            "counter_evidence",
            "observation_window",
        ]
        assert all(c["passed"] for c in v.evidence)
        assert v.required_action == ACTION_NONE
        assert v.pending_conditions == []


# ── 11. 边界 ───────────────────────────────────────────────
class TestEdgeCases:
    def test_unknown_status_treated_as_not_approved(self):
        v = _eval(_ctx(status="weird", human_approved=True))
        assert v.verdict == VERDICT_NOT_APPROVED

    def test_naive_datetime_treated_as_utc(self):
        # naive datetime 视为 UTC
        v = _eval(_ctx(), now=datetime(2026, 8, 14, 12, 0))  # naive
        assert v.verdict == VERDICT_EFFECTIVE

    def test_empty_target_families_with_current_families_passes(self):
        # 提案不限定族 → 与任意当前族兼容
        v = _eval(_ctx(target_strategy_families=[]),
                  profile=_profile(strategy_families=["trend"]))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_unparseable_effective_until_skips_time_gate(self):
        # 不可解析时间 → 视为无界限, 跳过该维度
        v = _eval(_ctx(effective_until="not-a-date", effective_from=None))
        assert v.verdict == VERDICT_EFFECTIVE

    def test_coerce_raises_on_bad_type(self):
        with pytest.raises(TypeError, match="ProposalContext | dict"):
            evaluate_proposal_effectiveness("not a proposal")  # type: ignore[arg-type]

    def test_coerce_raises_on_bad_profile_type(self):
        with pytest.raises(TypeError, match="CurrentProfile | dict"):
            evaluate_proposal_effectiveness(_ctx(), current_profile=123)  # type: ignore[arg-type]
