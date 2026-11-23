"""策略守护纯逻辑测试: 判定状态机、退化计算与窗口规划。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.guardian import (
    GuardianThresholds,
    choose_k,
    consecutive_watch,
    degradation_pct,
    evaluate_check,
    objective_soft_floor,
    plan_new_data_window,
    split_windows,
)
from app.backtest.robust_optimizer import (
    split_validation_windows as robust_split_validation_windows,
)


class TestThresholds:
    def test_defaults_valid(self):
        t = GuardianThresholds()
        assert t.watch_pct == 0.2 and t.retire_pct == 0.5

    def test_watch_must_be_below_retire(self):
        with pytest.raises(ValueError, match="watch_pct"):
            GuardianThresholds(watch_pct=0.5, retire_pct=0.5)
        with pytest.raises(ValueError, match="watch_pct"):
            GuardianThresholds(watch_pct=0.6, retire_pct=0.5)

    def test_k_bounds(self):
        with pytest.raises(ValueError):
            GuardianThresholds(k_max=5)
        with pytest.raises(ValueError):
            GuardianThresholds(k_max=1)


class TestDegradation:
    def test_improvement_is_zero(self):
        assert degradation_pct(2.0, 3.0) == 0.0

    def test_ratio(self):
        assert degradation_pct(2.0, 1.0) == pytest.approx(0.5)
        assert degradation_pct(2.0, 1.7) == pytest.approx(0.15)

    def test_nonpositive_baseline_returns_none(self):
        assert degradation_pct(0.0, -1.0) is None
        assert degradation_pct(-0.5, -2.0) is None


class TestSoftFloor:
    def test_whitelist_enforced(self):
        with pytest.raises(ValueError, match="白名单"):
            objective_soft_floor("win_rate")
        assert objective_soft_floor("sharpe") == 0.0
        assert objective_soft_floor("max_drawdown") == -0.30
        assert objective_soft_floor("mc_maxdd_p95") == -0.30


class TestEvaluateCheck:
    def _run(self, **overrides):
        params = dict(
            objective="sharpe",
            baseline_score=2.0,
            baseline_n_trades=300,
            current_score=1.9,
            current_n_trades=100,
            worst_drawdown=-0.10,
            thresholds=GuardianThresholds(),
        )
        params.update(overrides)
        return evaluate_check(**params)

    def test_healthy(self):
        verdict = self._run()
        assert verdict.status == "healthy"
        assert verdict.reasons == ()
        assert verdict.degradation_pct == pytest.approx(0.05)

    def test_watch_on_moderate_degradation(self):
        verdict = self._run(current_score=1.3)  # 35% 退化
        assert verdict.status == "watch"
        assert any("退化" in reason for reason in verdict.reasons)

    def test_retired_on_severe_degradation(self):
        verdict = self._run(current_score=0.4)  # 80% 退化
        assert verdict.status == "retired"

    def test_boundary_exactly_retire_pct_is_watch(self):
        verdict = self._run(current_score=1.0)  # 恰 50%, 未 "超过"
        assert verdict.status == "watch"

    def test_retired_on_trade_drought(self):
        verdict = self._run(current_n_trades=5)  # 硬底线 max(3, 300*0.1)=30
        assert verdict.status == "retired"
        assert any("枯竭" in reason for reason in verdict.reasons)

    def test_watch_on_soft_floor_sharpe(self):
        # 基线 ≤0 时无相对退化 (pct=None), 软底线是唯一判据
        verdict = self._run(baseline_score=0.0, current_score=-0.2, current_n_trades=150)
        assert verdict.status == "watch"
        assert verdict.degradation_pct is None
        assert any("软底线" in reason for reason in verdict.reasons)

    def test_watch_on_drawdown_floor(self):
        verdict = self._run(worst_drawdown=-0.45)
        assert verdict.status == "watch"
        assert any("回撤" in reason for reason in verdict.reasons)

    def test_watch_on_third_trades_floor(self):
        verdict = self._run(current_n_trades=80)  # < 300/3=100, ≥ 硬底线 30
        assert verdict.status == "watch"
        assert any("1/3" in reason for reason in verdict.reasons)

    def test_incomplete_data_caps_at_watch(self):
        verdict = self._run(current_score=-10.0, incomplete_data=True)
        assert verdict.status == "watch"
        assert verdict.incomplete_data is True

    def test_incomplete_data_drought_caps_at_watch(self):
        verdict = self._run(current_n_trades=0, incomplete_data=True)
        assert verdict.status == "watch"

    def test_missing_segments_force_watch(self):
        verdict = self._run(missing_segments=1)
        assert verdict.status == "watch"
        assert any("验证段回测失败" in reason for reason in verdict.reasons)

    def test_drawdown_objective_floor_uses_drawdown_scale(self):
        verdict = evaluate_check(
            objective="max_drawdown",
            baseline_score=-0.10,
            baseline_n_trades=100,
            current_score=-0.12,  # 退化 (0.10-(-0.12)... 基线<0 → pct=None)
            current_n_trades=90,
            worst_drawdown=-0.12,
            thresholds=GuardianThresholds(),
        )
        # 基线 -0.10 ≤ 0 → 无相对退化; -0.12 未击穿 -0.30 → 健康
        assert verdict.status == "healthy"
        assert verdict.degradation_pct is None

    def test_nonpositive_baseline_uses_floors_only(self):
        verdict = self._run(baseline_score=0.0, current_score=-0.5)
        assert verdict.degradation_pct is None
        assert verdict.status == "watch"


class TestWindowPlanning:
    def _dates(self, count: int, first: date = date(2026, 1, 1)) -> list[date]:
        return [first + timedelta(days=i) for i in range(count)]

    def test_insufficient_new_data_skips(self):
        assert plan_new_data_window(self._dates(29), date(2025, 12, 31)) is None

    def test_sufficient_new_data(self):
        dates = self._dates(40)
        window = plan_new_data_window(dates, date(2025, 12, 31))
        assert window == (dates[0], dates[-1])

    def test_as_of_date_excluded(self):
        dates = self._dates(40)
        # 基线当日及之前都不算新数据: 排除前 11 个 → 只剩 29 个 < 30
        window = plan_new_data_window(dates, dates[10])
        assert window is None

    def test_choose_k_prefers_max(self):
        start, end = date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=199)
        assert choose_k(start, end, 4) == 4

    def test_choose_k_falls_back(self):
        start, end = date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=39)
        assert choose_k(start, end, 4) == 2

    def test_choose_k_too_short_raises(self):
        start, end = date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=26)
        with pytest.raises(ValueError):
            choose_k(start, end, 2)

    def test_split_windows_matches_robust_optimizer(self):
        start, end = date(2026, 1, 1), date(2026, 4, 30)
        assert split_windows(start, end, 3) == robust_split_validation_windows(start, end, 3)
        assert split_windows(start, end, 4) == robust_split_validation_windows(start, end, 4)


class TestConsecutiveWatch:
    def test_counts_trailing_watch(self):
        checks = [{"verdict": "healthy"}, {"verdict": "watch"}, {"verdict": "watch"}]
        assert consecutive_watch(checks) == 2

    def test_stops_at_error(self):
        checks = [
            {"verdict": "watch"},
            {"verdict": "watch", "error": "boom"},
            {"verdict": "watch"},
        ]
        assert consecutive_watch(checks) == 1

    def test_empty(self):
        assert consecutive_watch([]) == 0


class TestGateWorstFold:
    """P2a: strict 档 (confidence=high) 的 worst-fold 稳健门槛。"""

    def _gate(self, **overrides):
        from app.backtest.mining import evaluate_candidate_gate

        params = dict(
            confidence="high",
            valid_folds=3,
            positive_fold_ratio=1.0,
            sharpe=1.2,
            max_drawdown=-0.1,
            n_trades=100,
            worst_fold_sharpe=0.3,
        )
        params.update(overrides)
        return evaluate_candidate_gate(**params)

    def test_strict_positive_worst_passes(self):
        assert self._gate().qualified

    def test_strict_nonpositive_worst_rejected(self):
        result = self._gate(worst_fold_sharpe=0.0)
        assert not result.qualified
        assert any("worst-fold" in r for r in result.reasons)
        assert not self._gate(worst_fold_sharpe=-0.5).qualified

    def test_legacy_artifact_without_worst_passes(self):
        assert self._gate(worst_fold_sharpe=None).qualified

    def test_balanced_unaffected_by_worst(self):
        assert self._gate(confidence="standard", worst_fold_sharpe=-2.0).qualified
