"""Backtest robustness checks: pure post-processing."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from app.backtest.metrics import MetricContext, annualized_sharpe

_DAILY_METRIC_CONTEXT = MetricContext("daily")


def returns_from_equity_curve(curve: list[dict]) -> np.ndarray:
    vals = np.asarray([float(p["value"]) for p in curve], dtype=float)
    if len(vals) < 2:
        return np.empty(0)
    return vals[1:] / vals[:-1] - 1.0


def bootstrap_sharpe_ci(
    rets,
    n_boot: int = 1000,
    ci: float = 0.95,
    seed: int | None = None,
    context: MetricContext = _DAILY_METRIC_CONTEXT,
) -> dict:
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return {
            "sharpe": 0.0,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "ci": ci,
            "n_boot": n_boot,
            "metric_context": context.to_dict(),
        }
    rng = np.random.default_rng(seed)
    samples = np.empty(n_boot)
    for i in range(n_boot):
        samples[i] = _sharpe(
            rets[rng.integers(0, len(rets), len(rets))],
            context,
        )
    lo, hi = np.quantile(samples, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return {
        "sharpe": round(_sharpe(rets, context), 4),
        "ci_low": round(float(lo), 4),
        "ci_high": round(float(hi), 4),
        "ci": ci,
        "n_boot": n_boot,
        "metric_context": context.to_dict(),
    }


def mc_permutation_pvalue(
    rets,
    n_perm: int = 1000,
    seed: int | None = None,
    context: MetricContext = _DAILY_METRIC_CONTEXT,
) -> dict:
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return {
            "p_value": 1.0,
            "n_perm": n_perm,
            "observed_sharpe": 0.0,
            "metric_context": context.to_dict(),
        }
    rng = np.random.default_rng(seed)
    observed = abs(_sharpe(rets, context))
    count = 0
    for _ in range(n_perm):
        if abs(_sharpe(rets * rng.choice([-1.0, 1.0], size=len(rets)), context)) >= observed:
            count += 1
    return {
        "p_value": round((count + 1) / (n_perm + 1), 4),
        "n_perm": n_perm,
        "observed_sharpe": round(_sharpe(rets, context), 4),
        "metric_context": context.to_dict(),
    }


def exit_reason_breakdown(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get("exit_reason") or "(none)"), []).append(float(trade.get("pnl_pct") or 0.0))
    rows = []
    for reason, pnls in sorted(groups.items()):
        arr = np.asarray(pnls, dtype=float)
        rows.append({
            "exit_reason": reason,
            "n": int(len(arr)),
            "win_rate": round(float((arr > 0).mean()), 4),
            "avg_pnl_pct": round(float(arr.mean()), 4),
            "total_pnl_pct": round(float(arr.sum()), 4),
        })
    return rows


def _finite_stat_value(stats, metric: str) -> float | None:
    """stats dict 中可聚合的有限数值; None/非数值/非有限 (NaN, ±inf) 均不可聚合。"""
    if not isinstance(stats, dict):
        return None
    raw = stats.get(metric)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if bool(np.isfinite(value)) else None


def _finite_fold_metric(fold: dict, metric: str) -> float | None:
    """折内可聚合的有限数值; None/非数值/非有限 (NaN, ±inf) 均不可聚合。"""
    return _finite_stat_value(fold.get("stats"), metric)


def segment_stability_summary(folds: list[dict], metric: str = "sharpe") -> dict:
    """同参数顺序切段的分段稳定性聚合。stats 中不可计算的指标 (策略服务会产生
    sharpe=None) 不进入聚合, 全部不可计算时返回空摘要 — /strategy/robustness
    不得因此 500。严格训练→冻结→OOS 的 Walk-Forward 见 run_walk_forward。"""
    vals = np.asarray(
        [v for v in (_finite_fold_metric(f, metric) for f in folds) if v is not None],
        dtype=float,
    )
    if len(vals) == 0:
        return {"metric": metric, "n_folds": 0, "mean": 0.0, "std": 0.0, "worst": 0.0, "positive_folds": 0}
    return {
        "metric": metric,
        "n_folds": int(len(vals)),
        "mean": round(float(vals.mean()), 4),
        "std": round(float(vals.std(ddof=1)), 4) if len(vals) > 1 else 0.0,
        "worst": round(float(vals.min()), 4),
        "positive_folds": int((vals > 0).sum()),
    }

def parameter_perturbations(
    param_specs: list[dict],
    params: dict | None,
    *,
    fraction: float = 0.1,
    max_params: int = 6,
) -> list[dict]:
    """Build bounded ± perturbations for numeric strategy parameters.

    Parameter metadata is authoritative for type/range/defaults.  This keeps
    integer lookback periods integral and never perturbs bool/select controls.
    """
    current_params = params or {}
    cases: list[dict] = []
    selected = 0
    for spec in param_specs:
        if selected >= max_params:
            break
        param_id = spec.get("id")
        param_type = spec.get("type")
        if not param_id or param_type not in {"int", "float"}:
            continue
        raw = current_params.get(param_id, spec.get("default"))
        try:
            baseline = float(raw)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(baseline):
            continue

        raw_step = spec.get("step")
        try:
            step = abs(float(raw_step)) if raw_step is not None else 0.0
        except (TypeError, ValueError):
            step = 0.0
        delta = max(abs(baseline) * fraction, step)
        if delta == 0:
            delta = 1.0 if param_type == "int" else fraction

        bounds: list[float | None] = []
        for key in ("min", "max"):
            try:
                value = spec.get(key)
                bounds.append(float(value) if value is not None else None)
            except (TypeError, ValueError):
                bounds.append(None)
        lower, upper = bounds

        variants: list[tuple[str, float]] = []
        for direction, value in (("down", baseline - delta), ("up", baseline + delta)):
            if lower is not None:
                value = max(value, lower)
            if upper is not None:
                value = min(value, upper)
            if param_type == "int":
                value = float(round(value))
            if value != baseline and all(existing != value for _, existing in variants):
                variants.append((direction, value))
        if not variants:
            continue

        selected += 1
        for direction, value in variants:
            cases.append({
                "param": str(param_id),
                "label": str(spec.get("label") or param_id),
                "direction": direction,
                "base_value": int(baseline) if param_type == "int" else baseline,
                "value": int(value) if param_type == "int" else round(value, 8),
            })
    return cases


def _sharpe(rets: np.ndarray, context: MetricContext = _DAILY_METRIC_CONTEXT) -> float:
    value = annualized_sharpe(rets, context)
    return 0.0 if value is None else float(value)


# ================================================================
# 严格 Walk-Forward: 训练优化 → 冻结参数 → 独立 OOS
# ================================================================

WALK_FORWARD_MIN_OOS_DAYS = 30
WALK_FORWARD_MAX_FOLDS = 12
WALK_FORWARD_CANDIDATE_SPACE = "baseline + 单参数±扰动邻域 (局部邻域, 非全局优化)"
_STATS_KEYS = ("total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "n_trades")


def walk_forward_fold_plan(
    start: date,
    end: date,
    n_folds: int,
    *,
    min_oos_days: int = WALK_FORWARD_MIN_OOS_DAYS,
) -> list[dict]:
    """expanding-train Walk-Forward 折计划 (纯日历切分, 交易日由引擎按窗口解析)。

    区间切成 n_folds+1 份: 第 1 份为初始训练窗, 之后每份依次为各折 OOS;
    第 i 折训练窗 = [start, oos_start-1], 随折扩张。请求折数放不下时自动收缩,
    连 1 折 (初始训练 + ≥min_oos_days 的 OOS) 都放不下时返回 [] — 由调用方
    输出结构化 warning, 不伪造折。
    """
    n_folds = max(1, min(WALK_FORWARD_MAX_FOLDS, int(n_folds)))
    total = (end - start).days + 1
    if total // (n_folds + 1) < min_oos_days:
        n_folds = max(1, total // min_oos_days - 1)
        if total // (n_folds + 1) < min_oos_days:
            return []
    fold_len = total // (n_folds + 1)
    folds: list[dict] = []
    oos_start = start + timedelta(days=fold_len)
    for i in range(n_folds):
        oos_end = end if i == n_folds - 1 else oos_start + timedelta(days=fold_len - 1)
        folds.append({
            "train_start": start,
            "train_end": oos_start - timedelta(days=1),
            "oos_start": oos_start,
            "oos_end": oos_end,
        })
        oos_start = oos_end + timedelta(days=1)
    return folds


def walk_forward_candidates(base_params: dict | None, cases: list[dict]) -> list[dict]:
    """候选 = baseline + 有界单参数邻域; 顺序确定性 (baseline 最先, 之后按 cases)。"""
    base = dict(base_params or {})
    candidates = [{"label": "baseline", "params": base, "perturbed_param": None}]
    for case in cases:
        params = dict(base)
        params[str(case["param"])] = case["value"]
        candidates.append({
            "label": f"{case['param']}={case['value']}",
            "params": params,
            "perturbed_param": str(case["param"]),
        })
    return candidates


def select_walk_forward_candidate(train_results: list, metric: str = "sharpe") -> int:
    """仅按训练期有限指标选候选索引; 平局按候选顺序稳定 tie-break (baseline 最先)。

    输入只有训练窗口结果 — OOS 指标不进入本函数, 结构上无法泄漏。全部候选
    训练指标不可计算 (None/NaN) 时确定性回退 baseline (索引 0)。
    """
    best_idx = 0
    best: float | None = None
    for idx, result in enumerate(train_results):
        score = _finite_stat_value(getattr(result, "stats", None), metric)
        if score is None:
            continue
        if best is None or score > best:
            best_idx, best = idx, score
    return best_idx


def stitch_oos_curves(fold_curves: list) -> list[dict]:
    """逐折首点归一后链式相乘拼接 OOS 净值; 空折/非有限首点折跳过, 不伪造。"""
    stitched: list[dict] = []
    level = 1.0
    for curve in fold_curves:
        points: list[tuple[str, float]] = []
        for point in curve or []:
            if not isinstance(point, dict) or point.get("date") is None:
                continue
            try:
                value = float(point.get("value"))
            except (TypeError, ValueError):
                continue
            if not bool(np.isfinite(value)):
                continue
            points.append((str(point["date"])[:10], value))
        if not points or points[0][1] <= 0:
            continue
        base = points[0][1]
        for d, value in points:
            stitched.append({"date": d, "value": round(level * value / base, 6)})
        level = stitched[-1]["value"]
    return stitched


def _normalized_curve(curve: list) -> list[dict]:
    """单折曲线首点归一; 首点缺失/非正/非有限时返回空, 不伪造。"""
    if not curve:
        return []
    try:
        first = float(curve[0].get("value"))
    except (AttributeError, TypeError, ValueError):
        return []
    if not bool(np.isfinite(first)) or first <= 0:
        return []
    out = []
    for point in curve:
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError):
            continue
        if not bool(np.isfinite(value)) or point.get("date") is None:
            continue
        out.append({"date": str(point["date"])[:10], "value": round(value / first, 6)})
    return out


def walk_forward_param_drift(folds: list[dict], base_params: dict | None) -> dict:
    """参数漂移: 各折选中参数组合数 + 相对基线发生过变化的参数逐折取值。"""
    base = dict(base_params or {})
    varying: set[str] = set()
    for fold in folds:
        for key, value in (fold.get("selected_params") or {}).items():
            if key not in base or base[key] != value:
                varying.add(str(key))
    labels = list(dict.fromkeys(str(f.get("selected_label")) for f in folds))
    return {
        "n_distinct_param_sets": len(labels),
        "distinct_labels": labels,
        "params": {
            key: [(f.get("selected_params") or {}).get(key) for f in folds]
            for key in sorted(varying)
        },
    }


def walk_forward_oos_summary(
    folds: list[dict],
    stitched_curve: list[dict],
    context: MetricContext = _DAILY_METRIC_CONTEXT,
    metric: str = "sharpe",
) -> dict:
    """OOS 汇总: 正收益折比例/最差折/平均退化 + 拼接曲线收益-Sharpe-回撤。
    不可计算的量输出 None, 不用 0 冒充。"""
    ok = [f for f in folds if not f.get("error")]
    oos_returns = [
        v for v in (_finite_stat_value(f.get("oos_stats"), "total_return") for f in ok)
        if v is not None
    ]
    degradations = [f["degradation"] for f in ok if f.get("degradation") is not None]
    oos_sharpe = None
    oos_total_return = None
    oos_max_drawdown = None
    if len(stitched_curve) >= 2:
        vals = np.asarray([float(p["value"]) for p in stitched_curve], dtype=float)
        if bool(np.isfinite(vals).all()) and vals[0] > 0:
            oos_total_return = float(vals[-1] / vals[0] - 1.0)
            oos_max_drawdown = float((vals / np.maximum.accumulate(vals) - 1.0).min())
            rets = vals[1:] / vals[:-1] - 1.0
            oos_sharpe = annualized_sharpe(rets, context)
    n_positive = sum(1 for v in oos_returns if v > 0)
    return {
        "metric": metric,
        "n_folds": len(ok),
        "positive_return_folds": n_positive,
        "positive_fold_ratio": round(n_positive / len(ok), 4) if ok else None,
        "worst_fold_return": round(min(oos_returns), 6) if oos_returns else None,
        "mean_oos_return": round(float(np.mean(oos_returns)), 6) if oos_returns else None,
        "mean_degradation": round(float(np.mean(degradations)), 4) if degradations else None,
        "oos_total_return": round(oos_total_return, 6) if oos_total_return is not None else None,
        "oos_sharpe": round(float(oos_sharpe), 4) if oos_sharpe is not None else None,
        "oos_max_drawdown": round(oos_max_drawdown, 6) if oos_max_drawdown is not None else None,
        "metric_context": context.to_dict(),
    }


def run_walk_forward(
    plan: list[dict],
    candidates: list[dict],
    run_fn,
    *,
    base_params: dict | None = None,
    metric: str = "sharpe",
    context: MetricContext = _DAILY_METRIC_CONTEXT,
) -> dict:
    """严格 Walk-Forward 编排 (纯函数, 回测执行注入)。

    每折: ① 在训练窗对全部候选运行并仅按训练期有限指标选出 winner;
    ② 冻结 winner 参数; ③ 仅用冻结参数在 OOS 窗运行一次。OOS 结果不参与
    任何选择。run_fn(start, end, params) -> 带 stats/equity_curve/error 的结果。
    """
    folds_out: list[dict] = []
    fold_curves: list[list] = []
    for spec in plan:
        train_results = [
            run_fn(spec["train_start"], spec["train_end"], params=candidate["params"])
            for candidate in candidates
        ]
        winner_idx = select_walk_forward_candidate(train_results, metric)
        winner = candidates[winner_idx]
        winner_train = train_results[winner_idx]
        oos_result = run_fn(spec["oos_start"], spec["oos_end"], params=winner["params"])
        oos_stats = getattr(oos_result, "stats", None) or {}
        oos_error = getattr(oos_result, "error", None)
        oos_curve_raw = list(getattr(oos_result, "equity_curve", None) or [])
        train_stats = getattr(winner_train, "stats", None) or {}
        degradation = None
        train_metric = _finite_stat_value(train_stats, metric)
        oos_metric = _finite_stat_value(oos_stats, metric)
        if train_metric is not None and oos_metric is not None:
            degradation = round(train_metric - oos_metric, 4)
        folds_out.append({
            "train_start": spec["train_start"].isoformat(),
            "train_end": spec["train_end"].isoformat(),
            "oos_start": spec["oos_start"].isoformat(),
            "oos_end": spec["oos_end"].isoformat(),
            "n_candidates": len(candidates),
            "selected_label": winner["label"],
            "selected_params": dict(winner["params"]),
            "train_stats": {key: train_stats.get(key) for key in _STATS_KEYS},
            "oos_stats": {key: oos_stats.get(key) for key in _STATS_KEYS},
            "degradation": degradation,
            "oos_curve": [] if oos_error else _normalized_curve(oos_curve_raw),
            "error": oos_error,
        })
        if not oos_error:
            fold_curves.append(oos_curve_raw)
    stitched = stitch_oos_curves(fold_curves)
    return {
        "scheme": "expanding_train",
        "selection_metric": metric,
        "candidate_space": WALK_FORWARD_CANDIDATE_SPACE,
        "n_candidates": len(candidates),
        "folds": folds_out,
        "stitched_curve": stitched,
        "summary": walk_forward_oos_summary(folds_out, stitched, context, metric),
        "param_drift": walk_forward_param_drift(folds_out, base_params),
        "warning": None,
    }


def empty_walk_forward(
    requested_n_folds: int,
    *,
    context: MetricContext = _DAILY_METRIC_CONTEXT,
    metric: str = "sharpe",
    min_oos_days: int = WALK_FORWARD_MIN_OOS_DAYS,
) -> dict:
    """区间不足时的结构化空结果 — 明确说明边界, 不伪造任何折。"""
    clamped = max(1, min(WALK_FORWARD_MAX_FOLDS, int(requested_n_folds)))
    return {
        "scheme": "expanding_train",
        "selection_metric": metric,
        "candidate_space": WALK_FORWARD_CANDIDATE_SPACE,
        "n_candidates": 0,
        "folds": [],
        "stitched_curve": [],
        "summary": walk_forward_oos_summary([], [], context, metric),
        "param_drift": walk_forward_param_drift([], None),
        "warning": (
            f"walk_forward: 请求区间不足以构成折 — 至少需要初始训练窗 + 1 个 "
            f"≥{min_oos_days} 天的 OOS 窗口 (请求 {clamped} 折), 已跳过 Walk-Forward"
        ),
    }

# ================================================================
# Walk-Forward 执行预算与开关 (API 层元数据; 纯 run_walk_forward 不感知预算)
# ================================================================

WALK_FORWARD_MAX_EXTRA_EXECUTIONS = 24
WALK_FORWARD_DISABLED_WARNING = (
    "walk_forward: 未启用 — 严格 Walk-Forward 会在每折训练窗对全部候选重复训练、"
    "冻结参数后再重跑样本外窗口, 回测次数远多于分段稳定性; "
    "需显式传 walk_forward_enabled=true 才会执行"
)


def walk_forward_planned_executions(n_folds: int, n_candidates: int) -> int:
    """额外回测执行数 = 折数 × (每折候选训练次数 + 1 次冻结后 OOS)。"""
    return max(0, int(n_folds)) * (max(0, int(n_candidates)) + 1)


def cap_walk_forward_candidates(
    candidates: list[dict],
    n_folds: int,
    *,
    max_extra_executions: int = WALK_FORWARD_MAX_EXTRA_EXECUTIONS,
) -> dict:
    """按执行预算确定性截断候选, 保证 planned ≤ max_extra_executions 恒成立。

    上界证明: effective = min(requested, ⌊budget/n_folds⌋ - 1) ⇒
    n_folds × (effective + 1) ≤ n_folds × ⌊budget/n_folds⌋ ≤ budget。
    截断保持确定性顺序 — baseline (索引 0) 永远保留, 之后按传入顺序取前序候选,
    这同时保住 select_walk_forward_candidate 全不可计算时回退 baseline 的路径。
    n_folds ≤ 0 (无折可跑) 时无任何执行, 无需截断; n_folds > budget 时连 baseline
    都放不下, effective 归 0 并输出 warning, 由调用方跳过运行而非伪造结果。
    """
    requested = len(candidates)
    folds = max(0, int(n_folds))
    if folds == 0:
        effective = requested
    else:
        effective = max(0, min(requested, max_extra_executions // folds - 1))
    planned = walk_forward_planned_executions(folds, effective)
    warning = None
    if folds > max_extra_executions and requested > 0:
        warning = (
            f"walk_forward: 折数 {folds} 连 baseline 候选都放不进执行预算 "
            f"{max_extra_executions}, 已跳过运行"
        )
    elif folds > 0 and effective < requested:
        warning = (
            f"walk_forward: 候选数 {requested} 超出执行预算, 已确定性截断为 {effective} 个 "
            f"(baseline + 前序候选), {folds} 折训练+OOS 共 {planned} 次回测 "
            f"≤ 上限 {max_extra_executions}"
        )
    return {
        "candidates": candidates[:effective],
        "requested_candidates": requested,
        "effective_candidates": effective,
        "max_executions": max_extra_executions,
        "planned_executions": planned,
        "warning": warning,
    }


def disabled_walk_forward(
    *,
    context: MetricContext = _DAILY_METRIC_CONTEXT,
    metric: str = "sharpe",
) -> dict:
    """未启用严格 Walk-Forward 时的结构化空块 — enabled=False + 明确 warning, 不伪造任何折。"""
    return {
        "enabled": False,
        "scheme": "expanding_train",
        "selection_metric": metric,
        "candidate_space": WALK_FORWARD_CANDIDATE_SPACE,
        "n_candidates": 0,
        "requested_candidates": 0,
        "effective_candidates": 0,
        "max_executions": WALK_FORWARD_MAX_EXTRA_EXECUTIONS,
        "folds": [],
        "stitched_curve": [],
        "summary": walk_forward_oos_summary([], [], context, metric),
        "param_drift": walk_forward_param_drift([], None),
        "warning": WALK_FORWARD_DISABLED_WARNING,
    }
