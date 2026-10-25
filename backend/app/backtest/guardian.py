"""策略守护纯逻辑: 退化判定、复验窗口规划与档案记录形状。

与挖掘 (mining) 的分工: 挖掘用嵌套样本内/外验证选冠军; 守护只在"发布/入护
基线之后的新数据"上复验, 结果绝不反馈进取参加权 (逐层 OOS 纪律, 设计见
docs/auto-strategy-lab-plan.md 第三节)。本模块不做任何 IO, 判定全部为纯函数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.backtest.guards import PRIMARY_OBJECTIVES

GUARDIAN_ALGORITHM_VERSION = "guardian-v1"
GUARDIAN_STATUSES: frozenset[str] = frozenset(
    {"pending", "healthy", "watch", "retired"}
)
BASELINE_KINDS: frozenset[str] = frozenset({"mining_gate", "trailing_reference"})

# 新数据不足时跳过本轮复验: 太短的窗口切出的稳健分只是噪声, 判定只会制造假告警。
MIN_NEW_TRADING_DAYS = 30
MIN_SEGMENT_DAYS = 14  # 与稳健优化器同口径 (自然日)
K_SEGMENTS_MIN = 2
K_SEGMENTS_MAX = 4
MAX_CHECKS = 52

# 回撤类目标本身为负值 (最大化带符号值 = 回撤越小越好), 软底线不同。
_DRAWDOWN_OBJECTIVES = frozenset({"max_drawdown", "mc_maxdd_p50", "mc_maxdd_p95"})
_TRADES_RATIO_SOFT = 1.0 / 3.0
_TRADES_RATIO_HARD = 0.1
_TRADES_DROUGHT_ABS = 3


@dataclass(frozen=True)
class GuardianThresholds:
    """守护判定阈值。watch/retire 为相对基线退化的百分比 (0-1 小数)。"""

    watch_pct: float = 0.20
    retire_pct: float = 0.50
    max_soft_drawdown: float = -0.30
    k_max: int = 4
    min_new_trading_days: int = MIN_NEW_TRADING_DAYS

    def __post_init__(self) -> None:
        if not 0.0 < self.watch_pct < self.retire_pct <= 1.0:
            raise ValueError(
                "守护阈值必须满足 0 < watch_pct < retire_pct <= 1 "
                f"(当前 {self.watch_pct}/{self.retire_pct})"
            )
        if not -1.0 <= self.max_soft_drawdown <= 0.0:
            raise ValueError("max_soft_drawdown 必须在 [-1, 0] (回撤为负值)")
        if not K_SEGMENTS_MIN <= self.k_max <= K_SEGMENTS_MAX:
            raise ValueError(f"k_max 必须在 {K_SEGMENTS_MIN}-{K_SEGMENTS_MAX} 之间")
        if self.min_new_trading_days < 1:
            raise ValueError("min_new_trading_days 必须为正")


@dataclass(frozen=True)
class GuardianVerdict:
    status: str  # healthy | watch | retired
    reasons: tuple[str, ...] = ()
    degradation_pct: float | None = None
    incomplete_data: bool = False


def objective_soft_floor(objective: str) -> float:
    """目标得分的软底线: 回撤类目标为 max_soft_drawdown 口径, 其余为 0。"""
    if objective not in PRIMARY_OBJECTIVES:
        raise ValueError(
            f"守护目标必须在稳健优化白名单内: {sorted(PRIMARY_OBJECTIVES)}"
        )
    return -0.30 if objective in _DRAWDOWN_OBJECTIVES else 0.0


def degradation_pct(baseline_score: float, current_score: float) -> float | None:
    """方向感知的相对退化 (正数=退化)。白名单目标全部为 max 方向。

    基线 ≤0 时相对退化在数学上失真 (除以接近 0 的数会爆炸), 返回 None —
    此时判定退化为只看绝对底线, UI 显示 "—"。改进视为 0 退化。
    """
    if baseline_score <= 0.0:
        return None
    pct = (baseline_score - current_score) / baseline_score
    return max(0.0, pct)


def evaluate_check(
    *,
    objective: str,
    baseline_score: float,
    baseline_n_trades: int,
    current_score: float | None,
    current_n_trades: int,
    worst_drawdown: float | None,
    thresholds: GuardianThresholds,
    incomplete_data: bool = False,
    missing_segments: int = 0,
) -> GuardianVerdict:
    """一次复验 → 三态判定。

    incomplete_data=True (存在段落回测失败/得分缺失) 时最多只降到 watch:
    数据残缺时按最差段假设 (robust_score 把缺失计 -inf) 直接退役过于激进,
    宁可连续观察; 退役只在数据完整的复验里发生 (含交易数枯竭判定 — 段落
    失败的交易数按 0 计入, 残缺时的"枯竭"可能是数据问题而非策略失效)。
    """
    reasons: list[str] = []
    soft_floor = objective_soft_floor(objective)
    if missing_segments > 0:
        reasons.append(f"{missing_segments} 个验证段回测失败, 退化程度无法可靠估计")

    pct: float | None = None
    if current_score is not None:
        pct = degradation_pct(baseline_score, current_score)
        if pct is not None and pct > thresholds.retire_pct:
            reasons.append(f"较基线退化 {pct:.0%} (超过 {thresholds.retire_pct:.0%})")
        elif pct is not None and pct > thresholds.watch_pct:
            reasons.append(f"较基线退化 {pct:.0%} (超过 {thresholds.watch_pct:.0%})")

    hard_drought = False
    if baseline_n_trades > 0:
        drought_floor = max(
            _TRADES_DROUGHT_ABS, int(baseline_n_trades * _TRADES_RATIO_HARD)
        )
        if current_n_trades < drought_floor:
            hard_drought = True
            reasons.append(
                f"交易数枯竭: 本轮 {current_n_trades} < 基线 {baseline_n_trades} 的硬底线 {drought_floor}"
            )

    if not hard_drought:
        if current_score is not None and current_score < soft_floor:
            reasons.append(f"{objective}={current_score:.3f} 击穿软底线 {soft_floor}")
        if (
            worst_drawdown is not None
            and worst_drawdown < thresholds.max_soft_drawdown
        ):
            reasons.append(
                f"最差段回撤 {worst_drawdown:.1%} 差于 {thresholds.max_soft_drawdown:.0%}"
            )
        if baseline_n_trades > 0:
            soft_floor_trades = int(baseline_n_trades * _TRADES_RATIO_SOFT)
            if current_n_trades < soft_floor_trades:
                reasons.append(
                    f"交易数 {current_n_trades} 低于基线 {baseline_n_trades} 的 1/3"
                )

    if not reasons:
        return GuardianVerdict("healthy", (), pct, incomplete_data)
    if not incomplete_data and (
        hard_drought or (pct is not None and pct > thresholds.retire_pct)
    ):
        return GuardianVerdict("retired", tuple(reasons), pct, incomplete_data)
    return GuardianVerdict("watch", tuple(reasons), pct, incomplete_data)


def plan_new_data_window(
    partitions: list[date],
    data_as_of: date,
    *,
    min_new_trading_days: int = MIN_NEW_TRADING_DAYS,
) -> tuple[date, date] | None:
    """基线之后的新交易日窗口; 不足 min_new_trading_days 个则返回 None (跳过)。"""
    fresh = [value for value in partitions if value > data_as_of]
    if len(fresh) < min_new_trading_days:
        return None
    return fresh[0], fresh[-1]


def choose_k(start: date, end: date, k_max: int) -> int:
    """窗口能切出的最大段数 (≥2), 切不出则抛 ValueError (调用方跳过该策略)。"""
    if not K_SEGMENTS_MIN <= k_max <= K_SEGMENTS_MAX:
        raise ValueError(f"k_max 必须在 {K_SEGMENTS_MIN}-{K_SEGMENTS_MAX} 之间")
    span = (end - start).days + 1
    for k in range(min(k_max, K_SEGMENTS_MAX), K_SEGMENTS_MIN - 1, -1):
        if span >= k * MIN_SEGMENT_DAYS:
            return k
    raise ValueError(
        f"新数据自然日跨度 {span} 天切不出 ≥{MIN_SEGMENT_DAYS} 天的 {K_SEGMENTS_MIN} 段验证窗口"
    )


def split_windows(start: date, end: date, k: int) -> list[tuple[date, date]]:
    """等分复验窗口 — 与稳健优化器 split_validation_windows 同算法。

    不直接复用是为了让纯逻辑模块不依赖服务层语义; 两者必须保持一致
    (见 tests 断言与 robust_optimizer 的对齐用例)。
    """
    span = (end - start).days + 1
    if span < k * MIN_SEGMENT_DAYS:
        raise ValueError("区间太短, 切不出等长验证段")
    windows: list[tuple[date, date]] = []
    base, rem = divmod(span, k)
    cursor = start
    for i in range(k):
        length = base + (1 if i < rem else 0)
        seg_end = cursor + timedelta(days=length - 1)
        windows.append((cursor, seg_end))
        cursor = seg_end + timedelta(days=1)
    return windows


def consecutive_watch(checks: list[Mapping[str, Any]]) -> int:
    """末尾连续 watch 判定数 (连续 ≥2 次在 overview 标记告警)。"""
    count = 0
    for check in reversed(checks):
        if check.get("verdict") == "watch" and not check.get("error"):
            count += 1
        else:
            break
    return count


@dataclass(frozen=True)
class CheckSummary:
    """一次复验的归约结果 — runtime 计算后交给 evaluate_check 与落档。"""

    objective: str
    segment_scores: list[float | None]
    segment_trades: list[int | None]
    robust: Mapping[str, Any]
    n_trades_total: int
    worst_drawdown: float | None
    incomplete_data: bool = False
    error: str | None = None
