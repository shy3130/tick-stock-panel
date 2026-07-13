"""Walk-forward 优化 — 滚动窗口的样本内优化 + 样本外验证。

每折在训练区间用参数网格优化选出最优参数, 再在紧邻的测试区间用该参数做样本外(OOS)
回测。滚动前移。核心产出是 OOS 拼接净值 + 每折 IS-vs-OOS 退化 —— 样本内漂亮、样本外
崩溃即过拟合信号, 单次样本内回测看不到。

依赖 PR2a 的 StrategyOptimizer 做每折训练区间的网格优化。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


@dataclass
class Fold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def generate_folds(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[Fold]:
    """滚动窗口 fold 切分: 训练窗口固定长度, 测试窗口紧接其后, 按 step 前移。

    测试区间超出 end 即停止。数据区间放不下一折则抛错。
    """
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days / test_days / step_days 必须为正")

    folds: list[Fold] = []
    i = 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        # 测试区间从训练末日的次日开始: 回测区间是闭区间, 若 test_start==train_end 则
        # 该日 K 线同时进训练优化与 OOS 首日, 构成前视泄漏。后移一天隔断。
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start = train_start + timedelta(days=step_days)

    if not folds:
        raise ValueError(
            f"数据区间不足以切出至少一折 (需 train+test={train_days + test_days}天, "
            f"实有 {(end - start).days}天)"
        )
    return folds


def _norm(v: float, direction: str) -> float:
    """把目标值归一到"越大越好"空间, 以便跨目标一致地算退化 (min 类目标取负)。"""
    return -v if direction == "min" else v


def aggregate_oos(fold_records: list[dict], objective: str, direction: str = "max") -> dict:
    """从**有效折** (IS 与 OOS 都成功) 聚合: 复利净值 / IS-OOS 退化 / 一致性。

    调用方只传有效折 (best_params 非空且 OOS 未 error), 故此处每折 is_score/oos_objective
    均有值, 无需 .get 默认兜底 —— 无效折被伪装成 0 收益混入曾是 H1/H2 的根因。

    - compounded_oos_return: 各折 OOS 总收益复利
    - degradation: 归一空间下 IS 目标均值 - OOS 目标均值, 正值 = 样本外退化 (过拟合信号),
      对"越小越好"目标 (max_drawdown 等) 方向也正确
    - consistency: OOS 总收益 > 0 的折占比 (与目标方向无关, 直观)
    """
    n = len(fold_records)
    if n == 0:
        return {
            "n_folds": 0,
            "compounded_oos_return": 0.0,
            "avg_is_objective": None,
            "avg_oos_objective": None,
            "degradation": None,
            "consistency": 0.0,
            "oos_equity_curve": [],
        }

    equity = 1.0
    curve: list[dict] = []
    n_positive = 0
    for f in fold_records:
        r = float(f["oos_stats"].get("total_return", 0.0) or 0.0)
        equity *= (1 + r)
        if r > 0:
            n_positive += 1
        curve.append({"fold": f["index"], "date": str(f["test_end"]), "value": round(equity, 4)})

    is_vals = [f["is_score"] for f in fold_records if f["is_score"] is not None]
    oos_vals = [f["oos_objective"] for f in fold_records if f["oos_objective"] is not None]
    avg_is = round(float(sum(is_vals) / len(is_vals)), 4) if is_vals else None
    avg_oos = round(float(sum(oos_vals) / len(oos_vals)), 4) if oos_vals else None
    degradation = (
        round(_norm(avg_is, direction) - _norm(avg_oos, direction), 4)
        if (avg_is is not None and avg_oos is not None) else None
    )

    return {
        "n_folds": n,
        "compounded_oos_return": round(equity - 1.0, 4),
        "avg_is_objective": avg_is,
        "avg_oos_objective": avg_oos,
        "degradation": degradation,
        "consistency": round(n_positive / n, 4),
        "oos_equity_curve": curve,
    }


@dataclass
class WalkForwardConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    train_days: int = 252
    test_days: int = 63
    step_days: int = 63
    direction: str | None = None
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)


class WalkForwardService:
    """滚动窗口 walk-forward: 每折训练区间优化 -> 测试区间 OOS 验证 -> 聚合。"""

    def __init__(self, optimizer, service, strategy_engine) -> None:
        self.optimizer = optimizer
        self.service = service
        self.strategy_engine = strategy_engine

    def run(
        self,
        cfg: WalkForwardConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> dict:
        from app.backtest.optimizer import OptimizeConfig, default_direction
        from app.backtest.strategy import StrategyBacktestConfig

        t0 = time.perf_counter()
        direction = cfg.direction or default_direction(cfg.objective)
        folds = generate_folds(cfg.start, cfg.end, cfg.train_days, cfg.test_days, cfg.step_days)
        n_total = len(folds)

        # 遥测: 首尾快照 PanelCache, 量化跨折重叠区间重复扫盘的 IO 占比 (是否值得进一步优化)。
        cache_before = self.service.engine.cache_stats()

        valid_records: list[dict] = []   # IS 与 OOS 都成功, 计入聚合
        skipped: list[dict] = []          # 无优化结果 或 OOS 失败, 不计入聚合 (避免伪装成有效折)
        done = 0

        # IS 训练区间强制 position 模式: full 模式会让训练折未平仓持仓用 train_end 之后
        # (即 OOS 区间) 的真实 K 线平仓, IS 分数被未来数据污染 -> 优化选参乐观偏移, 使过拟合
        # 被掩盖。OOS 回测保留用户所选 mode。参数扫描优化只看正式区间内的表现即可。
        is_backtest_kwargs = {**cfg.backtest_kwargs, "mode": "position"}

        for f in folds:
            if cancel_event is not None and cancel_event.is_set():
                break

            # 训练区间: 网格优化选最优参数
            opt_cfg = OptimizeConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.train_start,
                end=f.train_end,
                param_grid=cfg.param_grid,
                objective=cfg.objective,
                direction=cfg.direction,
                max_workers=cfg.max_workers,
                base_params=cfg.base_params,
                overrides=cfg.overrides,
                backtest_kwargs=is_backtest_kwargs,  # IS 强制 position, 堵前视泄漏
            )
            opt_res = self.optimizer.optimize(opt_cfg, cancel_event=cancel_event)
            best_params = opt_res.get("best_params")
            is_score = opt_res.get("best_score")
            done += 1

            base = {
                "index": f.index,
                "train_start": str(f.train_start),
                "train_end": str(f.train_end),
                "test_start": str(f.test_start),
                "test_end": str(f.test_end),
            }

            # 训练区间没优化出参数 (全组失败/取消) -> 跳过, 不用默认参数硬跑 OOS 伪装成有效折
            if best_params is None:
                skipped.append({**base, "reason": "训练区间未优化出参数"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 测试区间: 用最优参数做样本外回测
            merged = {**cfg.base_params, **best_params}
            oos_cfg = StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.test_start,
                end=f.test_end,
                params=merged,
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            oos_res = self.service.run(oos_cfg, cancel_event=cancel_event)

            # OOS 失败 (含 cancelled) -> 跳过, 不把空/0 收益混入复利曲线
            if oos_res.error:
                skipped.append({**base, "best_params": best_params, "reason": f"OOS 回测失败: {oos_res.error}"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            oos_objective = oos_res.stats.get(cfg.objective)
            # 该折 OOS 是否较 IS 退化 (方向感知: min 类目标数值更大才是退化)
            oos_degraded = (
                _norm(oos_objective, direction) < _norm(is_score, direction)
                if (oos_objective is not None and is_score is not None) else None
            )
            valid_records.append({
                **base,
                "best_params": best_params,
                "is_score": is_score,
                "oos_objective": oos_objective,
                "oos_degraded": oos_degraded,
                "oos_stats": oos_res.stats,
            })

            if progress_cb is not None:
                progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})

        summary = aggregate_oos(valid_records, cfg.objective, direction)

        # 遥测收尾: 本次 WF 累计扫盘耗时 / 命中 / 复用, 与总耗时对比得出 load_panel 占比。
        cache_after = self.service.engine.cache_stats()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        io_seconds = round(cache_after["compute_seconds"] - cache_before["compute_seconds"], 4)
        io_pct = round(io_seconds * 1000 / elapsed_ms * 100, 1) if elapsed_ms > 0 else 0.0
        cache_telemetry = {
            "load_panel_seconds": io_seconds,
            "load_panel_pct": io_pct,  # 扫盘耗时 / WF 总耗时
            "scans": cache_after["compute_count"] - cache_before["compute_count"],
            "hits": cache_after["hit_count"] - cache_before["hit_count"],
            "single_flight_reuses": cache_after["reuse_count"] - cache_before["reuse_count"],
        }
        logger.info(
            "walk-forward IO 占比: load_panel %.3fs (%.1f%% of %.1fms) | 扫盘 %d 次 命中 %d 复用 %d",
            io_seconds, io_pct, elapsed_ms,
            cache_telemetry["scans"], cache_telemetry["hits"], cache_telemetry["single_flight_reuses"],
        )

        return {
            "objective": cfg.objective,
            "direction": direction,
            "n_folds": len(valid_records),
            "n_skipped": len(skipped),
            "n_planned_folds": n_total,
            "folds": valid_records,
            "skipped": skipped,
            "summary": summary,
            "cache_telemetry": cache_telemetry,
            "elapsed_ms": elapsed_ms,
        }
