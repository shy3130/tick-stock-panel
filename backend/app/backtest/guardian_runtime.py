"""守护复验运行时: 发布/入护后的新数据上做 K 段真实回测并落判定。

进程模型与挖掘一致: 由 MiningJobManager 线程经 worker 子进程调用本函数。
矩阵纪律镜像稳健优化器 — 全区间超集矩阵 + 每段只读视图复用; 与挖掘共用
heavy 槽 (避免两份全市场矩阵并发)。

数据流纪律 (docs/auto-strategy-lab-plan.md §二): 本运行时的任何结果都
不反馈进取参加权 — 复验只做验证与退役判定。
"""
from __future__ import annotations

import itertools
import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from app.backtest.guardian import (
    GUARDIAN_ALGORITHM_VERSION,
    MIN_NEW_TRADING_DAYS,
    CheckSummary,
    GuardianThresholds,
    choose_k,
    evaluate_check,
    objective_soft_floor,
    plan_new_data_window,
    split_windows,
)
from app.backtest.guards import validate_primary_objective
from app.backtest.robust_optimizer import robust_score
from app.services.guardian_store import (
    GuardianStore,
    write_combinations,
)
from app.services.mining_preflight import enriched_partition_dates

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]
CancelCheck = Any  # threading.Event / mp.Event 或返回 bool 的可调用

_GUARDIAN_MATRIX_CACHE_BYTES = 64 * 1024 * 1024
# trailing_reference 基线标定用的尾随窗口长度 (交易日)。
_CALIBRATE_TRADING_DAYS = 252
# P2d 组合扫描: 相关性用近 _COMBINATION_WINDOW_TRADING_DAYS 个交易日的日收益,
# 共同日期不足 _COMBINATION_MIN_COMMON_DAYS 时该对不可判 (宁缺毋滥)。
_COMBINATION_WINDOW_TRADING_DAYS = 126
_COMBINATION_MIN_COMMON_DAYS = 30
_COMBINATION_MAX_CORR = 0.70
_SKIPPED_CAP = 200  # 结果里 skipped 明细上限, 防止极端数量策略撑爆事件载荷



class GuardianRuntimeCancelledError(RuntimeError):
    pass


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is None:
        return
    cancelled = cancel_check() if callable(cancel_check) else cancel_check.is_set()
    if cancelled:
        raise GuardianRuntimeCancelledError("guardian cancelled")


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _result_policy(objective: str):
    from app.backtest.strategy import BacktestResultPolicy

    return BacktestResultPolicy(
        required_stats=frozenset({objective, "n_trades", "max_drawdown"}),
        include_monte_carlo=str(objective).startswith("mc_maxdd_"),
        include_curves=False,
        include_trades=False,
        include_per_symbol_stats=False,
        include_return_distribution=False,
        include_benchmark=False,
        include_strategy_info=False,
    )


def run_guardian_runtime(
    config: Mapping[str, Any],
    *,
    data_dir: Path,
    service: Any,
    strategy_engine: Any,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """遍历守护档案执行一轮复验; 单策略失败隔离, 不中断整体。"""
    t0 = time.perf_counter()
    raw_thresholds = config.get("thresholds") or {}
    thresholds = GuardianThresholds(
        **{
            key: raw_thresholds[key]
            for key in (
                "watch_pct",
                "retire_pct",
                "max_soft_drawdown",
                "k_max",
                "min_new_trading_days",
            )
            if key in raw_thresholds
        }
    )
    store = GuardianStore(data_dir)
    if config.get("mode") == "combinations":
        return _run_combination_scan(
            config, data_dir=data_dir, service=service,
            strategy_engine=strategy_engine, store=store,
            cancel_check=cancel_check, t0=t0,
        )
    selected = config.get("strategy_ids")
    if selected is not None and not isinstance(selected, list):
        raise ValueError("guardian strategy_ids must be a list when provided")

    profiles = store.list_profiles()
    if selected is not None:
        wanted = {str(item) for item in selected}
        profiles = [p for p in profiles if str(p.get("strategy_id")) in wanted]

    # retired 档案不再自动复验 (设计: 退役不自动回场); 重新守护 = drop 后重新入护。
    runnable = [p for p in profiles if p.get("status") != "retired"]
    skipped_retired = len(profiles) - len(runnable)

    results: list[dict[str, Any]] = []
    checked = skipped = errors = 0
    for index, profile in enumerate(runnable):
        _raise_if_cancelled(cancel_check)
        strategy_id = str(profile["strategy_id"])
        outcome = _check_one(
            strategy_id,
            profile,
            store=store,
            data_dir=data_dir,
            service=service,
            strategy_engine=strategy_engine,
            thresholds=thresholds,
            run_id=str(config.get("run_id") or ""),
            cancel_check=cancel_check,
        )
        if outcome["outcome"] == "checked":
            checked += 1
        elif outcome["outcome"] == "skipped":
            skipped += 1
        else:
            errors += 1
        results.append({"strategy_id": strategy_id, **outcome})
        if progress_cb is not None:
            progress_cb(
                {
                    "type": "guardian_progress",
                    "done": index + 1,
                    "total": len(runnable),
                    "strategy_id": strategy_id,
                    "outcome": outcome["outcome"],
                    "verdict": outcome.get("verdict"),
                }
            )

    return {
        "status": "succeeded",
        "algorithm_version": GUARDIAN_ALGORITHM_VERSION,
        "checked": checked,
        "skipped": skipped,
        "errors": errors,
        "skipped_retired": skipped_retired,
        "results": results[:_SKIPPED_CAP],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _run_combination_scan(
    config: Mapping[str, Any],
    *,
    data_dir: Path,
    service: Any,
    strategy_engine: Any,
    store: GuardianStore,
    cancel_check: CancelCheck | None,
    t0: float,
) -> dict[str, Any]:
    """P2d: 健康守护冠军间的日收益相关性扫描, 低相关对写入组合建议。

    每策略一次近窗回测 (include_curves), 提取日收益做 Pearson 相关;
    只产出建议, 组合策略的创建始终由用户在页面上确认。
    """
    from app.backtest.strategy import StrategyBacktestConfig
    from app.strategy import config as strategy_config

    _raise_if_cancelled(cancel_check)
    healthy = [
        profile for profile in store.list_profiles()
        if profile.get("status") == "healthy"
    ]
    if len(healthy) < 2:
        return {
            "status": "succeeded", "mode": "combinations",
            "healthy_count": len(healthy), "pairs_evaluated": 0,
            "suggestions": 0, "results": [],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    partitions = enriched_partition_dates(data_dir, "stock")
    window = partitions[-_COMBINATION_WINDOW_TRADING_DAYS:]
    if len(window) < _COMBINATION_MIN_COMMON_DAYS:
        return {
            "status": "succeeded", "mode": "combinations",
            "healthy_count": len(healthy), "pairs_evaluated": 0,
            "suggestions": 0, "results": [],
            "error": f"enriched 数据不足 ({len(window)} 个交易日)",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    returns_by_strategy: dict[str, dict[str, float]] = {}
    errors: list[dict[str, str]] = []
    for profile in healthy:
        _raise_if_cancelled(cancel_check)
        strategy_id = str(profile["strategy_id"])
        try:
            strategy = strategy_engine.get(strategy_id)
        except ValueError:
            errors.append({"strategy_id": strategy_id, "error": "策略不可用"})
            continue
        asset_type = str((strategy.meta.get("asset_types") or ["stock"])[0])
        overrides = strategy_config.load_override(data_dir, strategy_id)
        params = strategy_engine.resolve_params(strategy, overrides=overrides)
        result = service.run(
            StrategyBacktestConfig(
                strategy_id=strategy_id,
                symbols=None,
                start=window[0],
                end=window[-1],
                params=dict(params),
                overrides=overrides,
                asset_type=asset_type,
            ),
            cancel_event=cancel_check,
        )
        series = _daily_returns(result)
        if series is None:
            errors.append({
                "strategy_id": strategy_id,
                "error": result.error or "未产出可用净值曲线",
            })
            continue
        returns_by_strategy[strategy_id] = series

    suggestions: list[dict[str, Any]] = []
    pairs_evaluated = 0
    ids = sorted(returns_by_strategy)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            corr = _pearson_on_common_dates(
                returns_by_strategy[left], returns_by_strategy[right]
            )
            pairs_evaluated += 1
            if corr is not None and corr <= _COMBINATION_MAX_CORR:
                suggestions.append({
                    "a": left, "b": right,
                    "corr": round(corr, 4),
                    "common_days": _common_day_count(
                        returns_by_strategy[left], returns_by_strategy[right]
                    ),
                })
    suggestions.sort(key=lambda item: item["corr"])
    write_combinations(
        data_dir,
        window_start=window[0].isoformat(),
        window_end=window[-1].isoformat(),
        suggestions=suggestions,
        pairs_evaluated=pairs_evaluated,
    )
    return {
        "status": "succeeded", "mode": "combinations",
        "healthy_count": len(healthy), "pairs_evaluated": pairs_evaluated,
        "suggestions": len(suggestions),
        "results": errors[:20],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _daily_returns(result: Any) -> dict[str, float] | None:
    """从回测净值曲线提取日收益序列 {date: pct}; 缺曲线或点数不足返回 None。"""
    curve = getattr(result, "equity_curve", None) or []
    points: list[tuple[str, float]] = []
    for item in curve:
        if not isinstance(item, Mapping):
            continue
        day = item.get("date")
        value = _finite_or_none(item.get("value"))
        if isinstance(day, str) and value is not None and value > 0:
            points.append((day[:10], value))
    if len(points) < _COMBINATION_MIN_COMMON_DAYS + 1:
        return None
    returns: dict[str, float] = {}
    for (_, prev), (day, cur) in itertools.pairwise(points):
        returns[day] = cur / prev - 1.0
    return returns if len(returns) >= _COMBINATION_MIN_COMMON_DAYS else None


def _pearson_on_common_dates(
    left: dict[str, float], right: dict[str, float]
) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < _COMBINATION_MIN_COMMON_DAYS:
        return None
    xs = [left[day] for day in common]
    ys = [right[day] for day in common]
    n = len(common)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0.0 or vy <= 0.0:
        return None  # 常数序列无相关性可言
    return cov / (vx * vy) ** 0.5


def _common_day_count(left: dict[str, float], right: dict[str, float]) -> int:
    return len(set(left) & set(right))


def _check_one(
    strategy_id: str,
    profile: Mapping[str, Any],
    *,
    store: GuardianStore,
    data_dir: Path,
    service: Any,
    strategy_engine: Any,
    thresholds: GuardianThresholds,
    run_id: str,
    cancel_check: CancelCheck | None,
) -> dict[str, Any]:
    try:
        strategy = strategy_engine.get(strategy_id)
    except ValueError:
        _record_simple_check(
            store, strategy_id, run_id, error=f"策略不存在或不可加载: {strategy_id}"
        )
        return {"outcome": "error", "error": "strategy_unavailable"}

    baseline = profile.get("baseline") or {}
    objective = str(baseline.get("objective") or "sharpe")
    try:
        validate_primary_objective(objective)
        objective_soft_floor(objective)
    except ValueError as exc:
        _record_simple_check(store, strategy_id, run_id, error=str(exc))
        return {"outcome": "error", "error": str(exc)}

    asset_type = str(
        (strategy.meta.get("asset_types") or ["stock"])[0]
    )
    partitions = enriched_partition_dates(data_dir, asset_type)
    if not partitions:
        _record_simple_check(
            store, strategy_id, run_id, error=f"{asset_type} enriched 数据不可用"
        )
        return {"outcome": "error", "error": "enriched_unavailable"}
    data_as_of = date.fromisoformat(str(baseline["data_as_of"]))

    # trailing_reference 且尚无分数: 先用尾随窗口标定基线 (只此一次)。
    metrics = baseline.get("metrics") or {}
    if baseline.get("kind") == "trailing_reference" and _finite_or_none(
        metrics.get("score")
    ) is None:
        calibrate_error = _calibrate_baseline(
            strategy_id,
            strategy,
            service=service,
            strategy_engine=strategy_engine,
            store=store,
            data_dir=data_dir,
            partitions=partitions,
            objective=objective,
            asset_type=asset_type,
            cancel_check=cancel_check,
        )
        if calibrate_error is not None:
            _record_simple_check(store, strategy_id, run_id, error=calibrate_error)
            return {"outcome": "error", "error": "calibration_failed"}
        metrics = (store.get(strategy_id) or {}).get("baseline", {}).get("metrics") or {}

    baseline_score = _finite_or_none(metrics.get("score"))
    baseline_n_trades = metrics.get("n_trades")
    baseline_n_trades = (
        int(baseline_n_trades)
        if isinstance(baseline_n_trades, (int, float)) and not isinstance(baseline_n_trades, bool)
        else 0
    )
    if baseline_score is None:
        _record_simple_check(
            store, strategy_id, run_id, error="基线分数缺失, 无法比较 (mining_gate 基线不应出现)"
        )
        return {"outcome": "error", "error": "baseline_missing"}

    window = plan_new_data_window(
        partitions, data_as_of, min_new_trading_days=thresholds.min_new_trading_days
    )
    if window is None:
        fresh = sum(1 for value in partitions if value > data_as_of)
        store.append_check(
            strategy_id,
            {
                "checked_at": _now_iso(),
                "run_id": run_id,
                "skipped": True,
                "reason": f"基线后新数据仅 {fresh} 个交易日 (需 ≥{thresholds.min_new_trading_days})",
            },
        )
        return {"outcome": "skipped", "reason": "insufficient_new_data"}

    window_start, window_end = window
    try:
        k = choose_k(window_start, window_end, thresholds.k_max)
    except ValueError as exc:
        store.append_check(
            strategy_id,
            {
                "checked_at": _now_iso(),
                "run_id": run_id,
                "skipped": True,
                "reason": str(exc),
            },
        )
        return {"outcome": "skipped", "reason": "window_too_short"}

    summary = _evaluate_windows(
        strategy_id,
        strategy,
        service=service,
        strategy_engine=strategy_engine,
        data_dir=data_dir,
        windows=split_windows(window_start, window_end, k),
        objective=objective,
        asset_type=asset_type,
        cancel_check=cancel_check,
    )
    missing = sum(1 for value in summary.segment_scores if value is None)
    if missing == len(summary.segment_scores):
        store.append_check(
            strategy_id,
            {
                "checked_at": _now_iso(),
                "run_id": run_id,
                "objective": objective,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "k": k,
                "error": summary.error or "全部验证段回测失败",
            },
        )
        return {"outcome": "error", "error": summary.error or "all_segments_failed"}

    verdict = evaluate_check(
        objective=objective,
        baseline_score=baseline_score,
        baseline_n_trades=baseline_n_trades,
        current_score=_finite_or_none(summary.robust.get("score")),
        current_n_trades=summary.n_trades_total,
        worst_drawdown=summary.worst_drawdown,
        thresholds=thresholds,
        incomplete_data=summary.incomplete_data,
        missing_segments=missing,
    )
    store.append_check(
        strategy_id,
        {
            "checked_at": _now_iso(),
            "run_id": run_id,
            "objective": objective,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "k": k,
            "segment_scores": summary.segment_scores,
            "segment_trades": summary.segment_trades,
            "robust": _sanitize_robust(summary.robust),
            # 残缺复验把首个段落级错误一并落档 (如"未产生买入信号"), 便于排查
            **({"error": summary.error} if summary.error else {}),
            "n_trades_total": summary.n_trades_total,
            "worst_drawdown": summary.worst_drawdown,
            "degradation_pct": verdict.degradation_pct,
            "verdict": verdict.status,
            "reasons": list(verdict.reasons),
            "incomplete": verdict.incomplete_data,
        },
    )
    if verdict.status != "healthy" or profile.get("status") != verdict.status:
        store.set_status(
            strategy_id,
            verdict.status,
            reason="; ".join(verdict.reasons) or None,
        )
    return {
        "outcome": "checked",
        "verdict": verdict.status,
        "degradation_pct": verdict.degradation_pct,
    }


def _evaluate_windows(
    strategy_id: str,
    strategy: Any,
    *,
    service: Any,
    strategy_engine: Any,
    data_dir: Path,
    windows: list[tuple[date, date]],
    objective: str,
    asset_type: str,
    cancel_check: CancelCheck | None,
) -> CheckSummary:
    from app.backtest.strategy import StrategyBacktestConfig
    from app.strategy import config as strategy_config

    overrides = strategy_config.load_override(data_dir, strategy_id)
    params = strategy_engine.resolve_params(strategy, overrides=overrides)
    policy = _result_policy(objective)

    def _config(start: date, end: date) -> Any:
        return StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=None,
            start=start,
            end=end,
            params=dict(params),
            overrides=overrides,
            asset_type=asset_type,
        )

    full_start, full_end = windows[0][0], windows[-1][1]
    prepared = None
    seg_views: list[Any] = []
    if getattr(strategy, "execution_backend", "") == "matrix_native":
        try:
            prepared = service.prepare_matrix_optimization(
                [_config(full_start, full_end)],
                matrix_cache_max_bytes=_GUARDIAN_MATRIX_CACHE_BYTES,
            )
            for seg_start, seg_end in windows:
                try:
                    seg_views.append(
                        service.prepare_matrix_optimization(
                            [_config(seg_start, seg_end)],
                            matrix_cache_max_bytes=_GUARDIAN_MATRIX_CACHE_BYTES,
                            market_data_override=prepared.market_data,
                        )
                    )
                except Exception:  # noqa: BLE001 — 单段视图失败走通用路径
                    logger.warning(
                        "守护复验 %s 段 %s 视图准备失败, 走通用路径",
                        strategy_id,
                        seg_start,
                        exc_info=True,
                    )
                    seg_views.append(None)
        except Exception as exc:  # noqa: BLE001 — 矩阵准备失败整体走通用路径
            logger.warning("守护复验 %s 矩阵准备失败, 走通用路径: %r", strategy_id, exc)
            prepared = None
            seg_views = []

    scores: list[float | None] = []
    trades: list[int | None] = []
    drawdowns: list[float | None] = []
    first_error: str | None = None
    try:
        for index, (seg_start, seg_end) in enumerate(windows):
            _raise_if_cancelled(cancel_check)
            config = _config(seg_start, seg_end)
            kwargs: dict[str, Any] = {"result_policy": policy}
            if prepared is not None and index < len(seg_views):
                view = seg_views[index]
                if view is not None:
                    kwargs["prepared"] = view
            elif prepared is not None:
                kwargs["prepared"] = prepared
            result = service.run(config, cancel_event=cancel_check, **kwargs)
            if result.error:
                scores.append(None)
                trades.append(None)
                drawdowns.append(None)
                first_error = first_error or str(result.error)
                continue
            stats = result.stats or {}
            scores.append(_finite_or_none(stats.get(objective)))
            n_trades = stats.get("n_trades")
            trades.append(
                int(n_trades)
                if isinstance(n_trades, (int, float)) and not isinstance(n_trades, bool)
                else None
            )
            drawdowns.append(_finite_or_none(stats.get("max_drawdown")))
    finally:
        for view in seg_views:
            if view is not None:
                view.compute_cache.close()
        if prepared is not None:
            prepared.compute_cache.close()

    valid_drawdowns = [value for value in drawdowns if value is not None]
    n_total = sum(value for value in trades if value is not None)
    return CheckSummary(
        objective=objective,
        segment_scores=scores,
        segment_trades=trades,
        robust=robust_score(scores),
        n_trades_total=n_total,
        worst_drawdown=min(valid_drawdowns) if valid_drawdowns else None,
        incomplete_data=any(value is None for value in scores),
        error=first_error,
    )


def _calibrate_baseline(
    strategy_id: str,
    strategy: Any,
    *,
    service: Any,
    strategy_engine: Any,
    store: GuardianStore,
    data_dir: Path,
    partitions: list[date],
    objective: str,
    asset_type: str,
    cancel_check: CancelCheck | None,
) -> str | None:
    """trailing_reference 首跑标定: 尾随窗口单次回测作为参考基线 (非 OOS, 仅供比较)。"""
    from app.backtest.strategy import StrategyBacktestConfig

    _raise_if_cancelled(cancel_check)
    window = partitions[-_CALIBRATE_TRADING_DAYS:]
    if len(window) < MIN_NEW_TRADING_DAYS:
        return f"尾随窗口交易日不足 ({len(window)})"
    from app.strategy import config as strategy_config

    overrides = strategy_config.load_override(data_dir, strategy_id)
    params = strategy_engine.resolve_params(strategy, overrides=overrides)
    config = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=window[0],
        end=window[-1],
        params=dict(params),
        overrides=overrides,
        asset_type=asset_type,
    )
    try:
        result = service.run(
            config, cancel_event=cancel_check, result_policy=_result_policy(objective)
        )
    except (OSError, ValueError, TypeError) as exc:
        return f"基线标定回测失败: {exc}"
    if result.error:
        return f"基线标定回测失败: {result.error}"
    score = _finite_or_none(result.stats.get(objective))
    if score is None:
        return "基线标定回测未返回有限分数"
    n_trades = result.stats.get("n_trades")
    store.set_baseline_metrics(
        strategy_id,
        {
            "score": score,
            "n_trades": int(n_trades)
            if isinstance(n_trades, (int, float)) and not isinstance(n_trades, bool)
            else 0,
            "max_drawdown": _finite_or_none(result.stats.get("max_drawdown")),
            "window_start": window[0].isoformat(),
            "window_end": window[-1].isoformat(),
        },
    )
    return None


def _record_simple_check(
    store: GuardianStore,
    strategy_id: str,
    run_id: str,
    *,
    error: str,
) -> None:
    store.append_check(
        strategy_id,
        {"checked_at": _now_iso(), "run_id": run_id, "error": error[:500]},
    )


def _sanitize_robust(robust: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _finite_or_none(value) for key, value in robust.items()}


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
