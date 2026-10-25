"""稳健优化器测试 — 切窗 / 稳健分 / 冠军收敛诊断 / 编排 (假 service)。

被测 (设计见 docs/robust-optimizer-plan.md):
- split_validation_windows: 区间等分为互不重叠验证段
- robust_score: R = 0.6×median + 0.4×worst
- champion_convergence: 各段冠军是否收敛于同一组参数
- guards: 主目标白名单 + 最小交易数门槛 (fail-closed)
- RobustOptimizeService.robust_optimize: K 段回测 → 三层排序 → 取消
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.backtest import guards
from app.backtest.robust_optimizer import (
    RobustOptimizeConfig,
    RobustOptimizeService,
    champion_convergence,
    neighborhood_plateau,
    robust_score,
    split_validation_windows,
)

# ---------------------------------------------------------------
# 切窗
# ---------------------------------------------------------------

def test_windows_cover_span_contiguously():
    start, end = date(2024, 1, 1), date(2024, 12, 31)
    wins = split_validation_windows(start, end, 4)
    assert len(wins) == 4
    assert wins[0][0] == start and wins[-1][1] == end
    # 无缝衔接: 下一段起点 = 上一段终点 + 1 天 (不重叠不留缝)
    for i in range(len(wins) - 1):
        assert wins[i + 1][0] == wins[i][1] + timedelta(days=1)
    # 余数摊给前面的段: 前 span%k 段比后面长 1 天
    span = (end - start).days + 1
    base, rem = divmod(span, 4)
    lengths = [(e - s).days + 1 for s, e in wins]
    assert lengths == [base + (1 if i < rem else 0) for i in range(4)]


def test_windows_reject_k_out_of_range():
    start, end = date(2024, 1, 1), date(2024, 12, 31)
    with pytest.raises(ValueError, match=r"验证段数"):
        split_validation_windows(start, end, 1)
    with pytest.raises(ValueError, match=r"验证段数"):
        split_validation_windows(start, end, 7)


def test_windows_reject_short_span():
    # 40 天切 4 段 = 每段 10 天 < 14 天下限
    with pytest.raises(ValueError, match=r"区间太短"):
        split_validation_windows(date(2024, 1, 1), date(2024, 2, 9), 4)


# ---------------------------------------------------------------
# 稳健分
# ---------------------------------------------------------------

def test_robust_score_formula_odd_count():
    sc = robust_score([1.0, 5.0, 3.0])
    assert sc["median"] == 3.0
    assert sc["worst"] == 1.0
    assert sc["spread"] == 4.0
    assert sc["score"] == pytest.approx(0.6 * 3.0 + 0.4 * 1.0)


def test_robust_score_even_median():
    sc = robust_score([1.0, 2.0, 3.0, 4.0])
    assert sc["median"] == 2.5
    assert sc["score"] == pytest.approx(0.6 * 2.5 + 0.4 * 1.0)


def test_robust_score_all_none_is_honest_minus_inf():
    """段值全缺失 → 不伪造 0 分, 直接 -inf (垫底而非登顶)。"""
    sc = robust_score([None, None])
    assert sc["median"] is None and sc["worst"] is None
    assert sc["score"] == float("-inf")


def test_robust_score_partial_none_uses_finite_only():
    """个别段缺失: 中位/最差只在有限值上算, 缺失段不顶替最差 (已有段诚实呈现)。"""
    sc = robust_score([2.0, None, 4.0])
    assert sc["median"] == 3.0
    assert sc["worst"] == 2.0


def test_robust_score_penalizes_one_hot_segment():
    """两组中位相同, 但一组靠单段运气 → 最差段更低, 稳健分更低。"""
    steady = robust_score([3.0, 3.0, 3.0, 3.0])
    lucky = robust_score([9.0, 3.0, 1.0, 1.0])  # median 2.0 其实更低
    assert steady["score"] > lucky["score"]


# ---------------------------------------------------------------
# 护栏
# ---------------------------------------------------------------

def test_validate_primary_objective():
    for obj in ("sortino", "sharpe", "max_drawdown", "profit_factor"):
        guards.validate_primary_objective(obj)  # 不抛
    # 退化目标与未知目标都拒绝
    with pytest.raises(ValueError, match=r"退化|不适合"):
        guards.validate_primary_objective("win_rate")
    with pytest.raises(ValueError):
        guards.validate_primary_objective("nope_metric")


def test_trades_ok_fail_closed():
    assert guards.trades_ok({"n_trades": 30}, 30) is True
    assert guards.trades_ok({"n_trades": 29}, 30) is False
    # 缺 n_trades 按不达标处理 (fail-closed)
    assert guards.trades_ok({}, 10) is False
    assert guards.trades_ok({"n_trades": None}, 10) is False
    # <=0 关闭门槛
    assert guards.trades_ok({}, 0) is True


# ---------------------------------------------------------------
# 冠军收敛诊断
# ---------------------------------------------------------------

def _conv_row(params, values, **kw):
    return {"params": params, "oos_values": values, "disqualified": False, "error": None, **kw}


def test_champion_convergence_unanimous():
    rows = [
        _conv_row({"a": 1}, [5.0, 5.0, 5.0, 5.0]),
        _conv_row({"a": 2}, [4.0, 4.0, 4.0, 4.0]),
    ]
    conv = champion_convergence(rows, k=4, direction="max", objective="sortino")
    assert conv["label"] == "unanimous"
    assert conv["max_count"] == 4
    assert conv["champions"][0]["params"] == {"a": 1}


def test_champion_convergence_scattered():
    # 每段冠军各不相同 (k=4, max_count=1 <= k/2)
    rows = [
        _conv_row({"a": 1}, [5.0, 0.0, 0.0, 0.0]),
        _conv_row({"a": 2}, [0.0, 5.0, 0.0, 0.0]),
        _conv_row({"a": 3}, [0.0, 0.0, 5.0, 0.0]),
        _conv_row({"a": 4}, [0.0, 0.0, 0.0, 5.0]),
    ]
    conv = champion_convergence(rows, k=4, direction="max", objective="sortino")
    assert conv["label"] == "scattered"


def test_champion_convergence_majority():
    # k=4: 3 段同冠军 > k/2=2 但非全票
    rows = [
        _conv_row({"a": 1}, [5.0, 5.0, 5.0, 0.0]),
        _conv_row({"a": 2}, [0.0, 0.0, 0.0, 5.0]),
    ]
    conv = champion_convergence(rows, k=4, direction="max", objective="sortino")
    assert conv["label"] == "majority"
    assert conv["max_count"] == 3


def test_champion_convergence_ignores_disqualified_and_errors():
    rows = [
        _conv_row({"a": 1}, [9.0, 9.0], disqualified=True),
        _conv_row({"a": 2}, [1.0, 1.0], error="boom"),
        _conv_row({"a": 3}, [5.0, 5.0]),
    ]
    conv = champion_convergence(rows, k=2, direction="max", objective="sortino")
    # 只有 a=3 有资格; k=2 全票
    assert conv["label"] == "unanimous"
    assert conv["champions"][0]["params"] == {"a": 3}


def test_champion_convergence_no_data():
    rows = [_conv_row({"a": 1}, [1.0], disqualified=True)]
    conv = champion_convergence(rows, k=3, direction="max", objective="sortino")
    assert conv["label"] == "no_data"
    assert conv["champions"] == []


# ---------------------------------------------------------------
# 编排 (假 service / 引擎, 模式同 test_optimizer_run)
# ---------------------------------------------------------------

@dataclass
class _FakeDef:
    meta: dict
    execution_backend: str = "polars_expr"


class _FakeStrategyEngine:
    def __init__(self, params_meta, execution_backend="polars_expr"):
        self._def = _FakeDef({"params": params_meta}, execution_backend)

    def get(self, strategy_id):
        return self._def


@dataclass
class _FakeResult:
    stats: dict
    error: str | None = None


class _FakeService:
    """run() 依据 (params, start, end) 调 score_fn 返回受控 stats。

    start/end 让用例可区分验证段调用与阶段二全窗调用 (全窗 end == cfg.end)。
    """

    FULL_START = date(2024, 1, 1)
    FULL_END = date(2024, 12, 31)

    def __init__(self, score_fn, error_fn=None):
        self.score_fn = score_fn
        self.error_fn = error_fn  # (params, start, end) -> bool 触发异常
        self.calls = []
        self.result_policies = []

    def run(self, config, progress_cb=None, cancel_event=None, result_policy=None, prepared=None):
        self.calls.append({
            "params": dict(config.params or {}),
            "start": config.start,
            "end": config.end,
            "is_full": config.start == self.FULL_START and config.end == self.FULL_END,
        })
        self.result_policies.append(result_policy)
        if self.error_fn is not None and self.error_fn(config.params, config.start, config.end):
            raise RuntimeError("分段回测炸了")
        v = self.score_fn(config.params, config.start, config.end)
        return _FakeResult(stats={"sortino": v, "n_trades": 20, "total_return": v / 10})


_PARAMS_META = [
    {"id": "p", "type": "int", "default": 1, "min": 1, "max": 10, "step": 1},
]


def _cfg(**kw):
    base = dict(
        strategy_id="s", symbols=None,
        start=date(2024, 1, 1), end=date(2024, 12, 31),
        param_grid={"p": [1, 2, 3]},
        objective="sortino", k_segments=4, min_trades_total=30,
    )
    base.update(kw)
    return RobustOptimizeConfig(**base)


def test_robust_optimize_ranks_by_robust_score():
    # p 越大每段得分越高 → 排名 p=3 > p=2 > p=1
    svc = _FakeService(lambda params, start, end: float(params["p"]))
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    assert out["n_combinations"] == 3 and out["n_completed"] == 3
    assert out["best_params"] == {"p": 3}
    assert out["best_score"] == pytest.approx(0.6 * 3.0 + 0.4 * 3.0)
    ranks = [r["params"]["p"] for r in out["results"]]
    assert ranks == [3, 2, 1]
    assert [r["rank"] for r in out["results"]] == [1, 2, 3]
    # 每组 4 段都真的跑了 (3 组 × 4 段 = 12 次), 阶段二另跑 3 次全窗
    assert len(svc.calls) == 15
    assert sum(1 for c in svc.calls if c["is_full"]) == 3
    # 每段独立回测收到 trial 策略 (轻量 stats)
    assert all(pol is not None for pol in svc.result_policies)


def test_robust_optimize_lucky_one_segment_loses_to_steady():
    """p=1 只在首段爆发 (9.0) 其余 0.1; p=2 四段稳定 2.0 → 中位+最差让稳定者登顶。
    旧样本内优化会选峰值 (9.0 > 2.0), 这是本设计要堵的过拟合路径。"""
    def score(params, start, end):
        if params["p"] == 1:
            return 9.0 if start == date(2024, 1, 1) and end != _FakeService.FULL_END else 0.1
        return 2.0
    svc = _FakeService(score)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    # p=1 段峰值 9.0 最高, 但稳健分 0.1 垫底; 稳定的 p=2 压过它
    assert out["best_params"] == {"p": 2}
    lucky = next(r for r in out["results"] if r["params"]["p"] == 1)
    assert lucky["rank"] == 3
    assert lucky["spread"] == pytest.approx(8.9)  # 段间离散警报
    assert out["best_score"] > lucky["score"]


def test_robust_optimize_trades_gate_disqualifies():
    """K 段合计交易数不足的组合即使得分最高也不得冠军, 垫到合格组之后。"""
    def score(params, start, end):
        return float(params["p"])
    svc = _FakeService(score)
    # 每段 20 笔 × 4 段 = 80 笔; p=3 只有 5 笔/段 × 4 = 20 < 30 → 除名
    orig_run = svc.run

    def gated_run(config, **kw):
        res = orig_run(config, **kw)
        if config.params["p"] == 3:
            res.stats["n_trades"] = 5
        return res

    svc.run = gated_run
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    top = out["results"][0]
    assert top["params"] == {"p": 2} and top["disqualified"] is False
    dq = next(r for r in out["results"] if r["params"]["p"] == 3)
    assert dq["disqualified"] is True
    assert "交易数" in dq["disqualify_reason"]
    assert dq["rank"] == 3  # 垫在合格组后
    assert out["best_params"] == {"p": 2}


def test_robust_optimize_segment_error_isolated():
    """单组单段回测异常: 该组记 error 垫底, 其余组照常完成。"""
    def boom_on(params, start, end):
        return params["p"] == 2 and start == date(2024, 1, 1) and end != _FakeService.FULL_END
    svc = _FakeService(lambda params, start, end: float(params["p"]), error_fn=boom_on)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    err_row = next(r for r in out["results"] if r["params"]["p"] == 2)
    assert err_row["error"] and "分段回测炸了" in err_row["error"]
    assert err_row["rank"] == 3  # 错误组垫底
    assert out["n_completed"] == 2  # 出错组不计入完成数
    assert out["best_params"] == {"p": 3}


def test_robust_optimize_progress_and_cancel():
    events = []
    cancel = threading.Event()

    def score(params, start, end):
        if params["p"] == 1:   # 首组首段跑完即请求取消
            cancel.set()
        return float(params["p"])
    svc = _FakeService(score)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(), progress_cb=events.append, cancel_event=cancel,
    )

    assert out["cancelled"] is True
    assert len(out["results"]) == 1          # 只完成了首组
    assert out["n_combinations"] == 3        # 计划数不变
    assert events[-1]["done"] == 1 and events[-1]["total"] == 3
    assert all(e["type"] == "robust_optimizer_progress" for e in events)


def test_robust_optimize_result_windows_match_split():
    out = RobustOptimizeService(_FakeService(lambda p, s, e: 1.0), _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())
    wins = split_validation_windows(date(2024, 1, 1), date(2024, 12, 31), 4)
    assert out["windows"] == [{"start": s.isoformat(), "end": e.isoformat()} for s, e in wins]
    # 每组带各段明细 (段区间 + 原始目标值)
    row = out["results"][0]
    assert len(row["segments"]) == 4
    assert row["segments"][0]["objective_raw"] == 1.0


def test_robust_optimize_rejects_degenerate_objective():
    svc = _FakeService(lambda p, s, e: 1.0)
    with pytest.raises(ValueError, match=r"不适合"):
        RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
            _cfg(objective="win_rate"),
        )


def test_robust_optimize_explicit_min_direction_reverses_ranking():
    """显式 direction="min" 覆盖: 归一分数 = -raw, 排名应反转 (方向感知)。
    白名单主目标默认全为 max (回撤类为负值, 最大化带符号值 = 回撤越小越好)。"""
    def score(params, start, end):
        return float(params["p"])  # 值越大越差
    svc = _FakeService(score)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(direction="min"),
    )
    assert out["direction"] == "min"
    assert out["best_params"] == {"p": 1}
    # 归一分数 = -raw, 稳健分为负值
    assert out["best_score"] < 0 and math.isfinite(out["best_score"])


# ---------------------------------------------------------------
# P2: 阶段二 Top-N 全窗 IS 细化 + 高原判据
# ---------------------------------------------------------------

def test_stage2_full_window_is_and_degradation():
    """阶段二对合格组跑 [S,E] 全窗: IS 分 vs 稳健分差 = 样本内乐观偏差。"""
    # 段分 = 参数值; 全窗 IS 分固定 5.0 → 退化 = 5.0 - score
    svc = _FakeService(lambda p, s, e: float(p["p"]) if not (s == _FakeService.FULL_START and e == _FakeService.FULL_END) else 5.0)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    assert out["stage2"] == {"top_n": 20, "evaluated": 3}
    top = out["results"][0]           # p=3, score=3.0
    assert top["is_full"]["objective_raw"] == 5.0
    assert top["degradation"] == pytest.approx(5.0 - 3.0)
    assert top["degradation_pct"] == pytest.approx(2.0 / 5.0)
    # 排名不受阶段二影响 (仍是 p=3 冠军)
    assert out["best_params"] == {"p": 3}


def test_stage2_top_n_limits_and_disable():
    svc = _FakeService(lambda p, s, e: float(p["p"]))
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(stage2_top_n=1),
    )
    full_rows = [r for r in out["results"] if "is_full" in r]
    assert len(full_rows) == 1 and full_rows[0]["params"] == {"p": 3}  # 只有冠军
    assert out["stage2"] == {"top_n": 1, "evaluated": 1}

    svc2 = _FakeService(lambda p, s, e: float(p["p"]))
    out2 = RobustOptimizeService(svc2, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(stage2_top_n=0),
    )
    assert out2["stage2"]["evaluated"] == 0
    assert all("is_full" not in r for r in out2["results"])
    assert all(c["is_full"] is False for c in svc2.calls)  # 没有任何全窗调用


def test_stage2_skips_disqualified_and_error_rows():
    """阶段二只细化合格组: 除名组/错误组即使排名靠前也不跑全窗。"""
    svc = _FakeService(lambda p, s, e: float(p["p"]))
    orig_run = svc.run

    def gated_run(config, **kw):
        res = orig_run(config, **kw)
        if config.params["p"] == 3:          # 冠军除名
            res.stats["n_trades"] = 5
        return res

    svc.run = gated_run
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())
    # p=3 除名 → 阶段二只细化 p=2, p=1 (合格前二)
    full_rows = [r for r in out["results"] if "is_full" in r]
    assert sorted(r["params"]["p"] for r in full_rows) == [1, 2]


def test_stage2_error_isolated():
    """全窗回测异常只影响该行的 is_full.error, 不拖垮整体结果。"""
    def boom_full(params, start, end):
        return params["p"] == 3 and start == _FakeService.FULL_START and end == _FakeService.FULL_END
    svc = _FakeService(lambda p, s, e: float(p["p"]), error_fn=boom_full)
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())

    top = out["results"][0]
    assert top["params"] == {"p": 3}
    assert "分段回测炸了" in top["is_full"]["error"]
    assert "degradation" not in top
    # 其余行的阶段二照常
    assert out["results"][1]["degradation"] == pytest.approx(0.0)
    assert out["best_params"] == {"p": 3}


def test_stage2_progress_events():
    events = []
    svc = _FakeService(lambda p, s, e: float(p["p"]))
    RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(), progress_cb=events.append,
    )
    s2 = [e for e in events if e["type"] == "robust_optimizer_stage2"]
    assert len(s2) == 3
    assert s2[-1] == {"type": "robust_optimizer_stage2", "done": 3, "total": 3}


def test_stage2_skipped_when_cancelled():
    """取消后不进入阶段二 (预算纪律: 取消即停)。"""
    cancel = threading.Event()
    cancel.set()  # 一开始就取消: 阶段一 0 组
    svc = _FakeService(lambda p, s, e: float(p["p"]))
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(
        _cfg(), cancel_event=cancel,
    )
    assert out["cancelled"] is True
    assert out["stage2"]["evaluated"] == 0
    assert len(svc.calls) == 0


# ---------------------------------------------------------------
# P2: 高原判据 (纯函数)
# ---------------------------------------------------------------

def _prow(p_val, score, **kw):
    return {"params": {"p": p_val}, "score": score, "disqualified": False, "error": None, **kw}


def test_plateau_flat_grid_is_plateau():
    rows = [_prow(1, 1.0), _prow(2, 0.99), _prow(3, 0.98)]
    out = neighborhood_plateau(rows, rows[0])
    assert out["label"] == "plateau"
    assert out["ratio"] == 1.0
    assert out["n_valid"] == 1  # p=1 的直接邻居只有 p=2


def test_plateau_cliff_is_peak():
    rows = [_prow(1, 1.0), _prow(2, 0.2), _prow(3, 0.1)]
    out = neighborhood_plateau(rows, rows[0])
    assert out["label"] == "peak"
    assert out["ratio"] == 0.0


def test_plateau_mixed_is_slope():
    """2 参数 3×3 网格 (笛卡尔积序): 冠军 (1,1) 的直接邻居 = a±1 与 b±1 共 4 个,
    其中 2 个在容差内 (slope)。"""
    scores = {
        (0, 0): 0.5, (0, 1): 0.2, (0, 2): 0.5,
        (1, 0): 0.1, (1, 1): 1.0, (1, 2): 0.98,
        (2, 0): 0.5, (2, 1): 0.99, (2, 2): 0.5,
    }
    rows = [
        {"params": {"a": a, "b": b}, "score": scores[(a, b)], "disqualified": False, "error": None}
        for a in (0, 1, 2) for b in (0, 1, 2)
    ]
    champion = next(r for r in rows if r["params"] == {"a": 1, "b": 1})
    out = neighborhood_plateau(rows, champion)
    assert out["n_valid"] == 4
    assert out["label"] == "slope"       # (2,1)=0.99 与 (1,2)=0.98 在内容差内 → 2/4
    assert out["ratio"] == 0.5


def test_plateau_neighbor_is_one_grid_step_both_sides():
    """邻居 = 网格候选序的一步 (双侧): 网格 [1,2,3] 中 p=2 的邻居是 p=1 与 p=3;
    p=1 与 p=3 互不为邻居 (中间隔着 p=2)。"""
    rows = [_prow(1, 0.99), _prow(2, 1.0), _prow(3, 0.98)]
    mid = neighborhood_plateau(rows, rows[1])
    assert mid["n_valid"] == 2           # p=2 (冠军) 双侧邻居都算
    edge = neighborhood_plateau(rows, rows[0])
    assert edge["n_valid"] == 1          # p=1 只有 p=2 一个邻居, p=3 隔步不算


def test_plateau_ignores_invalid_neighbors():
    rows = [
        _prow(1, 1.0),
        _prow(2, 0.9, disqualified=True),   # 除名 → 不计入
        _prow(3, 0.5, error="boom"),        # 错误 → 不计入
    ]
    out = neighborhood_plateau(rows, rows[0])
    assert out["label"] == "isolated"  # 唯一直接邻居无效
    assert out["n_valid"] == 0


def test_plateau_negative_score_scale():
    """负值目标 (如回撤类归一分): 尺度取绝对值/极差, 判据同样成立。"""
    rows = [_prow(1, -3.0), _prow(2, -3.05), _prow(3, -6.0)]
    out = neighborhood_plateau(rows, rows[0])
    assert out["label"] == "plateau"   # 衰减 0.05 <= 0.05×max(3.0, 3.0)=0.15
    assert out["tolerance"] == pytest.approx(0.15)


def test_robust_optimize_result_contains_plateau():
    """服务级: 平坦网格 (1.0 / 0.995 / 0.99) → 冠军 p=1 落在高原。"""
    svc = _FakeService(lambda p, s, e: {1: 1.0, 2: 0.995, 3: 0.99}[p["p"]])
    out = RobustOptimizeService(svc, _FakeStrategyEngine(_PARAMS_META)).robust_optimize(_cfg())
    assert out["plateau"]["label"] == "plateau"
    assert out["plateau"]["ratio"] == 1.0
    assert out["plateau"]["n_valid"] == 1  # 冠军 p=1 的唯一直接邻居是 p=2


# ---------------------------------------------------------------
# P2: 折预览端点 (不回测, 只切窗)
# ---------------------------------------------------------------

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_robust_preview_returns_windows():
    from app.api.backtest import optimize_robust_preview

    out = _run(optimize_robust_preview(start="2024-01-01", end="2024-12-31", k_segments=4))
    assert out["k_segments"] == 4
    assert len(out["windows"]) == 4
    # 366 天 / 4 段 = 92/92/91/91 (余数摊给前面的段)
    assert out["windows"][0] == {"start": "2024-01-01", "end": "2024-04-01", "days": 92}
    assert out["warnings"] == []          # 4 段无告警


def test_robust_preview_warns_few_segments():
    from app.api.backtest import optimize_robust_preview

    out = _run(optimize_robust_preview(start="2024-01-01", end="2024-12-31", k_segments=2))
    assert out["k_segments"] == 2
    assert any("统计意义不足" in w for w in out["warnings"])


def test_robust_preview_rejects_bad_input():
    from fastapi import HTTPException

    from app.api.backtest import optimize_robust_preview

    with pytest.raises(HTTPException) as ei:
        _run(optimize_robust_preview(start="2024-01-01", end="2024-12-31", k_segments=7))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        _run(optimize_robust_preview(start="2024/01/01", end="2024-12-31", k_segments=4))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:  # 区间太短
        _run(optimize_robust_preview(start="2024-01-01", end="2024-01-20", k_segments=4))
    assert ei.value.status_code == 400


def test_walkforward_preview_returns_folds():
    from app.api.backtest import walkforward_preview

    out = _run(walkforward_preview(
        start="2024-01-01", end="2024-12-31", train_days=90, test_days=30, step_days=30,
    ))
    assert out["n_folds"] == len(out["folds"]) > 3
    f0 = out["folds"][0]
    assert f0["train_start"] == "2024-01-01" and f0["test_start"] > f0["train_end"]
    assert out["warnings"] == []


def test_walkforward_preview_warns_and_rejects():
    from fastapi import HTTPException

    from app.api.backtest import walkforward_preview

    out = _run(walkforward_preview(
        start="2024-01-01", end="2024-06-30", train_days=120, test_days=30, step_days=90,
    ))
    assert out["n_folds"] < 3
    assert any("统计意义不足" in w for w in out["warnings"])

    with pytest.raises(HTTPException) as ei:  # 放不下一折
        _run(walkforward_preview(
            start="2024-01-01", end="2024-02-01", train_days=90, test_days=30, step_days=30,
        ))
    assert ei.value.status_code == 400


def test_robust_cancel_by_echoed_job_key():
    """cancel 用 stream 回吐的 job_key 查表 (与 /optimize/cancel 同契约)。"""
    import asyncio

    from app.api.backtest import _BacktestJob, _running_jobs, optimize_robust_cancel

    class _Req:
        def __init__(self, body):
            self._body = body
        async def json(self):
            return self._body

    key = "robust_cancel_test_1"
    _running_jobs[key] = _BacktestJob(key)
    try:
        res = asyncio.run(optimize_robust_cancel(_Req({"job_key": key})))
        assert res["ok"] is True
        assert _running_jobs[key].cancel_event.is_set()
        assert asyncio.run(optimize_robust_cancel(_Req({"job_key": "nope"})))["ok"] is False
    finally:
        _running_jobs.pop(key, None)
