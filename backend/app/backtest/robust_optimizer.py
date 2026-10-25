"""稳健参数优化器: 多段互不重叠验证窗上的稳健分排名。

设计: docs/robust-optimizer-plan.md 第三节。核心转变 — 排名对象从"样本内得分"
改为"折外稳健分": R(p) = 0.6×median(oos₁…K) + 0.4×worst(oos₁…K), 一组参数只有在
多个互不重叠的验证段上都表现好才有资格当冠军。

阶段一 (本模块): 全网格 × K 个验证窗, 每窗独立回测 (参数固定不变)。
阶段二 (Top-N 全窗 IS + 邻域细化, 见 P2): 预留结果字段。

复用 optimizer.py 的网格展开/目标求值/方向推导与矩阵准备路径; 复用 walkforward
的"共享窗口准备 → 各段 run"纪律。运行环境与旧优化器一致 (独立 worker 进程)。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.backtest import guards
from app.backtest.optimizer import (
    default_direction,
    expand_param_grid,
    objective_value,
)

logger = logging.getLogger(__name__)

# 验证段数允许范围: 2 段已可算中位/最差, 上限 6 防过度切碎 (每段统计噪声放大)。
K_SEGMENTS_MIN = 2
K_SEGMENTS_MAX = 6
# 每个验证段的最短自然日数: 短于此的段统计意义不足, 直接拒绝切窗。
_MIN_SEGMENT_DAYS = 14


def split_validation_windows(start: date, end: date, k: int) -> list[tuple[date, date]]:
    """区间等分为 k 个互不重叠的验证段 [V₁…V_k] (按自然日, 余数摊给前面的段)。

    段内交易日密度由回测层自行处理; 此处只做日历切分与合法性校验。
    """
    if not (K_SEGMENTS_MIN <= k <= K_SEGMENTS_MAX):
        raise ValueError(f"验证段数必须在 {K_SEGMENTS_MIN}-{K_SEGMENTS_MAX} 之间 (当前 {k})")
    span = (end - start).days + 1
    if span < k * _MIN_SEGMENT_DAYS:
        raise ValueError(
            f"区间太短: {span} 自然日切不出 {k} 个 ≥{_MIN_SEGMENT_DAYS} 天的验证段; "
            f"请扩大区间或减少段数"
        )
    windows: list[tuple[date, date]] = []
    base, rem = divmod(span, k)
    cursor = start
    for i in range(k):
        length = base + (1 if i < rem else 0)
        seg_end = cursor + timedelta(days=length - 1)
        windows.append((cursor, seg_end))
        cursor = seg_end + timedelta(days=1)
    return windows


def robust_score(values: list[float | None]) -> dict:
    """K 段得分 → 稳健分组成。None/非有限值按 -inf 计入最差与中位 (诚实降级)。

    R = 0.6×median + 0.4×worst — 中位数扛单段运气, 最差段惩罚"只在一段行情好用"。
    spread (max-min, 归一空间) 是过拟合警报: 段间离散本身即不稳定信号。
    """
    finite = [v for v in values if v is not None and v == v and abs(v) != float("inf")]
    worst = min(
        (v for v in values if v is not None and v == v and abs(v) != float("inf")),
        default=float("-inf"),
    )
    if not finite:
        return {"median": None, "worst": None, "spread": None, "score": float("-inf")}
    n = len(finite)
    s = sorted(finite)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    spread = s[-1] - s[0]
    score = 0.6 * median + 0.4 * worst
    return {"median": median, "worst": worst, "spread": spread, "score": score}


def champion_convergence(
    rows: list[dict],
    k: int,
    direction: str,
    objective: str,
) -> dict:
    """各段冠军收敛度诊断 (免费: 阶段一已持有"每组×每段"得分矩阵)。

    各验证段的 argmax 参数组收敛于同一组 → 参数可信; 散落 → 策略本身参数不稳定,
    任何"最优"都别信。unanimous = 全部段冠军同组; majority > k/2 段同组; scattered。
    """
    def _key(params: dict) -> str:
        return "|".join(f"{k}={params[k]}" for k in sorted(params))

    champions: list[dict | None] = [None] * k
    for row in rows:
        if row.get("disqualified") or row.get("error"):
            continue
        for i, v in enumerate(row.get("oos_values", [])):
            if v is None or i >= k:
                continue
            cur = champions[i]
            if cur is None or _norm(v, direction) > _norm(cur["_v"], direction):
                champions[i] = {"params": row["params"], "_v": v}
    keys = [_key(c["params"]) for c in champions if c is not None]
    if not keys:
        return {"label": "no_data", "champions": [], "max_count": 0, "k": k}
    max_count = max(keys.count(x) for x in set(keys))
    label = "unanimous" if max_count == len(keys) and len(keys) == k else (
        "majority" if max_count > k / 2 else "scattered"
    )
    return {
        "label": label,
        "champions": [
            {"segment": i, "params": c["params"], "objective_raw": c["_v"]}
            for i, c in enumerate(champions) if c is not None
        ],
        "max_count": max_count,
        "k": k,
        "objective": objective,
    }


def _norm(v: float, direction: str) -> float:
    return -v if direction == "min" else v


def neighborhood_plateau(
    rows: list[dict],
    best_row: dict,
    tol_pct: float = 0.05,
) -> dict | None:
    """冠军网格邻域的高原判据 (数据来自阶段一得分矩阵, 零额外回测)。

    邻居 = 恰好一个参数沿网格移动一步的组合。得分衰减 ≤ tol 的邻居占比高 → 冠军
    落在参数高原上, 参数选择不敏感; 邻居全部大幅衰减 → 孤峰, 过拟合警报。
    tol 以"冠军得分绝对值与全体合格分极差中的较大者"为尺度, 对负值目标 (回撤类)
    同样成立。邻居全为错误/除名/缺分时无从判高原 → isolated。
    """
    best_params = best_row.get("params") or {}
    best_score = best_row.get("score")
    if not best_params or best_score is None or best_score == float("-inf"):
        return None

    def _key(params: dict) -> tuple:
        return tuple(sorted(params.items()))

    by_key = {_key(r.get("params") or {}): r for r in rows}
    # 网格候选值顺序: rows 来自 expand_param_grid 的笛卡尔积序, 首次出现序即候选序
    order: dict[str, list] = {}
    for r in rows:
        for p, v in (r.get("params") or {}).items():
            vals = order.setdefault(p, [])
            if v not in vals:
                vals.append(v)

    neighbors: list[dict] = []
    for p, vals in order.items():
        if p not in best_params or len(vals) < 2:
            continue
        i = vals.index(best_params[p])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                cand = dict(best_params)
                cand[p] = vals[j]
                r = by_key.get(_key(cand))
                if r is None:
                    continue
                entry = {
                    "params": cand,
                    "score": r.get("score"),
                    "disqualified": bool(r.get("disqualified")),
                    "error": r.get("error"),
                }
                if (
                    entry["score"] is not None
                    and entry["score"] != float("-inf")
                    and not entry["disqualified"]
                    and not entry["error"]
                ):
                    entry["decay"] = best_score - entry["score"]
                neighbors.append(entry)

    valid = [n for n in neighbors if "decay" in n]
    if not valid:
        return {
            "label": "isolated", "ratio": None, "neighbors": neighbors,
            "tolerance": None, "n_valid": 0,
        }
    finite_scores = [
        r.get("score") for r in rows
        if r.get("score") is not None and r.get("score") != float("-inf")
    ]
    scale = max(abs(best_score), max(finite_scores) - min(finite_scores))
    tol = tol_pct * scale
    within = sum(1 for n in valid if n["decay"] <= tol)
    ratio = within / len(valid)
    label = "plateau" if ratio >= 0.6 else ("slope" if ratio > 0 else "peak")
    return {
        "label": label, "ratio": round(ratio, 4), "neighbors": neighbors,
        "tolerance": round(tol, 6), "n_valid": len(valid),
    }


@dataclass
class RobustOptimizeConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    direction: str | None = None
    k_segments: int = 4
    min_trades_total: int = 30   # K 段合计交易数门槛 (<=0 关闭)
    stage2_top_n: int = 20       # 阶段二全窗 IS 细化名额 (<=0 关闭)
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)
    matrix_cache_max_mb: int = 512


class RobustOptimizeService:
    """与 StrategyOptimizer 同构 (service + strategy_engine), 供 worker 构造。"""

    def __init__(self, service, strategy_engine) -> None:
        self.service = service
        self.strategy_engine = strategy_engine

    def robust_optimize(
        self,
        cfg: RobustOptimizeConfig,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        from app.backtest.optimizer import _validate_backtest_kwargs
        from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

        t0 = time.perf_counter()
        guards.validate_primary_objective(cfg.objective)
        direction = cfg.direction or default_direction(cfg.objective)
        _validate_backtest_kwargs(cfg.backtest_kwargs)
        if int(cfg.matrix_cache_max_mb) <= 0:
            raise ValueError("matrix_cache_max_mb 必须为正整数")

        s = self.strategy_engine.get(cfg.strategy_id)  # 可能抛 ValueError
        params_meta = s.meta.get("params", [])
        combos = expand_param_grid(params_meta, cfg.param_grid)
        if not combos:
            raise ValueError("参数网格为空或无有效组合")
        windows = split_validation_windows(cfg.start, cfg.end, cfg.k_segments)
        n_total = len(combos)

        trial_policy = BacktestResultPolicy.optimizer_trial(cfg.objective)

        # 共享矩阵准备 (与 walkforward 同一纪律): matrix_native 后端先造覆盖全区间的
        # 超集矩阵; 各验证段经 market_data_override 取只读时间视图, 不重复扫盘编矩阵。
        # prepare_matrix_optimization 要求全部 trial 同区间 —— 不能直接喂各段 config。
        prepared = None
        if getattr(s, "execution_backend", "polars_expr") == "matrix_native":
            prepare_started = time.perf_counter()
            full_span_configs = [
                StrategyBacktestConfig(
                    strategy_id=cfg.strategy_id,
                    symbols=cfg.symbols,
                    start=cfg.start,
                    end=cfg.end,
                    params={**cfg.base_params, **combos[0]},
                    overrides=cfg.overrides,
                    **cfg.backtest_kwargs,
                )
            ]
            prepared = self.service.prepare_matrix_optimization(
                full_span_configs,
                matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
            )
            if progress_cb is not None:
                progress_cb({
                    "type": "robust_optimizer_prepare",
                    "done": 0,
                    "total": n_total,
                    "prepare_ms": round((time.perf_counter() - prepare_started) * 1000, 1),
                })

        # 段级只读视图只建 K 次 (视图与参数无关), 全组合复用 —— 逐段重建会付出
        # 组合数×K 次签名/上下文校验的固定开销 (126 组合×4 段实测约占总耗时一半)。
        # 单窗视图准备失败只让该段走通用路径 (慢而正确), 不中断整体。
        seg_views: list | None = None
        if prepared is not None:
            seg_views = []
            for ws, we in windows:
                seg_cfg = StrategyBacktestConfig(
                    strategy_id=cfg.strategy_id,
                    symbols=cfg.symbols,
                    start=ws,
                    end=we,
                    params={**cfg.base_params, **combos[0]},
                    overrides=cfg.overrides,
                    **cfg.backtest_kwargs,
                )
                try:
                    seg_views.append(self.service.prepare_matrix_optimization(
                        [seg_cfg],
                        matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
                        market_data_override=prepared.market_data,
                    ))
                except Exception as e:  # noqa: BLE001
                    logger.warning("稳健优化 段 %s~%s 视图准备失败, 该段走通用路径: %r", ws, we, e)
                    seg_views.append(None)

        rows: list[dict] = []
        best_score: float | None = None
        cancelled = False

        for idx, combo in enumerate(combos):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            oos_values: list[float | None] = []
            seg_stats: list[dict] = []
            trades_total = 0
            error: str | None = None
            for wi, (ws, we) in enumerate(windows):
                bt_cfg = StrategyBacktestConfig(
                    strategy_id=cfg.strategy_id,
                    symbols=cfg.symbols,
                    start=ws,
                    end=we,
                    params={**cfg.base_params, **combo},
                    overrides=cfg.overrides,
                    **cfg.backtest_kwargs,
                )
                # 单段异常隔离: 一段失败不拖垮整组, 该组记 error 参与展示但不得冠军
                try:
                    kwargs = {"result_policy": trial_policy}
                    if seg_views is not None:
                        kwargs["prepared"] = seg_views[wi]
                    res = self.service.run(bt_cfg, cancel_event=cancel_event, **kwargs)
                except Exception as e:  # noqa: BLE001
                    logger.warning("稳健优化 参数组 %s 段 %s~%s 回测异常: %r", combo, ws, we, e)
                    error = repr(e)
                    break
                if res.error:
                    error = res.error
                    break
                raw = res.stats.get(cfg.objective)
                oos_values.append(
                    None if raw is None else objective_value(res.stats, cfg.objective, direction)
                )
                trades_total += int(res.stats.get("n_trades") or 0)
                seg_stats.append({
                    "start": ws.isoformat(),
                    "end": we.isoformat(),
                    "objective_raw": raw,
                    "n_trades": res.stats.get("n_trades"),
                    "total_return": res.stats.get("total_return"),
                    "sharpe": res.stats.get("sharpe"),
                    "max_drawdown": res.stats.get("max_drawdown"),
                })

            if error is not None:
                rows.append({
                    "params": combo, "error": error,
                    "oos_values": oos_values, "segments": seg_stats,
                    "trades_total": trades_total, "disqualified": False,
                })
            else:
                sc = robust_score(oos_values)
                qualified = guards.trades_ok({"n_trades": trades_total}, cfg.min_trades_total)
                rows.append({
                    "params": combo,
                    "oos_values": oos_values,
                    "segments": seg_stats,
                    "trades_total": trades_total,
                    "disqualified": not qualified,
                    "disqualify_reason": None if qualified else (
                        f"K 段合计交易数 {trades_total} < {cfg.min_trades_total}"
                    ),
                    **sc,
                })
                if qualified and (best_score is None or sc["score"] > best_score):
                    best_score = sc["score"]

            if progress_cb is not None:
                progress_cb({
                    "type": "robust_optimizer_progress",
                    "done": idx + 1,
                    "total": n_total,
                    "best_score": best_score,
                })

        # 排序: 合格组按稳健分降序 → 未达标组 → 错误组 (透明垫底, 不静默剔除)
        def _tier(r: dict) -> int:
            if r.get("error"):
                return 2
            if r.get("disqualified"):
                return 1
            return 0

        def _score(r: dict) -> float:
            return float(r.get("score") or float("-inf"))

        ordered = (
            sorted([r for r in rows if _tier(r) == 0], key=_score, reverse=True)
            + sorted([r for r in rows if _tier(r) == 1], key=_score, reverse=True)
            + [r for r in rows if _tier(r) == 2]
        )
        for rank, r in enumerate(ordered, start=1):
            r["rank"] = rank

        # ---- 阶段二: Top-N 全窗 IS 细化 (仅诊断, 不改排名) ----
        # 全窗 = [S,E] 整段回测一次, 参数与阶段一完全相同 → IS 得分 vs 稳健分的差
        # 就是"样本内乐观偏差"。超集矩阵本就覆盖全窗, 直接复用 (零额外准备)。
        stage2_evaluated = 0
        top_rows: list[dict] = []
        if int(cfg.stage2_top_n) > 0 and not cancelled:
            top_rows = [
                r for r in ordered
                if not r.get("error") and not r.get("disqualified")
            ][: int(cfg.stage2_top_n)]
        for i, row in enumerate(top_rows):
            if cancel_event is not None and cancel_event.is_set():
                break
            full_cfg = StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=cfg.start,
                end=cfg.end,
                params={**cfg.base_params, **row["params"]},
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            try:
                kwargs = {"result_policy": trial_policy}
                if prepared is not None:
                    kwargs["prepared"] = prepared
                res = self.service.run(full_cfg, cancel_event=cancel_event, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "稳健优化 阶段二 参数组 %s 全窗回测异常: %r", row["params"], e,
                )
                row["is_full"] = {"error": repr(e)}
            else:
                if res.error:
                    row["is_full"] = {"error": res.error}
                else:
                    raw = res.stats.get(cfg.objective)
                    is_norm = (
                        None if raw is None
                        else objective_value(res.stats, cfg.objective, direction)
                    )
                    row["is_full"] = {
                        "objective_raw": raw,
                        "score_norm": is_norm,
                        "n_trades": res.stats.get("n_trades"),
                        "total_return": res.stats.get("total_return"),
                    }
                    if is_norm is not None and row.get("score") is not None:
                        deg = is_norm - row["score"]
                        row["degradation"] = deg
                        row["degradation_pct"] = (
                            deg / abs(is_norm) if is_norm != 0 else None
                        )
            stage2_evaluated += 1
            if progress_cb is not None:
                progress_cb({
                    "type": "robust_optimizer_stage2",
                    "done": i + 1,
                    "total": len(top_rows),
                })

        # 矩阵生命周期到此结束 (段视图被全组合复用, 统一在此关闭)
        if seg_views is not None:
            for v in seg_views:
                if v is not None:
                    v.compute_cache.close()
        if prepared is not None:
            prepared.compute_cache.close()

        best_row = next((r for r in ordered if not r.get("error") and not r.get("disqualified")), None)
        convergence = champion_convergence(
            [r for r in ordered], cfg.k_segments, direction, cfg.objective,
        )
        # 高原判据: 冠军网格邻域的得分衰减 (阶段一数据, 零额外回测)
        plateau = neighborhood_plateau(ordered, best_row) if best_row is not None else None

        return {
            "objective": cfg.objective,
            "direction": direction,
            "k_segments": cfg.k_segments,
            "windows": [{"start": ws.isoformat(), "end": we.isoformat()} for ws, we in windows],
            "n_combinations": n_total,
            "n_completed": sum(1 for r in rows if not r.get("error")),
            "min_trades_total": cfg.min_trades_total,
            "best_params": best_row["params"] if best_row else None,
            "best_score": best_row["score"] if best_row else None,
            "best_row_detail": best_row,
            "convergence": convergence,
            "plateau": plateau,
            "stage2": {"top_n": int(cfg.stage2_top_n), "evaluated": stage2_evaluated},
            "cancelled": cancelled,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "results": ordered,
        }
