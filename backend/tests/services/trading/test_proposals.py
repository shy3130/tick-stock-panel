"""策略变更提案校验测试 — falsifier 必填 / 非法状态迁移 / sampleSize 门槛。"""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta

from app.services.trading import store
from app.services.trading.proposals import (
    ProposalError,
    compute_relaxation_after_loss,
    create_proposal,
    get_proposal,
    has_recent_loss,
    is_relaxation,
    list_proposals,
    update_proposal,
)

TS = "2026-08-04 14:30"


def _valid_payload(**overrides) -> dict:
    payload = {
        "title": "止损收紧阈值调整",
        "target": "gate_rules",
        "evidence": ["红旗:放宽止损 3 次"],
        "before": {"stopLossPct": 0.05},
        "after": {"stopLossPct": 0.04},
        "falsifier": "如果调整后回测最大回撤超过 15%，说明阈值过紧",
        "sampleSize": 12,
    }
    payload.update(overrides)
    return payload


# ── falsifier 必填 ───────────────────────────────────────
def test_create_requires_falsifier(tmp_path):
    with pytest.raises(ProposalError, match="falsifier"):
        create_proposal(tmp_path, _valid_payload(falsifier=""))


def test_create_requires_non_whitespace_falsifier(tmp_path):
    with pytest.raises(ProposalError, match="falsifier"):
        create_proposal(tmp_path, _valid_payload(falsifier="   "))


def test_create_with_falsifier_succeeds(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    assert proposal["falsifier"]
    assert proposal["status"] == "draft"
    assert proposal["id"].startswith("prop_")


# ── sampleSize 门槛 ──────────────────────────────────────
def test_sample_size_below_10_cannot_approve(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload(sampleSize=5))
    with pytest.raises(ProposalError, match="sampleSize"):
        update_proposal(tmp_path, proposal["id"], {"status": "approved", "sampleSize": 5})


def test_sample_size_below_10_with_patch_update_still_blocked(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload(sampleSize=12))
    # 先降到 5 再批准
    update_proposal(tmp_path, proposal["id"], {"sampleSize": 5})
    with pytest.raises(ProposalError, match="sampleSize"):
        update_proposal(tmp_path, proposal["id"], {"status": "approved"})


def test_sample_size_10_can_approve(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload(sampleSize=10))
    updated = update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    assert updated["status"] == "approved"


# ── 合法状态迁移 ─────────────────────────────────────────
def test_draft_to_approved(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    updated = update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    assert updated["status"] == "approved"


def test_draft_to_rejected(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    updated = update_proposal(tmp_path, proposal["id"], {"status": "rejected"})
    assert updated["status"] == "rejected"


def test_approved_to_trial(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    updated = update_proposal(tmp_path, proposal["id"], {"status": "trial"})
    assert updated["status"] == "trial"


def test_trial_to_verified(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    update_proposal(tmp_path, proposal["id"], {"status": "trial"})
    updated = update_proposal(tmp_path, proposal["id"], {"status": "verified"})
    assert updated["status"] == "verified"


def test_trial_to_rejected(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    update_proposal(tmp_path, proposal["id"], {"status": "trial"})
    updated = update_proposal(tmp_path, proposal["id"], {"status": "rejected"})
    assert updated["status"] == "rejected"


# ── 非法状态迁移 ─────────────────────────────────────────
def test_draft_to_trial_illegal(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    with pytest.raises(ProposalError, match="非法状态迁移"):
        update_proposal(tmp_path, proposal["id"], {"status": "trial"})


def test_draft_to_verified_illegal(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    with pytest.raises(ProposalError, match="非法状态迁移"):
        update_proposal(tmp_path, proposal["id"], {"status": "verified"})


def test_approved_to_verified_illegal(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    with pytest.raises(ProposalError, match="非法状态迁移"):
        update_proposal(tmp_path, proposal["id"], {"status": "verified"})


def test_rejected_is_terminal(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "rejected"})
    with pytest.raises(ProposalError, match="非法状态迁移"):
        update_proposal(tmp_path, proposal["id"], {"status": "approved"})


def test_verified_is_terminal(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    update_proposal(tmp_path, proposal["id"], {"status": "approved"})
    update_proposal(tmp_path, proposal["id"], {"status": "trial"})
    update_proposal(tmp_path, proposal["id"], {"status": "verified"})
    with pytest.raises(ProposalError, match="非法状态迁移"):
        update_proposal(tmp_path, proposal["id"], {"status": "rejected"})


# ── history 记录 ─────────────────────────────────────────
def test_transition_records_history(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    updated = update_proposal(tmp_path, proposal["id"], {"status": "approved", "note": "证据充分"})
    assert len(updated["history"]) == 1
    assert updated["history"][0]["from"] == "draft"
    assert updated["history"][0]["to"] == "approved"
    assert updated["history"][0]["note"] == "证据充分"


# ── 字段更新 ─────────────────────────────────────────────
def test_patch_updates_fields(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    updated = update_proposal(tmp_path, proposal["id"], {"title": "新标题", "sampleSize": 20})
    assert updated["title"] == "新标题"
    assert updated["sampleSize"] == 20
    assert updated["status"] == "draft"  # 未传 status 不迁移


def test_patch_falsifier_empty_rejected(tmp_path):
    proposal = create_proposal(tmp_path, _valid_payload())
    with pytest.raises(ProposalError, match="falsifier"):
        update_proposal(tmp_path, proposal["id"], {"falsifier": ""})


# ── CRUD ─────────────────────────────────────────────────
def test_get_proposal_not_found(tmp_path):
    assert get_proposal(tmp_path, "nonexistent") is None


def test_update_proposal_not_found(tmp_path):
    with pytest.raises(ProposalError, match="不存在"):
        update_proposal(tmp_path, "nonexistent", {"status": "approved"})


def test_list_proposals_filtered_by_status(tmp_path):
    p1 = create_proposal(tmp_path, _valid_payload())
    p2 = create_proposal(tmp_path, _valid_payload(falsifier="反证2"))
    update_proposal(tmp_path, p1["id"], {"status": "approved"})
    approved = list_proposals(tmp_path, status="approved")
    assert len(approved) == 1
    assert approved[0]["id"] == p1["id"]
    drafts = list_proposals(tmp_path, status="draft")
    assert len(drafts) == 1
    assert drafts[0]["id"] == p2["id"]


# ── autopsy prompt 构建与解析 ────────────────────────────
def test_build_autopsy_prompt_contains_red_flags():
    from app.services.trading.autopsy import build_autopsy_prompt

    trade = {
        "symbol": "600519.SH", "name": "茅台", "strategy": "趋势",
        "status": "已平仓", "position": {"qty": 0, "costPrice": 1680, "invested": 168000},
        "realizedPnl": -5000, "stopLoss": 1600,
        "thesis": {"text": "突破前高", "invalidation": "跌破1600"},
    }
    events = [{"kind": "fill", "ts": TS, "payload": {"qty": 100, "price": 1680}}]
    red_flags = [{"type": "stop_loss_widened", "ts": TS, "old": 1600, "new": 1500, "costPrice": 1680}]
    messages = build_autopsy_prompt(trade, events, red_flags, deviation="计划全仓实际半仓")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "四分类" in messages[0]["content"]
    user = messages[1]["content"]
    assert "茅台" in user
    assert "stop_loss_widened" in user
    assert "计划全仓实际半仓" in user


def test_structured_autopsy_accepts_classification_b():
    from app.services.trading.autopsy import _AutopsyOutput

    result = _AutopsyOutput.model_validate(
        {
            "tradeId": "trade-1",
            "classification": "B",
            "reasoning": "放宽止损导致亏损扩大，模式12",
            "fix": "硬性锁定止损",
            "patternIds": [12],
        }
    )
    assert result.classification == "B"
    assert result.pattern_ids == [12]


def test_structured_autopsy_rejects_invalid_default_a():
    from pydantic import ValidationError
    from app.services.trading.autopsy import _AutopsyOutput

    with pytest.raises(ValidationError):
        _AutopsyOutput.model_validate(
            {
                "tradeId": "trade-1",
                "classification": "A",
                "reasoning": "无",
                "fix": "修改策略",
                "patternIds": [],
            }
        )


# ── P6.1: 提案放宽标记(is_relaxation 纯函数) ─────────────
def test_is_relaxation_limit_increase():
    # after 的 limit 数值上调 → True
    assert is_relaxation({"stopLossPct": 0.05}, {"stopLossPct": 0.08}) is True


def test_is_relaxation_budget_increase():
    assert is_relaxation({"lossBudgetPct": 5}, {"lossBudgetPct": 8}) is True


def test_is_relaxation_invalidation_reduced():
    # 失效信号条目减少 → True
    before = {"invalidation": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    after = {"invalidation": [{"name": "a"}]}
    assert is_relaxation(before, after) is True


def test_is_relaxation_min_decrease():
    # 下限下调(min/floor 数值变小) → True(下限放宽)
    assert is_relaxation({"minHolding": 10}, {"minHolding": 5}) is True


def test_is_relaxation_not_relaxation():
    # 收紧: after 数值更小 → False
    assert is_relaxation({"stopLossPct": 0.08}, {"stopLossPct": 0.05}) is False


def test_is_relaxation_same_values():
    assert is_relaxation({"stopLossPct": 0.05}, {"stopLossPct": 0.05}) is False


def test_is_relaxation_invalidation_same_count():
    before = {"invalidation": [{"name": "a"}, {"name": "b"}]}
    after = {"invalidation": [{"name": "x"}, {"name": "y"}]}
    assert is_relaxation(before, after) is False


def test_is_relaxation_empty_dicts():
    assert is_relaxation({}, {}) is False
    assert is_relaxation(None, None) is False
    assert is_relaxation(None, {"a": 1}) is False


def test_is_relaxation_invalidation_added():
    # after 比 before 多失效信号 → 不是放宽(更严格)
    before = {"invalidation": [{"name": "a"}]}
    after = {"invalidation": [{"name": "a"}, {"name": "b"}]}
    assert is_relaxation(before, after) is False


# ── has_recent_loss 纯函数 ───────────────────────────────
def test_has_recent_loss_true():
    now = datetime(2026, 8, 4)
    trades = [{"status": "已平仓", "realizedPnl": -5000, "closedAt": "2026-07-20 14:30"}]
    assert has_recent_loss(trades, now=now) is True


def test_has_recent_loss_outside_window():
    now = datetime(2026, 8, 4)
    trades = [{"status": "已平仓", "realizedPnl": -5000, "closedAt": "2026-06-01 14:30"}]
    assert has_recent_loss(trades, now=now) is False


def test_has_recent_loss_profit_not_counted():
    now = datetime(2026, 8, 4)
    trades = [{"status": "已平仓", "realizedPnl": 5000, "closedAt": "2026-07-20 14:30"}]
    assert has_recent_loss(trades, now=now) is False


def test_has_recent_loss_open_position_not_counted():
    now = datetime(2026, 8, 4)
    trades = [{"status": "持仓中", "realizedPnl": -5000, "closedAt": None}]
    assert has_recent_loss(trades, now=now) is False


def test_has_recent_loss_no_closedat():
    now = datetime(2026, 8, 4)
    trades = [{"status": "已平仓", "realizedPnl": -5000, "closedAt": None}]
    assert has_recent_loss(trades, now=now) is False


def test_has_recent_loss_empty():
    assert has_recent_loss([]) is False


# ── compute_relaxation_after_loss 组合判定 ───────────────
def test_compute_relaxation_after_loss_true(tmp_path):
    from app.services.trading.lifecycle import apply_event, new_trade
    from app.services.trading.models import KIND_FILL

    # 构造一笔近 30 天的亏损平仓
    now = datetime.now()
    close_ts = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    trade = new_trade("t1", "600519.SH",
                      {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"}, "stopLoss": 1600.0}, close_ts)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, close_ts)
    trade["status"] = "已平仓"
    trade["realizedPnl"] = -5000
    trade["closedAt"] = close_ts
    store.write_trade(tmp_path, trade)

    result = compute_relaxation_after_loss(tmp_path, {"lossBudgetPct": 5}, {"lossBudgetPct": 8})
    assert result is True


def test_compute_relaxation_after_loss_not_relaxation(tmp_path):
    # 非放宽 → False(不查 trades)
    result = compute_relaxation_after_loss(tmp_path, {"stopLossPct": 0.08}, {"stopLossPct": 0.05})
    assert result is False


def test_compute_relaxation_after_loss_no_recent_loss(tmp_path):
    # 放宽但无近 30 天亏损 → False
    result = compute_relaxation_after_loss(tmp_path, {"lossBudgetPct": 5}, {"lossBudgetPct": 8})
    assert result is False


# ── create_proposal 落盘 relaxationAfterLoss ─────────────
def test_create_proposal_marks_relaxation_after_loss(tmp_path):
    from app.services.trading.lifecycle import apply_event, new_trade
    from app.services.trading.models import KIND_FILL

    # 构造近 30 天亏损平仓
    now = datetime.now()
    close_ts = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    trade = new_trade("t1", "600519.SH",
                      {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"}, "stopLoss": 1600.0}, close_ts)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, close_ts)
    trade["status"] = "已平仓"
    trade["realizedPnl"] = -3000
    trade["closedAt"] = close_ts
    store.write_trade(tmp_path, trade)

    proposal = create_proposal(tmp_path, _valid_payload(
        before={"lossBudgetPct": 5},
        after={"lossBudgetPct": 8},
    ))
    assert proposal["relaxationAfterLoss"] is True


def test_create_proposal_no_relaxation_after_loss(tmp_path):
    # 非放宽 → relaxationAfterLoss False
    proposal = create_proposal(tmp_path, _valid_payload(
        before={"stopLossPct": 0.08},
        after={"stopLossPct": 0.05},
    ))
    assert proposal["relaxationAfterLoss"] is False


# ── autopsy rubric:12 模式清单 ──────────────────────────
def test_build_autopsy_prompt_contains_12_patterns():
    from app.services.trading.autopsy import build_autopsy_prompt

    trade = {"symbol": "600519.SH", "name": "茅台", "strategy": "趋势",
             "status": "已平仓", "position": {"qty": 0, "costPrice": 1680, "invested": 168000},
             "realizedPnl": -5000, "stopLoss": 1600,
             "thesis": {"text": "突破前高", "invalidation": "跌破1600"}}
    messages = build_autopsy_prompt(trade, [], [], deviation=None)
    system = messages[0]["content"]
    assert "12 种不一致模式" in system
    # 验证压缩清单包含关键模式
    assert "裁判切换" in system
    assert "期限漂移" in system
    assert "仓位代替信念" in system
    assert "亏损后放松规则" in system
    # 验证要求引用模式编号
    assert "引用命中模式编号" in system
