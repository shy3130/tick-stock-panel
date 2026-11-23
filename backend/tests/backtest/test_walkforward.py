"""Walk-forward 核心测试 — 滚动窗口 fold 生成 + OOS 聚合 + 编排。

被测:
- generate_folds: 滚动训练/测试窗口切分
- aggregate_oos: 从各折 OOS 结果聚合 (复利净值/IS-OOS 退化/一致性)
- WalkForwardService.run: 每折 训练区间优化 -> 测试区间 OOS 验证
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from app.backtest.walkforward import (
    WalkForwardConfig,
    WalkForwardService,
    aggregate_oos,
    generate_folds,
)

# ---------------------------------------------------------------
# fold 生成
# ---------------------------------------------------------------

def test_folds_rolling_windows():
    # 1 年数据, 训练 90d / 测试 30d / 步进 30d
    folds = generate_folds(date(2024, 1, 1), date(2024, 12, 31), train_days=90, test_days=30, step_days=30)
    assert len(folds) > 0
    f0 = folds[0]
    assert f0.train_start == date(2024, 1, 1)
    assert f0.train_end == date(2024, 3, 31)     # +90d (2024 闰年)
    assert f0.test_start == date(2024, 4, 1)     # train_end + 1天 (隔断前视泄漏)
    assert f0.test_end == date(2024, 5, 1)       # +30d
    # 滚动: 下一折训练起点 +step
    assert folds[1].train_start == date(2024, 1, 31)  # +30d


def test_folds_test_starts_day_after_train_end():
    """无前视泄漏: 每折 test_start 严格晚于 train_end (不共享同一天)。"""
    folds = generate_folds(date(2024, 1, 1), date(2024, 12, 31), train_days=90, test_days=30, step_days=30)
    for f in folds:
        assert f.test_start > f.train_end


def test_folds_no_test_beyond_end():
    folds = generate_folds(date(2024, 1, 1), date(2024, 12, 31), train_days=90, test_days=30, step_days=30)
    for f in folds:
        assert f.test_end <= date(2024, 12, 31)


def test_folds_insufficient_span_raises():
    # 训练90+测试30=120d, 但只有 100d 数据 -> 0 折
    with pytest.raises(ValueError, match=r"数据区间不足|至少"):
        generate_folds(date(2024, 1, 1), date(2024, 4, 10), train_days=90, test_days=30, step_days=30)


def test_folds_reject_nonpositive_windows():
    with pytest.raises(ValueError, match=r"必须为正"):
        generate_folds(date(2024, 1, 1), date(2024, 12, 31), train_days=0, test_days=30, step_days=30)


# ---------------------------------------------------------------
# OOS 聚合
# ---------------------------------------------------------------

def _rec(index, is_score, total_return, obj):
    return {
        "index": index,
        "test_end": date(2024, 1, 1),
        "best_params": {"p": index},
        "is_score": is_score,
        "oos_objective": obj,
        "oos_stats": {"total_return": total_return, "sortino": obj},
    }


def test_aggregate_compounds_oos_returns():
    recs = [_rec(0, 2.0, 0.10, 1.5), _rec(1, 2.0, -0.05, 0.8), _rec(2, 2.0, 0.08, 1.2)]
    agg = aggregate_oos(recs, objective="sortino")
    # 复利: 1.1 * 0.95 * 1.08 - 1
    assert abs(agg["compounded_oos_return"] - (1.10 * 0.95 * 1.08 - 1)) < 1e-9
    assert len(agg["oos_equity_curve"]) == 3


def test_aggregate_is_oos_degradation():
    # IS 目标平均远高于 OOS -> 退化为正 (过拟合信号)
    recs = [_rec(0, 3.0, 0.05, 0.5), _rec(1, 3.0, 0.02, 0.3)]
    agg = aggregate_oos(recs, objective="sortino")
    assert agg["avg_is_objective"] == 3.0
    assert abs(agg["avg_oos_objective"] - 0.4) < 1e-9
    assert agg["degradation"] > 0  # IS 3.0 - OOS 0.4 = 2.6


def test_aggregate_consistency_fraction_positive():
    # consistency 按 OOS 总收益 > 0 的折占比: total_return 0.1>0, -0.1<=0, 0.1>0 -> 2/3
    recs = [_rec(0, 1, 0.1, 1.5), _rec(1, 1, -0.1, -0.2), _rec(2, 1, 0.1, 0.8)]
    agg = aggregate_oos(recs, objective="sortino")
    assert agg["consistency"] == round(2 / 3, 4)  # 0.6667


def test_aggregate_degradation_direction_aware_for_min_objective():
    """min 类目标 (avg_holding_days, 越小越好): OOS 持仓天数更大 = 退化, degradation>0。"""
    # IS 持仓 3 天, OOS 持仓 5 天 (更长=更差) -> 退化
    recs = [{"index": 0, "test_end": date(2024, 1, 1), "is_score": 3.0,
             "oos_objective": 5.0, "oos_stats": {"total_return": 0.05}}]
    agg = aggregate_oos(recs, objective="avg_holding_days", direction="min")
    # 归一空间: norm(3)=-3, norm(5)=-5 -> degradation = -3 - (-5) = 2 > 0 = 退化
    assert agg["degradation"] == round(2.0, 4)


def test_aggregate_empty_folds():
    agg = aggregate_oos([], objective="sortino")
    assert agg["n_folds"] == 0
    assert agg["compounded_oos_return"] == 0.0


# ---------------------------------------------------------------
# 编排 (假 optimizer / service)
# ---------------------------------------------------------------

@dataclass
class _FakeResult:
    stats: dict
    error: str | None = None


class _FakeOptimizer:
    """optimize 返回受控 best_params/best_score, 记录被优化的训练区间。"""
    def __init__(self):
        self.train_ranges = []
        self.opt_kwargs = []  # 记录每折 IS 优化收到的 backtest_kwargs (验证 mode 强制)

    def optimize(self, cfg, progress_cb=None, cancel_event=None):
        self.train_ranges.append((cfg.start, cfg.end))
        self.opt_kwargs.append(dict(cfg.backtest_kwargs))
        # best_params 随训练起点变化, best_score 固定
        return {"best_params": {"p": cfg.start.month}, "best_score": 2.0, "results": [], "n_completed": 1}


# 从真实 PanelCache 取字段模板 —— 字段被重命名时本桩自动跟随, 避免 test 绿而生产 KeyError。
from app.backtest.engine import PanelCache

_ZERO_CACHE_STATS = {k: type(v)() for k, v in PanelCache().stats().items()}


class _FakeEngine:
    """最小引擎桩: 仅提供 WF 遥测所需的 cache_stats (字段同源自 PanelCache.stats)。"""
    def cache_stats(self):
        return dict(_ZERO_CACHE_STATS)


class _FakeService:
    """run 返回受控 OOS stats, 记录测试区间 + 收到的 params。

    stats 带 n_trades: 每折 OOS 交易数门槛 (fail-closed) 要求桩显式声明交易数,
    缺省 50 足以通过默认 min_trades_oos=10。
    """
    def __init__(self, n_trades=50):
        self.calls = []
        self.engine = _FakeEngine()
        self.n_trades = n_trades

    def run(self, config, progress_cb=None, cancel_event=None):
        self.calls.append({"start": config.start, "end": config.end,
                           "params": dict(config.params or {}), "mode": config.mode})
        return _FakeResult(stats={"total_return": 0.05, "sortino": 1.0, "n_trades": self.n_trades})


def _wf_cfg(**kw):
    base = dict(
        strategy_id="s", symbols=None, start=date(2024, 1, 1), end=date(2024, 12, 31),
        param_grid={"p": [1, 2]}, objective="sortino",
        train_days=90, test_days=30, step_days=30,
    )
    base.update(kw)
    return WalkForwardConfig(**base)


def test_walkforward_optimizes_train_applies_oos():
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())

    assert out["n_folds"] > 0
    # 每折: optimizer 在训练区间跑, service 在测试区间用最优参数跑
    assert len(opt.train_ranges) == out["n_folds"]
    assert len(svc.calls) == out["n_folds"]
    # OOS 回测用的是该折优化出的 best_params (来自训练起点月份)
    first_fold = out["folds"][0]
    assert svc.calls[0]["params"] == first_fold["best_params"]
    # 训练区间与测试区间不重叠 (测试在训练之后)
    assert svc.calls[0]["start"] >= opt.train_ranges[0][1]


class _CountingEngine:
    """首尾两次 cache_stats 返回不同值, 用于验证 WF 遥测差值/顺序计算 (非全零掩盖)。"""
    def __init__(self):
        self._n = 0

    def cache_stats(self):
        self._n += 1
        if self._n == 1:  # run 开头快照 (before)
            return {"compute_seconds": 1.0, "compute_count": 2, "hit_count": 0, "reuse_count": 0}
        return {"compute_seconds": 3.5, "compute_count": 7, "hit_count": 4, "reuse_count": 3}  # after


def test_walkforward_cache_telemetry_computes_deltas():
    """cache_telemetry 用首尾快照差值: scans/hits/reuses/秒数 = after - before, 且方向正确。"""
    opt, svc = _FakeOptimizer(), _FakeService()
    svc.engine = _CountingEngine()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())

    tel = out["cache_telemetry"]
    assert tel["scans"] == 5          # 7 - 2, 顺序写反会得 -5
    assert tel["hits"] == 4           # 4 - 0
    assert tel["single_flight_reuses"] == 3  # 3 - 0
    assert abs(tel["load_panel_seconds"] - 2.5) < 1e-9  # 3.5 - 1.0
    assert tel["load_panel_pct"] >= 0.0  # 扫盘耗时 / WF总耗时, 非负


def test_walkforward_forces_position_mode_for_is_optimization():
    """训练折(IS)强制 position 防前视泄漏(full 会用 OOS 区间 K 线平仓污染 IS);
    OOS 回测保留用户所选 mode。"""
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    wf.run(_wf_cfg(backtest_kwargs={"mode": "full"}))

    assert len(opt.opt_kwargs) > 0 and len(svc.calls) > 0
    # 用户选了 full, 但每折 IS 优化都被强制 position
    assert all(kw["mode"] == "position" for kw in opt.opt_kwargs), "IS 优化未强制 position"
    # OOS 回测保留用户的 full
    assert all(c["mode"] == "full" for c in svc.calls), "OOS 未保留用户 mode"


def test_walkforward_reports_degradation():
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())
    # IS best_score=2.0, OOS sortino=1.0 -> 退化 1.0
    assert out["summary"]["avg_is_objective"] == 2.0
    assert out["summary"]["avg_oos_objective"] == 1.0
    assert abs(out["summary"]["degradation"] - 1.0) < 1e-9


# ---------------------------------------------------------------
# 非矩阵后端的闸门: filter_history/内置走通用路径, 其余 fail-closed
# ---------------------------------------------------------------

class _FakeStrategyDef:
    def __init__(self, backend):
        self.execution_backend = backend


class _FakeStrategyEngine:
    def get(self, strategy_id):
        return _FakeStrategyDef(self._backend)

    def __init__(self, backend):
        self._backend = backend


def test_walkforward_supports_filter_history_strategy():
    """python_history_legacy (filter_history) 策略走通用路径:
    不建共享矩阵, 每折在训练区间独立优化、测试区间独立 OOS。"""
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=_FakeStrategyEngine("python_history_legacy"))
    out = wf.run(_wf_cfg())

    assert out["n_folds"] > 0
    assert out["shared_market_data"] is False
    assert len(opt.train_ranges) == out["n_folds"]
    assert len(svc.calls) == out["n_folds"]
    first_fold = out["folds"][0]
    assert svc.calls[0]["params"] == first_fold["best_params"]
    assert svc.calls[0]["start"] >= opt.train_ranges[0][1]


def test_walkforward_supports_polars_expr_strategy():
    """内置 polars_expr 策略同样走通用路径 (与矩阵策略共用同一套折编排)。"""
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=_FakeStrategyEngine("polars_expr"))
    out = wf.run(_wf_cfg())
    assert out["n_folds"] > 0
    assert out["shared_market_data"] is False


def test_walkforward_rejects_minute_filter_strategy():
    """minute_filter / composite 无法用日线折编排评估: 保持 fail-closed。"""
    wf = WalkForwardService(
        _FakeOptimizer(), _FakeService(),
        strategy_engine=_FakeStrategyEngine("minute_filter"),
    )
    with pytest.raises(ValueError, match=r"步进优化暂仅支持|minute_filter"):
        wf.run(_wf_cfg())


class _NoParamsOptimizer(_FakeOptimizer):
    """模拟训练区间全组失败: best_params=None。"""
    def optimize(self, cfg, progress_cb=None, cancel_event=None):
        self.train_ranges.append((cfg.start, cfg.end))
        return {"best_params": None, "best_score": None, "results": [], "n_completed": 0}


class _ErrorService(_FakeService):
    """模拟 OOS 回测失败。"""
    def run(self, config, progress_cb=None, cancel_event=None):
        self.calls.append({"start": config.start, "end": config.end, "params": dict(config.params or {})})
        return _FakeResult(stats={}, error="no data")


def test_walkforward_skips_folds_without_optimized_params():
    """训练区间没优化出参数 (best_params=None) -> 跳过, 不用默认参数硬跑 OOS 伪装成有效折。"""
    opt, svc = _NoParamsOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())
    assert out["n_folds"] == 0           # 无有效折
    assert out["n_skipped"] > 0          # 全部跳过
    assert svc.calls == []               # 不跑 OOS
    assert out["summary"]["compounded_oos_return"] == 0.0  # 无效折不污染净值


def test_walkforward_skips_oos_error_folds():
    """OOS 回测失败的折 -> 跳过, 不把空/0 收益混入复利曲线。"""
    opt, svc = _FakeOptimizer(), _ErrorService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())
    assert out["n_folds"] == 0
    assert out["n_skipped"] > 0
    assert len(svc.calls) > 0            # OOS 跑了但失败
    assert out["summary"]["compounded_oos_return"] == 0.0  # 失败折不计入


def test_walkforward_cancel_stops():
    import threading
    ev = threading.Event()
    ev.set()
    opt, svc = _FakeOptimizer(), _FakeService()
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg(), cancel_event=ev)
    # 取消 -> 不跑任何折
    assert svc.calls == []
    assert out["n_folds"] == 0


# ---------------------------------------------------------------
# API: job_key 回吐 + cancel 按 key 查表
# ---------------------------------------------------------------

def test_wf_job_key_distinguishes_windows():
    from app.api.backtest import _make_wf_job_key
    base = _make_wf_job_key("s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig")
    assert base != _make_wf_job_key("s", None, None, None, '{"p":[1]}', "sortino", None, "120/30/30", "sig")


def test_wf_job_key_distinguishes_params_and_overrides():
    """params/overrides 不同必须产出不同 job_key —— 否则 stream 与 cancel 会错配到别的任务。"""
    from app.api.backtest import _make_wf_job_key
    base = _make_wf_job_key("s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig")
    # params 不同 (未扫描参数固定值不同 -> 优化的策略不同)
    assert base != _make_wf_job_key(
        "s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig", params='{"x":1}')
    # overrides 不同 (basic_filter/信号/风控 不同)
    assert base != _make_wf_job_key(
        "s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig", overrides='{"score_min":5}')
    # 相同 params/overrides 必须稳定一致 (stream 端与 cancel 端对齐前提)
    k = _make_wf_job_key("s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig", params='{"x":1}')
    assert k == _make_wf_job_key(
        "s", None, None, None, '{"p":[1]}', "sortino", None, "252/63/63", "sig", params='{"x":1}')


def test_wf_cancel_by_echoed_key():
    import asyncio

    from app.api.backtest import _BacktestJob, _running_jobs, walkforward_cancel

    class _Req:
        def __init__(self, body):
            self._body = body
        async def json(self):
            return self._body

    key = "wfkey_test_1"
    _running_jobs[key] = _BacktestJob(key)
    try:
        res = asyncio.run(walkforward_cancel(_Req({"job_key": key})))
        assert res["ok"] is True
        assert _running_jobs[key].cancel_event.is_set()
        res2 = asyncio.run(walkforward_cancel(_Req({"job_key": "nope"})))
        assert res2["ok"] is False
    finally:
        _running_jobs.pop(key, None)


# ---------------------------------------------------------------
# 每折 OOS 交易数门槛 (共享护栏 trades_ok, fail-closed)
# ---------------------------------------------------------------

def test_walkforward_oos_min_trades_gate_skips_thin_folds():
    """OOS 段交易数不足的折 → 标记 skipped (原因可见), 不混入复利曲线。"""
    opt, svc = _FakeOptimizer(), _FakeService(n_trades=3)  # 3 < 默认 10
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())

    assert out["n_planned_folds"] > 0
    assert out["n_folds"] == 0                    # 全部折被门槛拦下
    assert out["n_skipped"] == out["n_planned_folds"]
    assert all("OOS 交易数不足" in s["reason"] for s in out["skipped"])
    assert out["skipped"][0]["best_params"] is not None  # 仍可诊断用了什么参数


def test_walkforward_oos_min_trades_gate_disabled_when_zero():
    """min_trades_oos<=0 关闭门槛: 少交易折恢复计入 (向后兼容开关)。"""
    opt, svc = _FakeOptimizer(), _FakeService(n_trades=3)
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg(min_trades_oos=0))

    assert out["n_folds"] == out["n_planned_folds"]
    assert out["n_skipped"] == 0


def test_walkforward_oos_missing_n_trades_fails_closed():
    """stats 缺 n_trades 字段按不达标处理 (宁可误杀不让无交易折伪装有效)。"""
    opt, svc = _FakeOptimizer(), _FakeService(n_trades=3)
    orig_run = svc.run

    def run_without_trades(config, **kw):
        res = orig_run(config, **kw)
        res.stats.pop("n_trades")
        return res

    svc.run = run_without_trades
    wf = WalkForwardService(opt, svc, strategy_engine=None)
    out = wf.run(_wf_cfg())

    assert out["n_folds"] == 0
    assert all("交易数不足 (0 <" in s["reason"] for s in out["skipped"])


def test_walkforward_stream_rejects_degenerate_objective():
    """P4 白名单全面生效: 退化目标 (win_rate) 在 walkforward 端点层即被 400 拒绝。"""
    import asyncio

    from fastapi import HTTPException

    from app.api.backtest import walkforward_stream

    class _Req:
        def __init__(self):
            self.app = None

    with pytest.raises(HTTPException) as ei:
        asyncio.run(walkforward_stream(_Req(), strategy_id="s", param_grid='{"p":[1]}', objective="win_rate"))
    assert ei.value.status_code == 400
    assert "退化" in str(ei.value.detail) or "不适合" in str(ei.value.detail)
