"""成本敏感性分析 (A2) 测试 — fake run_fn 验证倍数作用与 fail-closed 契约。

关键契约:
- 倍数同时作用于 fees_pct 与 slippage_bps, 0.0 为零成本;
- 倍数规整: 去重、含 1.0 基线 (缺失自动补)、升序; 负数/非有限拒绝;
- is_baseline 仅在 m==1.0 为 True;
- 原 cfg 不被修改, 每档收到独立副本;
- run_fn 异常整体透传, 不静默丢行;
- stats 缺失/None/NaN 指标 → 行内 null, 不伪造 0;
- dataclass 与 dict 两种结果形态均适配。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pytest

from app.backtest import cost_sensitivity as cs
from app.backtest.strategy import StrategyBacktestConfig


# ── 桩: 记录每次收到的费用/滑点, 返回固定 stats ────────────


@dataclass
class _Result:
    """dataclass 形态的回测结果 (同 StrategyBacktestResult 的 stats 属性)。"""
    stats: dict = field(default_factory=dict)


def _base_stats(multiplier: float) -> dict:
    """固定 stats: 成本随倍数变化, 其余指标固定 (含引擎真实键名 annual_return)。"""
    return {
        "total_return": 0.25,
        "annual_return": 0.10,
        "sharpe": 1.5,
        "max_drawdown": -0.12,
        "final_equity": 1_250_000.0,
        "n_trades": 42,
        "cost_breakdown": {
            "gross_notional": 8_000_000.0,
            "commission": 1_600.0 * multiplier,
            "slippage": 4_000.0 * multiplier,
            "total": 5_600.0 * multiplier,
        },
    }


class _RecordingRun:
    """fake run_fn: 记录每档收到的 (fees, slippage) 与配置对象。"""

    def __init__(self, stats_factory=_base_stats):
        self.calls: list[tuple[float, float]] = []
        self.cfgs: list[StrategyBacktestConfig] = []
        self._stats_factory = stats_factory

    def __call__(self, cfg: StrategyBacktestConfig):
        self.calls.append((float(cfg.fees_pct), float(cfg.slippage_bps)))
        self.cfgs.append(cfg)
        # run_fn 只收到 cfg; 由 fees 相对基准的比值还原倍数传给 stats 工厂。
        multiplier = cfg.fees_pct / 0.0002 if cfg.fees_pct else 0.0
        return _Result(self._stats_factory(multiplier))


def _base_cfg() -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id="test-strategy",
        symbols=["000001.SZ"],
        start=date(2024, 1, 1),
        end=date(2024, 6, 30),
        fees_pct=0.0002,
        slippage_bps=5.0,
    )


# ── 倍数作用与基线标记 ─────────────────────────────────────


def test_default_multipliers_applied_to_fees_and_slippage():
    run = _RecordingRun()
    out = cs.run_cost_sensitivity(run, _base_cfg())
    assert out["multipliers"] == [0.0, 0.5, 1.0, 2.0, 5.0]
    assert len(out["rows"]) == 5
    # 倍数同时作用于费用与滑点; 0.0 档两项归零 (零成本)。
    assert run.calls == [(0.0, 0.0), (0.0001, 2.5), (0.0002, 5.0), (0.0004, 10.0), (0.001, 25.0)]
    for row, m in zip(out["rows"], out["multipliers"]):
        assert row["multiplier"] == m
        assert math.isclose(row["fees_pct"], 0.0002 * m)
        assert math.isclose(row["slippage_bps"], 5.0 * m)
        assert row["is_baseline"] is (m == 1.0)
        assert row["total_return"] == 0.25
        assert row["annualized_return"] == 0.10
        assert row["sharpe"] == 1.5
        assert row["max_drawdown"] == -0.12
        assert row["final_equity"] == 1_250_000.0
        assert row["n_trades"] == 42
        assert math.isclose(row["total_cost"], 5_600.0 * m)


def test_exactly_one_baseline_row():
    out = cs.run_cost_sensitivity(_RecordingRun(), _base_cfg(), multipliers=(0.0, 1.0, 3.0, 1.0))
    baselines = [r for r in out["rows"] if r["is_baseline"]]
    assert len(baselines) == 1
    assert baselines[0]["multiplier"] == 1.0
    assert out["multipliers"] == [0.0, 1.0, 3.0]  # 重复的 1.0 已去重


# ── 倍数校验 ───────────────────────────────────────────────


def test_negative_multiplier_rejected():
    with pytest.raises(ValueError):
        cs.run_cost_sensitivity(_RecordingRun(), _base_cfg(), multipliers=(0.5, -1.0))


def test_non_finite_multiplier_rejected():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            cs.run_cost_sensitivity(_RecordingRun(), _base_cfg(), multipliers=(1.0, bad))


def test_non_numeric_multiplier_rejected():
    with pytest.raises(ValueError):
        cs.run_cost_sensitivity(_RecordingRun(), _base_cfg(), multipliers=(1.0, "cheap"))


def test_baseline_auto_appended_and_sorted_when_missing():
    run = _RecordingRun()
    out = cs.run_cost_sensitivity(run, _base_cfg(), multipliers=(3.0, 0.0))
    assert out["multipliers"] == [0.0, 1.0, 3.0]  # 自动补 1.0 并升序
    assert [r["multiplier"] for r in out["rows"] if r["is_baseline"]] == [1.0]
    assert len(run.calls) == 3


def test_bad_cfg_type_rejected():
    with pytest.raises(TypeError):
        cs.run_cost_sensitivity(_RecordingRun(), object())  # type: ignore[arg-type]


# ── 配置隔离 ───────────────────────────────────────────────


def test_original_cfg_untouched_and_scenarios_are_distinct_copies():
    cfg = _base_cfg()
    run = _RecordingRun()
    cs.run_cost_sensitivity(run, cfg, multipliers=(0.0, 2.0))
    assert cfg.fees_pct == 0.0002 and cfg.slippage_bps == 5.0  # 原 cfg 不被修改
    assert len({id(c) for c in run.cfgs}) == 3  # 0.0/2.0 自动补 1.0 基线 → 3 档独立副本
    assert all(c is not cfg for c in run.cfgs)


# ── fail-closed: 异常透传与 null 处理 ─────────────────────


def test_run_fn_exception_propagates_without_partial_rows():
    def boom(cfg):
        if cfg.fees_pct == 0.0:
            raise RuntimeError("engine exploded")
        return _Result(_base_stats(1.0))

    with pytest.raises(RuntimeError, match="engine exploded"):
        cs.run_cost_sensitivity(boom, _base_cfg(), multipliers=(0.0, 1.0))


def test_missing_none_and_nan_stats_become_null():
    def sparse_stats(fees):
        return {
            "total_return": float("nan"),
            "sharpe": None,
            "max_drawdown": float("inf"),
            "n_trades": "not-a-number",
            # 无 annual_return / final_equity / cost_breakdown
        }

    out = cs.run_cost_sensitivity(
        _RecordingRun(sparse_stats), _base_cfg(), multipliers=(1.0,)
    )
    row = out["rows"][0]
    assert row["is_baseline"] is True
    # 配置侧字段照常, 指标侧全部 null, 不伪造 0。
    assert row["fees_pct"] == 0.0002 and row["slippage_bps"] == 5.0
    assert row["total_return"] is None
    assert row["annualized_return"] is None
    assert row["sharpe"] is None
    assert row["max_drawdown"] is None
    assert row["final_equity"] is None
    assert row["total_cost"] is None
    assert row["n_trades"] is None


def test_non_finite_total_cost_becomes_null():
    def weird_breakdown(fees):
        return {
            "total_return": 0.1,
            "cost_breakdown": {"total": float("nan")},
        }

    out = cs.run_cost_sensitivity(_RecordingRun(weird_breakdown), _base_cfg(), multipliers=(1.0,))
    assert out["rows"][0]["total_cost"] is None
    assert out["rows"][0]["total_return"] == 0.1


def test_no_stats_at_all_yields_all_null_metrics():
    class _Bare:
        pass

    out = cs.run_cost_sensitivity(lambda cfg: _Bare(), _base_cfg(), multipliers=(1.0,))
    row = out["rows"][0]
    assert row["multiplier"] == 1.0 and row["is_baseline"] is True
    assert all(row[k] is None for k in (
        "total_return", "annualized_return", "sharpe", "max_drawdown",
        "final_equity", "total_cost", "n_trades",
    ))


def test_dict_shaped_result_supported():
    def dict_run(cfg):
        return {"stats": _base_stats(cfg.fees_pct / 0.0002 if cfg.fees_pct else 0.0)}

    out = cs.run_cost_sensitivity(dict_run, _base_cfg(), multipliers=(1.0,))
    row = out["rows"][0]
    assert row["total_return"] == 0.25
    assert row["total_cost"] == 5_600.0


# ── 输出结构 ───────────────────────────────────────────────


def test_output_shape_and_note():
    out = cs.run_cost_sensitivity(_RecordingRun(), _base_cfg(), multipliers=(2.0,))
    assert set(out) == {"multipliers", "rows", "note"}
    assert out["note"] == cs.COST_SENSITIVITY_NOTE
    assert "成本倍数" in out["note"]
    row_keys = {
        "multiplier", "fees_pct", "slippage_bps", "is_baseline", "total_return",
        "annualized_return", "sharpe", "max_drawdown", "final_equity", "total_cost", "n_trades",
    }
    for row in out["rows"]:
        assert set(row) == row_keys
