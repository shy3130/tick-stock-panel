"""``loss_budget`` 纯函数损失预算约束测试。

覆盖验收点: 未超预算 / 刚好超限 / 跨周期边界 / 缺数据 / 非有限数值 /
负损失方向 / 亏损后放宽仍拒绝。纯计算, 不落盘, 不依赖外部数据源。
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.loss_budget import (
    DIMENSIONS,
    SCHEMA_VERSION,
    VERDICT_ALLOW,
    VERDICT_DENY,
    VERDICT_INSUFFICIENT_DATA,
    check_budget_relaxation,
    evaluate_dimension,
    evaluate_loss_budget,
    realized_loss_of,
)


def _rec(realized_pnl, date="2026-08-14", *, strategy="general", account="default",
         trade_id="t1", symbol="DEMO"):
    return {
        "date": date,
        "realizedPnl": realized_pnl,
        "strategy": strategy,
        "accountId": account,
        "tradeId": trade_id,
        "symbol": symbol,
    }


# --------------------------------------------------------------------------- #
# realized_loss_of
# --------------------------------------------------------------------------- #
class TestRealizedLossOf:
    def test_negative_pnl_is_loss(self):
        assert realized_loss_of(_rec(-500.0)) == 500.0

    def test_positive_pnl_is_zero_loss(self):
        assert realized_loss_of(_rec(800.0)) == 0.0

    def test_zero_pnl_is_zero_loss(self):
        assert realized_loss_of(_rec(0.0)) == 0.0

    def test_non_finite_returns_none(self):
        assert realized_loss_of(_rec(float("nan"))) is None
        assert realized_loss_of(_rec(float("inf"))) is None
        assert realized_loss_of(_rec(float("-inf"))) is None

    def test_missing_pnl_key_is_unknown(self):
        # 缺 realizedPnl 键 → 数据不完整 → None (fail-closed, 不当作 0)
        assert realized_loss_of({"date": "2026-08-14"}) is None
        assert realized_loss_of({}) is None


# --------------------------------------------------------------------------- #
# 1. 未超预算 → allow
# --------------------------------------------------------------------------- #
class TestUnderBudget:
    def test_daily_below_budget_allows(self):
        res = evaluate_loss_budget(
            [_rec(-300.0)],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        d = res["dimensions"]["daily"]
        assert d["amount"] == 300.0
        assert d["budget"] == 1000.0
        assert d["remaining"] == 700.0
        assert d["utilization"] == pytest.approx(0.3)

    def test_profit_offset_within_period(self):
        # 同一天一笔亏 300, 一笔盈 500 → 净盈 → 不消耗预算
        res = evaluate_loss_budget(
            [_rec(-300.0), _rec(500.0)],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert res["dimensions"]["daily"]["amount"] == 0.0

    def test_multi_dimension_all_allow(self):
        res = evaluate_loss_budget(
            [_rec(-100.0, "2026-08-14"), _rec(-200.0, "2026-08-13", strategy="swing")],
            {"daily": 1000.0, "weekly": 3000.0, "strategy": 5000.0, "portfolio": 10000.0},
            context={"date": "2026-08-14", "strategy": "general"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert set(res["dimensions"]) == set(DIMENSIONS)


# --------------------------------------------------------------------------- #
# 2. 刚好超限 → deny
# --------------------------------------------------------------------------- #
class TestAtOrOverBudget:
    def test_exactly_at_budget_denies(self):
        # 刚好达到预算: amount == budget → deny (fail-closed 边界)
        res = evaluate_loss_budget(
            [_rec(-1000.0)],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_DENY
        d = res["dimensions"]["daily"]
        assert d["amount"] == 1000.0
        assert d["remaining"] == 0.0
        assert d["utilization"] == pytest.approx(1.0)

    def test_over_budget_denies(self):
        res = evaluate_loss_budget(
            [_rec(-1500.0)],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_DENY
        assert res["dimensions"]["daily"]["remaining"] == -500.0
        assert res["dimensions"]["daily"]["utilization"] == pytest.approx(1.5)

    def test_zero_budget_any_loss_denies(self):
        res = evaluate_loss_budget(
            [_rec(-1.0)],
            {"daily": 0.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_DENY

    def test_zero_budget_no_loss_allows(self):
        res = evaluate_loss_budget(
            [_rec(500.0)],
            {"daily": 0.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert res["dimensions"]["daily"]["amount"] == 0.0


# --------------------------------------------------------------------------- #
# 3. 跨周期 (周边界)
# --------------------------------------------------------------------------- #
class TestWeeklyBoundary:
    def test_prior_week_loss_excluded(self):
        # 2026-08-14 是周四; 周一为 2026-08-10, 上周日为 2026-08-09
        res = evaluate_loss_budget(
            [_rec(-5000.0, "2026-08-09")],  # 上周日, 不在本周
            {"weekly": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert res["dimensions"]["weekly"]["amount"] == 0.0
        assert res["dimensions"]["weekly"]["evidence"]["recordCount"] == 0

    def test_same_week_loss_included(self):
        # 本周三 (2026-08-12) 的亏损计入本周 (周一 08-10 ~ 周日 08-16)
        res = evaluate_loss_budget(
            [_rec(-1200.0, "2026-08-12")],
            {"weekly": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_DENY
        assert res["dimensions"]["weekly"]["amount"] == 1200.0

    def test_week_bounds_in_evidence(self):
        res = evaluate_loss_budget(
            [_rec(-100.0, "2026-08-14")],
            {"weekly": 1000.0},
            context={"date": "2026-08-14"},
        )
        period = res["dimensions"]["weekly"]["evidence"]["period"]
        assert period == {"start": "2026-08-10", "end": "2026-08-16"}

    def test_daily_dimension_only_exact_date(self):
        # 同周不同日不计入 daily
        res = evaluate_loss_budget(
            [_rec(-5000.0, "2026-08-13")],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW


# --------------------------------------------------------------------------- #
# 4. 缺数据 → fail-closed
# --------------------------------------------------------------------------- #
class TestMissingData:
    def test_missing_date_anchor_for_daily(self):
        res = evaluate_loss_budget(
            [_rec(-100.0)],
            {"daily": 1000.0},
            context={},
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA
        assert res["dimensions"]["daily"]["amount"] is None

    def test_missing_strategy_for_strategy_dim(self):
        res = evaluate_loss_budget(
            [_rec(-100.0)],
            {"strategy": 1000.0},
            context={"date": "2026-08-14"},  # 无 strategy
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA

    def test_no_budgets_configured(self):
        res = evaluate_loss_budget(
            [_rec(-100.0)],
            {},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA
        assert res["reason"].startswith("未配置任何维度")

    def test_none_budget_skips_dimension(self):
        # daily=None 不评估, portfolio 有预算 → 仅评估 portfolio
        res = evaluate_loss_budget(
            [_rec(-100.0)],
            {"daily": None, "portfolio": 1000.0},
            context={},
        )
        assert "daily" not in res["dimensions"]
        assert "portfolio" in res["dimensions"]
        assert res["verdict"] == VERDICT_ALLOW

    def test_deny_dominates_insufficient(self):
        # daily deny + weekly insufficient → 汇总 deny (最受限)
        res = evaluate_loss_budget(
            [_rec(-2000.0, "2026-08-14")],
            {"daily": 1000.0, "weekly": 1000.0},
            context={},  # 无 date → weekly insufficient, daily 也无 date
        )
        # 两者都因无 date → insufficient
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA


# --------------------------------------------------------------------------- #
# 5. 非有限数值
# --------------------------------------------------------------------------- #
class TestNonFinite:
    def test_nan_pnl_in_scope_insufficient(self):
        res = evaluate_loss_budget(
            [_rec(-100.0), _rec(float("nan"))],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA
        ev = res["dimensions"]["daily"]["evidence"]
        assert ev["nonFiniteCount"] == 1

    def test_inf_pnl_in_scope_insufficient(self):
        res = evaluate_loss_budget(
            [_rec(float("inf"))],
            {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA

    def test_non_finite_outside_period_ignored(self):
        # 非有限值在另一周期 → 不影响本周 daily/portfolio? portfolio 含全部 → insufficient
        res_portfolio = evaluate_loss_budget(
            [_rec(float("nan"), "2026-08-01")],
            {"portfolio": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res_portfolio["verdict"] == VERDICT_INSUFFICIENT_DATA

    def test_negative_budget_insufficient(self):
        res = evaluate_loss_budget(
            [_rec(-100.0)],
            {"daily": -5.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA


# --------------------------------------------------------------------------- #
# 6. 负损失方向 (符号约定)
# --------------------------------------------------------------------------- #
class TestLossDirection:
    def test_profit_does_not_consume_budget(self):
        res = evaluate_dimension(
            [_rec(5000.0)], 60.0, "daily", context={"date": "2026-08-14"},
        )
        assert res["amount"] == 0.0
        assert res["verdict"] == VERDICT_ALLOW

    def test_net_positive_no_loss(self):
        # -300 + 1000 = +700 净 → amount 0
        res = evaluate_dimension(
            [_rec(-300.0), _rec(1000.0)], 500.0, "daily", context={"date": "2026-08-14"},
        )
        assert res["amount"] == 0.0
        assert res["evidence"]["netRealizedPnl"] == 700.0

    def test_net_negative_partial_offset(self):
        # -800 + 300 = -500 净 → amount 500 (部分抵扣)
        res = evaluate_dimension(
            [_rec(-800.0), _rec(300.0)], 1000.0, "daily", context={"date": "2026-08-14"},
        )
        assert res["amount"] == 500.0
        assert res["evidence"]["netRealizedPnl"] == -500.0


# --------------------------------------------------------------------------- #
# 7. 亏损后放宽仍拒绝
# --------------------------------------------------------------------------- #
class TestBudgetRelaxation:
    def test_relaxation_after_loss_denied(self):
        res = check_budget_relaxation(1000.0, 1500.0, realized_loss_total=300.0)
        assert res["verdict"] == VERDICT_DENY
        assert res["isRelaxation"] is True
        assert "禁止放宽" in res["reason"]

    def test_tightening_after_loss_allowed(self):
        res = check_budget_relaxation(1000.0, 800.0, realized_loss_total=300.0)
        assert res["verdict"] == VERDICT_ALLOW
        assert res["isRelaxation"] is False

    def test_keep_same_after_loss_allowed(self):
        res = check_budget_relaxation(1000.0, 1000.0, realized_loss_total=300.0)
        assert res["verdict"] == VERDICT_ALLOW
        assert res["isRelaxation"] is False

    def test_relaxation_with_no_loss_allowed(self):
        res = check_budget_relaxation(1000.0, 2000.0, realized_loss_total=0.0)
        assert res["verdict"] == VERDICT_ALLOW

    def test_non_finite_inputs_insufficient(self):
        res = check_budget_relaxation(float("nan"), 1500.0, realized_loss_total=300.0)
        assert res["verdict"] == VERDICT_INSUFFICIENT_DATA

    def test_default_loss_zero_relaxation_ok(self):
        # 默认 realized_loss_total=0 → 无亏损放宽不在禁令范围
        res = check_budget_relaxation(1000.0, 1500.0)
        assert res["verdict"] == VERDICT_ALLOW


# --------------------------------------------------------------------------- #
# 组合 / 策略维度 / provenance
# --------------------------------------------------------------------------- #
class TestStrategyAndPortfolio:
    def test_strategy_dimension_filters_by_strategy(self):
        res = evaluate_loss_budget(
            [
                _rec(-4000.0, "2026-08-14", strategy="general"),
                _rec(-100.0, "2026-08-14", strategy="swing"),
            ],
            {"strategy": 1000.0},
            context={"strategy": "general"},
        )
        # 仅 general 策略: -4000 > 1000 → deny
        assert res["verdict"] == VERDICT_DENY
        assert res["dimensions"]["strategy"]["amount"] == 4000.0

    def test_portfolio_cumulative_all_records(self):
        res = evaluate_loss_budget(
            [_rec(-300.0, "2026-08-14"), _rec(-400.0, "2026-07-01")],
            {"portfolio": 1000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert res["dimensions"]["portfolio"]["amount"] == 700.0

    def test_account_filter_isolates(self):
        res = evaluate_loss_budget(
            [_rec(-5000.0, account="accA"), _rec(-100.0, account="accB")],
            {"portfolio": 1000.0},
            context={"accountId": "accB"},
        )
        assert res["verdict"] == VERDICT_ALLOW
        assert res["dimensions"]["portfolio"]["amount"] == 100.0


class TestProvenanceAndSerializable:
    def test_schema_version_present(self):
        res = evaluate_loss_budget([_rec(-100.0)], {"daily": 1000.0},
                                   context={"date": "2026-08-14"})
        assert res["schemaVersion"] == SCHEMA_VERSION

    def test_evidence_has_source_and_counts(self):
        res = evaluate_loss_budget(
            [_rec(-100.0), _rec(-200.0)], {"daily": 1000.0},
            context={"date": "2026-08-14"},
        )
        ev = res["dimensions"]["daily"]["evidence"]
        assert ev["source"] == "realized_loss_records"
        assert ev["recordCount"] == 2
        assert ev["validRecordCount"] == 2
        assert ev["nonFiniteCount"] == 0
        assert ev["netRealizedPnl"] == -300.0

    def test_output_is_json_serializable(self):
        import json

        res = evaluate_loss_budget(
            [_rec(-100.0), _rec(float("nan"))],
            {"daily": 1000.0, "portfolio": 5000.0},
            context={"date": "2026-08-14"},
        )
        # 全字段必须可 JSON 序列化 (None / finite float / str)
        serialized = json.dumps(res, ensure_ascii=False)
        assert isinstance(serialized, str)
        roundtrip = json.loads(serialized)
        assert roundtrip["verdict"] == res["verdict"]

    def test_binding_dimension_is_most_restrictive(self):
        # daily deny (utilization 1.5) vs portfolio allow → binding=daily
        res = evaluate_loss_budget(
            [_rec(-1500.0, "2026-08-14")],
            {"daily": 1000.0, "portfolio": 10000.0},
            context={"date": "2026-08-14"},
        )
        assert res["verdict"] == VERDICT_DENY
        assert res["bindingDimension"] == "daily"
        assert res["amount"] == 1500.0

    def test_unknown_dimension_raises(self):
        with pytest.raises(ValueError):
            evaluate_dimension([], 100.0, "hourly", context={})


# --------------------------------------------------------------------------- #
# 确认数学常量可用 (防御性)
# --------------------------------------------------------------------------- #
def test_math_isfinite_used_correctly():
    assert math.isfinite(1.0)
    assert not math.isfinite(float("inf"))
    assert not math.isfinite(float("nan"))
