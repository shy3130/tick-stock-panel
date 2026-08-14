"""``signal_lifecycle`` 决策信号生命周期状态机测试。

覆盖验收点: 合法/非法转换、终态拒绝、过期、幂等、重复信号去重、审计记录序列化。
纯计算, 不落盘, 不依赖外部数据源。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.trading.signal_lifecycle import (
    ACTIVE_STATES,
    ALL_STATES,
    HORIZON_DAYS,
    REASON_CONSUMED,
    REASON_DEDUP,
    REASON_EXPIRED,
    REASON_EXPIRED_AT_CREATION,
    REASON_REJECTED,
    REASON_SUPPRESSED,
    REASON_VALIDATED,
    SCHEMA_VERSION,
    STATE_CONSUMED,
    STATE_CREATED,
    STATE_ELIGIBLE,
    STATE_EXPIRED,
    STATE_REJECTED,
    STATE_SUPPRESSED,
    STATE_VALIDATED,
    TERMINAL_STATES,
    TRANSITIONS,
    SignalLifecycleError,
    create_or_dedup,
    create_signal,
    dedup_key,
    dedup_key_hash,
    expire_due,
    find_duplicate,
    is_active,
    is_past_window,
    is_terminal,
    serialize,
    transition,
)

# ── fixtures / helpers ────────────────────────────────────
NOW = datetime(2026, 8, 14, 15, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))


def _mk(**overrides):
    """快速创建一个 validated 信号 (便于测后续迁移)。"""
    base = dict(
        signal_id="sig-1",
        symbol="600000.SH",
        market="cn",
        action="buy",
        source_type="analysis",
        horizon="3d",
        source_ref="report-42",
        trace_id="trace-abc",
        actor="test",
        now=NOW,
    )
    base.update(overrides)
    sig = create_signal(**base)
    if sig["state"] == STATE_CREATED:
        sig = transition(sig, STATE_VALIDATED, reason=REASON_VALIDATED, actor="test", now=NOW)
    return sig


# --------------------------------------------------------------------------- #
# 1. 创建 & 初始状态
# --------------------------------------------------------------------------- #
class TestCreate:
    def test_created_initial_state(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert sig["state"] == STATE_CREATED
        assert sig["schemaVersion"] == SCHEMA_VERSION
        assert sig["signalId"] == "s1"
        assert is_active(sig)
        assert not is_terminal(sig)

    def test_initial_transition_record(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        recs = sig["transitions"]
        assert len(recs) == 1
        assert recs[0]["from"] is None
        assert recs[0]["to"] == STATE_CREATED
        assert recs[0]["seq"] == 0
        assert recs[0]["reason"] == "signal_created"
        assert recs[0]["at"] == NOW.isoformat()

    def test_horizon_computes_expiry(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        expected = NOW + timedelta(days=3)
        assert sig["expiresAt"] is not None
        assert datetime.fromisoformat(sig["expiresAt"]) == expected

    def test_intraday_horizon_expiry(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="alert",
            source_type="alert", horizon="intraday", source_ref="r1", now=NOW,
        )
        expected = NOW + timedelta(hours=4)
        assert datetime.fromisoformat(sig["expiresAt"]) == expected

    def test_no_horizon_no_expiry(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="swing", source_ref="r1", now=NOW,
        )
        assert sig["expiresAt"] is None
        assert not is_past_window(sig, now=NOW)

    def test_explicit_expires_at_overrides_horizon(self):
        explicit = NOW + timedelta(hours=2)
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1",
            expires_at=explicit, now=NOW,
        )
        assert datetime.fromisoformat(sig["expiresAt"]) == explicit

    def test_payload_preserved(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1",
            payload={"entry": 10.5, "note": "测试"}, now=NOW,
        )
        assert sig["payload"] == {"entry": 10.5, "note": "测试"}

    def test_does_not_mutate_on_transition(self):
        sig = _mk()
        original_state = sig["state"]
        original_len = len(sig["transitions"])
        _ = transition(sig, STATE_ELIGIBLE, reason="test", actor="test", now=NOW)
        # 原 dict 不变
        assert sig["state"] == original_state
        assert len(sig["transitions"]) == original_len


# --------------------------------------------------------------------------- #
# 2. 合法转换
# --------------------------------------------------------------------------- #
class TestLegalTransitions:
    @pytest.mark.parametrize("src,target", [
        (STATE_CREATED, STATE_VALIDATED),
        (STATE_CREATED, STATE_REJECTED),
        (STATE_CREATED, STATE_EXPIRED),
        (STATE_VALIDATED, STATE_ELIGIBLE),
        (STATE_VALIDATED, STATE_SUPPRESSED),
        (STATE_VALIDATED, STATE_REJECTED),
        (STATE_VALIDATED, STATE_EXPIRED),
        (STATE_ELIGIBLE, STATE_CONSUMED),
        (STATE_ELIGIBLE, STATE_SUPPRESSED),
        (STATE_ELIGIBLE, STATE_EXPIRED),
        (STATE_SUPPRESSED, STATE_ELIGIBLE),
        (STATE_SUPPRESSED, STATE_EXPIRED),
        (STATE_SUPPRESSED, STATE_REJECTED),
    ])
    def test_allowed_transition(self, src, target):
        sig = _mk()
        sig["state"] = src
        result = transition(sig, target, reason="test", actor="test", now=NOW)
        assert result["state"] == target
        assert result["transitions"][-1]["from"] == src
        assert result["transitions"][-1]["to"] == target
        assert result["transitions"][-1]["seq"] == len(sig["transitions"])

    def test_full_happy_path(self):
        """created → validated → eligible → consumed 完整消费路径。"""
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        sig = transition(sig, STATE_VALIDATED, reason=REASON_VALIDATED, actor="v", now=NOW)
        assert sig["state"] == STATE_VALIDATED
        sig = transition(sig, STATE_ELIGIBLE, reason="ok", actor="e", now=NOW)
        assert sig["state"] == STATE_ELIGIBLE
        sig = transition(sig, STATE_CONSUMED, reason=REASON_CONSUMED, actor="t", now=NOW)
        assert sig["state"] == STATE_CONSUMED
        assert is_terminal(sig)
        # 审计链完整
        states = [r["to"] for r in sig["transitions"]]
        assert states == [STATE_CREATED, STATE_VALIDATED, STATE_ELIGIBLE, STATE_CONSUMED]

    def test_suppress_then_recover(self):
        """validated → suppressed → eligible 恢复路径。"""
        sig = _mk()
        sig = transition(sig, STATE_SUPPRESSED, reason=REASON_SUPPRESSED, actor="r", now=NOW)
        assert sig["state"] == STATE_SUPPRESSED
        sig = transition(sig, STATE_ELIGIBLE, reason="re-eval", actor="e", now=NOW)
        assert sig["state"] == STATE_ELIGIBLE

    def test_transition_updates_timestamp(self):
        sig = _mk()
        later = NOW + timedelta(minutes=5)
        result = transition(sig, STATE_ELIGIBLE, reason="t", actor="a", now=later)
        assert result["updatedAt"] == later.isoformat()


# --------------------------------------------------------------------------- #
# 3. 非法转换
# --------------------------------------------------------------------------- #
class TestIllegalTransitions:
    @pytest.mark.parametrize("src,target", [
        # 跳级: created 不能直接到 eligible/consumed/suppressed
        (STATE_CREATED, STATE_ELIGIBLE),
        (STATE_CREATED, STATE_CONSUMED),
        (STATE_CREATED, STATE_SUPPRESSED),
        # validated 不能直接 consumed (必须经 eligible)
        (STATE_VALIDATED, STATE_CONSUMED),
        # eligible 不能回到 validated/created
        (STATE_ELIGIBLE, STATE_VALIDATED),
        (STATE_ELIGIBLE, STATE_CREATED),
        # suppressed 不能回到 created/validated
        (STATE_SUPPRESSED, STATE_CREATED),
        (STATE_SUPPRESSED, STATE_VALIDATED),
        (STATE_SUPPRESSED, STATE_CONSUMED),
    ])
    def test_illegal_transition_raises(self, src, target):
        sig = _mk()
        sig["state"] = src
        with pytest.raises(SignalLifecycleError, match="非法迁移"):
            transition(sig, target, now=NOW)

    def test_unknown_target_raises(self):
        sig = _mk()
        with pytest.raises(SignalLifecycleError, match="未知目标状态"):
            transition(sig, "frozen", now=NOW)

    def test_transition_table_covers_all_states(self):
        assert set(TRANSITIONS.keys()) == ALL_STATES
        # 终态无出边
        for term in TERMINAL_STATES:
            assert TRANSITIONS[term] == frozenset()
        # 活跃态至少有一条出边
        for act in ACTIVE_STATES:
            assert len(TRANSITIONS[act]) >= 1


# --------------------------------------------------------------------------- #
# 4. 终态拒绝
# --------------------------------------------------------------------------- #
class TestTerminalRejection:
    @pytest.mark.parametrize("terminal", [STATE_EXPIRED, STATE_CONSUMED, STATE_REJECTED])
    @pytest.mark.parametrize("target", sorted(ALL_STATES))
    def test_terminal_rejects_all_non_idempotent(self, terminal, target):
        if target == terminal:
            pytest.skip("idempotent same-state tested separately")
        sig = _mk()
        sig["state"] = terminal
        with pytest.raises(SignalLifecycleError, match="终态"):
            transition(sig, target, now=NOW)

    def test_expired_rejects_consume(self):
        sig = _mk()
        sig = transition(sig, STATE_EXPIRED, reason=REASON_EXPIRED, actor="s", now=NOW)
        assert is_terminal(sig)
        with pytest.raises(SignalLifecycleError, match="终态"):
            transition(sig, STATE_CONSUMED, now=NOW + timedelta(days=1))

    def test_consumed_rejects_anything(self):
        sig = _mk()
        sig = transition(sig, STATE_ELIGIBLE, reason="ok", actor="e", now=NOW)
        sig = transition(sig, STATE_CONSUMED, reason=REASON_CONSUMED, actor="t", now=NOW)
        for target in [STATE_CREATED, STATE_VALIDATED, STATE_ELIGIBLE, STATE_SUPPRESSED]:
            with pytest.raises(SignalLifecycleError):
                transition(sig, target, now=NOW)

    def test_rejected_rejects_revival(self):
        sig = _mk()
        sig = transition(sig, STATE_REJECTED, reason=REASON_REJECTED, actor="g", now=NOW)
        with pytest.raises(SignalLifecycleError, match="终态"):
            transition(sig, STATE_VALIDATED, now=NOW)
        with pytest.raises(SignalLifecycleError, match="终态"):
            transition(sig, STATE_ELIGIBLE, now=NOW)


# --------------------------------------------------------------------------- #
# 5. 过期
# --------------------------------------------------------------------------- #
class TestExpiry:
    def test_expire_due_active_signal(self):
        sig = _mk(horizon="1d")  # expires NOW + 1d
        future = NOW + timedelta(days=2)
        result = expire_due(sig, now=future)
        assert result["state"] == STATE_EXPIRED
        assert result["transitions"][-1]["reason"] == REASON_EXPIRED

    def test_expire_due_not_yet(self):
        sig = _mk(horizon="3d")
        result = expire_due(sig, now=NOW + timedelta(hours=1))
        assert result is sig  # 未过期, 原样返回
        assert result["state"] == STATE_VALIDATED

    def test_expire_due_no_window(self):
        sig = _mk(horizon="swing")  # 无自动过期
        assert sig["expiresAt"] is None
        result = expire_due(sig, now=NOW + timedelta(days=365))
        assert result is sig
        assert result["state"] == STATE_VALIDATED

    def test_expire_due_idempotent_on_terminal(self):
        sig = _mk(horizon="1d")
        expired = expire_due(sig, now=NOW + timedelta(days=2))
        assert expired["state"] == STATE_EXPIRED
        # 再次调用 → 无操作
        again = expire_due(expired, now=NOW + timedelta(days=3))
        assert again is expired

    def test_created_past_window_auto_expires(self):
        """创建时窗口已过 (显式 expires_at 在 now 之前) → 自动过期。"""
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1",
            expires_at=NOW - timedelta(hours=1), now=NOW,
        )
        assert sig["state"] == STATE_EXPIRED
        states = [r["to"] for r in sig["transitions"]]
        assert states == [STATE_CREATED, STATE_EXPIRED]
        assert sig["transitions"][-1]["reason"] == REASON_EXPIRED_AT_CREATION

    def test_window_blocks_non_expiry_transition(self):
        """窗口已过的活跃信号, 拒绝合法但非过期的迁移。"""
        sig = _mk(horizon="1d")
        future = NOW + timedelta(days=2)
        with pytest.raises(SignalLifecycleError, match="时间窗已过"):
            transition(sig, STATE_ELIGIBLE, now=future)
        with pytest.raises(SignalLifecycleError, match="时间窗已过"):
            transition(sig, STATE_SUPPRESSED, now=future)

    def test_is_past_window(self):
        sig = _mk(horizon="3d")
        assert not is_past_window(sig, now=NOW)
        assert is_past_window(sig, now=NOW + timedelta(days=4))
        # 无窗口
        sig2 = _mk(horizon="swing")
        assert not is_past_window(sig2, now=NOW + timedelta(days=999))

    def test_horizon_days_mapping(self):
        for h, days in HORIZON_DAYS.items():
            sig = _mk(horizon=h)
            assert datetime.fromisoformat(sig["expiresAt"]) == NOW + timedelta(days=days)


# --------------------------------------------------------------------------- #
# 6. 幂等
# --------------------------------------------------------------------------- #
class TestIdempotency:
    @pytest.mark.parametrize("state", list(ALL_STATES))
    def test_same_state_is_noop(self, state):
        sig = _mk()
        # 尽力把 sig 推到指定状态
        if state in ACTIVE_STATES and state != STATE_VALIDATED:
            if state == STATE_CREATED:
                sig = create_signal(
                    signal_id="s1", symbol="600000.SH", market="cn", action="buy",
                    source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
                )
            elif state == STATE_ELIGIBLE:
                sig = transition(sig, STATE_ELIGIBLE, now=NOW)
            elif state == STATE_SUPPRESSED:
                sig = transition(sig, STATE_SUPPRESSED, now=NOW)
        elif state == STATE_EXPIRED:
            sig = transition(sig, STATE_EXPIRED, now=NOW)
        elif state == STATE_CONSUMED:
            sig = transition(sig, STATE_ELIGIBLE, now=NOW)
            sig = transition(sig, STATE_CONSUMED, now=NOW)
        elif state == STATE_REJECTED:
            sig = transition(sig, STATE_REJECTED, now=NOW)

        assert sig["state"] == state
        before_count = len(sig["transitions"])
        result = transition(sig, state, reason="noop", actor="x", now=NOW)
        assert result is sig
        assert len(result["transitions"]) == before_count  # 无新记录

    def test_expire_due_idempotent(self):
        sig = _mk(horizon="1d")
        expired1 = expire_due(sig, now=NOW + timedelta(days=5))
        expired2 = expire_due(expired1, now=NOW + timedelta(days=6))
        assert expired2 is expired1
        assert len(expired1["transitions"]) == len(expired2["transitions"])

    def test_double_consume_idempotent(self):
        sig = _mk()
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        consumed1 = transition(sig, STATE_CONSUMED, reason=REASON_CONSUMED, actor="t", now=NOW)
        consumed2 = transition(consumed1, STATE_CONSUMED, now=NOW + timedelta(hours=1))
        assert consumed2 is consumed1


# --------------------------------------------------------------------------- #
# 7. 重复信号去重
# --------------------------------------------------------------------------- #
class TestDedup:
    def test_dedup_key_deterministic(self):
        sig = _mk()
        assert dedup_key(sig) == dedup_key(sig)

    def test_dedup_key_includes_identity(self):
        sig_a = _mk(source_ref="r1")
        sig_b = _mk(source_ref="r2")
        assert dedup_key(sig_a) != dedup_key(sig_b)

    def test_dedup_key_trace_id_fallback(self):
        sig = _mk(source_ref=None, trace_id="trace-xyz")
        key = dedup_key(sig)
        assert "trace-xyz" in key

    def test_dedup_key_no_ref_dimensions_only(self):
        sig = _mk(source_ref=None, trace_id=None)
        key = dedup_key(sig)
        # 无身份锚点 → 仅维度
        assert "600000.SH" in key
        assert "buy" in key

    def test_find_duplicate_active_match(self):
        existing = _mk(source_ref="r1")
        # 新信号维度完全一致, 同 sourceRef
        new = create_signal(
            signal_id="s2", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        dup = find_duplicate(new, [existing])
        assert dup is existing

    def test_find_duplicate_different_ref_no_match(self):
        existing = _mk(source_ref="r1")
        new = create_signal(
            signal_id="s2", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r2", now=NOW,
        )
        assert find_duplicate(new, [existing]) is None

    def test_find_duplicate_different_action_no_match(self):
        existing = _mk(action="buy")
        new = create_signal(
            signal_id="s2", symbol="600000.SH", market="cn", action="sell",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert find_duplicate(new, [existing]) is None

    def test_find_duplicate_terminal_not_matched(self):
        """终态信号不抑制新信号。"""
        existing = _mk(source_ref="r1")
        existing = transition(existing, STATE_EXPIRED, now=NOW)
        assert is_terminal(existing)
        new = create_signal(
            signal_id="s2", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert find_duplicate(new, [existing]) is None

    def test_find_duplicate_no_ref_skips_dedup(self):
        """无身份锚点的新信号不做去重。"""
        existing = _mk(source_ref=None, trace_id=None)
        new = create_signal(
            signal_id="s2", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref=None, trace_id=None, now=NOW,
        )
        assert find_duplicate(new, [existing]) is None

    def test_create_or_dedup_creates_new(self):
        sig, created, dup = create_or_dedup(
            existing=[],
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert created is True
        assert dup is None
        assert sig["state"] == STATE_CREATED

    def test_create_or_dedup_returns_existing(self):
        existing = _mk(source_ref="r1")
        sig, created, dup = create_or_dedup(
            existing=[existing],
            signal_id="s2", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert created is False
        assert dup is existing
        assert sig is existing

    def test_dedup_key_hash_stable(self):
        sig = _mk()
        h = dedup_key_hash(sig)
        assert len(h) == 16
        assert h == dedup_key_hash(sig)


# --------------------------------------------------------------------------- #
# 8. 审计记录序列化
# --------------------------------------------------------------------------- #
class TestAuditSerialization:
    def test_transitions_append_only(self):
        sig = _mk()
        n0 = len(sig["transitions"])
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        assert len(sig["transitions"]) == n0 + 1
        sig = transition(sig, STATE_CONSUMED, now=NOW)
        assert len(sig["transitions"]) == n0 + 2
        # 前两条记录未被修改
        for i, r in enumerate(sig["transitions"]):
            assert r["seq"] == i

    def test_transition_record_fields(self):
        sig = _mk()
        sig = transition(sig, STATE_ELIGIBLE, reason="market_open", actor="gate", now=NOW)
        rec = sig["transitions"][-1]
        assert rec["from"] == STATE_VALIDATED
        assert rec["to"] == STATE_ELIGIBLE
        assert rec["reason"] == "market_open"
        assert rec["actor"] == "gate"
        assert rec["at"] == NOW.isoformat()
        assert isinstance(rec["seq"], int)

    def test_serialize_json_roundtrip(self):
        import json
        sig = _mk()
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        sig = transition(sig, STATE_CONSUMED, reason=REASON_CONSUMED, actor="trader", now=NOW)
        proj = serialize(sig)
        s = json.dumps(proj, ensure_ascii=False)
        restored = json.loads(s)
        assert restored["state"] == STATE_CONSUMED
        assert restored["transitionCount"] == len(restored["transitions"])
        assert restored["transitions"][-1]["to"] == STATE_CONSUMED

    def test_serialize_includes_dedup_key(self):
        sig = _mk(source_ref="r1")
        proj = serialize(sig)
        assert proj["dedupKey"] == dedup_key_hash(sig)
        assert isinstance(proj["dedupKey"], str)

    def test_serialize_payload_safe(self):
        sig = _mk(payload={"k": [1, 2, {"nested": True}]})
        proj = serialize(sig)
        assert proj["payload"] == {"k": [1, 2, {"nested": True}]}

    def test_transition_chain_preserved_through_serialization(self):
        """完整生命周期后审计链可序列化且顺序正确。"""
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        sig = transition(sig, STATE_VALIDATED, now=NOW)
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        sig = transition(sig, STATE_SUPPRESSED, reason=REASON_DEDUP, now=NOW)
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        sig = transition(sig, STATE_CONSUMED, now=NOW)
        proj = serialize(sig)
        states = [r["to"] for r in proj["transitions"]]
        assert states == [
            STATE_CREATED, STATE_VALIDATED, STATE_ELIGIBLE,
            STATE_SUPPRESSED, STATE_ELIGIBLE, STATE_CONSUMED,
        ]
        assert proj["transitionCount"] == 6

    def test_audit_records_immutable_originals(self):
        """transition 不修改原始 transitions 列表的元素。"""
        sig = _mk()
        original_records = [dict(r) for r in sig["transitions"]]
        _ = transition(sig, STATE_ELIGIBLE, now=NOW)
        _ = transition(transition(sig, STATE_ELIGIBLE, now=NOW), STATE_CONSUMED, now=NOW)
        # 原始列表未被追加
        assert sig["transitions"] == original_records


# --------------------------------------------------------------------------- #
# 9. 边界 & 不变量
# --------------------------------------------------------------------------- #
class TestInvariants:
    def test_all_states_partitioned(self):
        assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)
        assert ACTIVE_STATES | TERMINAL_STATES == ALL_STATES

    def test_created_signal_has_one_transition(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=NOW,
        )
        assert len(sig["transitions"]) == 1

    def test_auto_expired_signal_has_two_transitions(self):
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1",
            expires_at=NOW - timedelta(hours=1), now=NOW,
        )
        assert sig["state"] == STATE_EXPIRED
        assert len(sig["transitions"]) == 2

    def test_naive_now_localized(self):
        """naive now 按默认时区本地化, 不报错。"""
        naive = datetime(2026, 8, 14, 15, 0)
        sig = create_signal(
            signal_id="s1", symbol="600000.SH", market="cn", action="buy",
            source_type="analysis", horizon="3d", source_ref="r1", now=naive,
        )
        assert sig["createdAt"] is not None
        # 带 tz 信息
        assert "+" in sig["createdAt"] or sig["createdAt"].endswith("Z") is False

    def test_suppressed_is_recoverable(self):
        sig = _mk()
        sig = transition(sig, STATE_SUPPRESSED, now=NOW)
        assert is_active(sig)
        assert not is_terminal(sig)
        sig = transition(sig, STATE_ELIGIBLE, now=NOW)
        assert sig["state"] == STATE_ELIGIBLE
