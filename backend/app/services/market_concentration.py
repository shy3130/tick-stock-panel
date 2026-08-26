"""市场抱团/拥挤度状态 — 做T适用性研究的市场轴(严格 T-1, 可复算)。

设计契约:
  - 纯计算/仓储编排分层: 只依赖 repo.get_enriched_range(canonical enriched
    日线, raw_close 口径)与 screener_financials 的 PIT 公告日行业事件,
    不接 ext_data 行业映射、不发 HTTP、无外部 fallback、不写行情 data。
  - 严格 T-1: target_date 只用于定位上一交易日 signal_date, 状态只使用
    signal_date 及更早的数据; target 当日数据永不进入计算。
  - fail-closed: 覆盖不足/平滑窗口不足/历史校准不足/零正贡献/指标非有限
    一律 unavailable, 绝不放行为 dispersed。仓储读取失败抛
    MarketStateDataError(API 层转 503), 错误消息不含本地路径。
  - 复现边界: 这里实现的是公开可推导的公式化口径, 不声称复现任何
    未公开原始公式(methodology.hidden_formula_replicated 恒为 false)。

模块导入无副作用: app.* 依赖全部在函数体内延迟导入。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── 方法论常量(契约逐字锁定) ────────────────────────────────────
METHODOLOGY_ID = "market_concentration_v1"
METHODOLOGY_VERSION = 1
SMOOTHING_DAYS = 5
CALIBRATION_DAYS = 252
MIN_CALIBRATION_DAYS = 120
T_LAG = 1

# 最小覆盖门槛(任一不满足 → 该交易日无效)
MIN_STOCK_COUNT = 1000
MIN_INDUSTRY_COUNT = 20
MIN_SYMBOL_COVERAGE = 0.9
MIN_TURNOVER_COVERAGE = 0.95
MIN_OBSERVATION_SESSIONS = 60

# 分类阈值(作用于经验百分位, 非原始值)
CONCENTRATED_TURNOVER_PCT = 0.8
CONCENTRATED_POSITIVE_PCT = 0.8
CONCENTRATED_TOP3_PCT = 0.8
DISPERSED_TURNOVER_PCT = 0.4
DISPERSED_POSITIVE_PCT = 0.4
DISPERSED_TOP3_PCT = 0.4
DISPERSED_RETURN_STD_PCT = 0.5
# 冷路径只拉最小可用窗口：60 日观察期 + 至少 120 个平滑校准点 + 当前 5 日。
# 320 个自然日通常覆盖 220+ 个交易日；若有效日不足则 fail-closed，不扩大扫描。
HISTORY_CALENDAR_DAYS = 320
FETCH_TRADING_DAYS = 200

_FORMULAS: dict[str, str] = {
    "return": "close-to-close pct change on raw_close; daily cross-section vs prior trading day",
    "return_std": "cross-sectional population std (ddof=0) of daily returns",
    "return_q90_q10": "cross-sectional quantile spread Q90-Q10 of daily returns (linear interpolation)",
    "turnover_share": "PIT industry amount / mapped amount of the day's eligible universe",
    "normalized_hhi": "(sum(share^2)-1/K)/(1-1/K) over K shares summing to 1",
    "industry_return": "median of member daily returns per PIT industry",
    "positive_contribution": (
        "industry turnover share * max(industry return - market median return, 0), "
        "normalized to sum 1"
    ),
    "smoothing": "current value = median of the last 5 trading days of each daily metric",
    "percentile": (
        "inclusive empirical CDF: share of strictly-prior valid-day values <= current value, "
        "over the most recent <=252 valid days"
    ),
    "classification": (
        "concentrated iff pct(turnover_hhi)>=0.8 and (pct(positive_return_hhi)>=0.8 "
        "or pct(top3_contribution)>=0.8); dispersed iff pct(turnover_hhi)<=0.4 and "
        "pct(positive_return_hhi)<=0.4 and pct(top3_contribution)<=0.4 and "
        "pct(return_std)>=0.5; otherwise transition"
    ),
}

_METRIC_KEYS = (
    "return_std",
    "return_q90_q10",
    "turnover_hhi",
    "positive_return_hhi",
    "top3_contribution",
    "top5_contribution",
)
_PERCENTILE_KEYS = (
    "return_std",
    "turnover_hhi",
    "positive_return_hhi",
    "top3_contribution",
)


class MarketStateDataError(RuntimeError):
    """仓储读取失败的脱敏信号(API 层转 503)。消息不得包含本地路径。"""


# ── 快照契约模型(与前端共享, 字段逐字锁定) ─────────────────────
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MarketStateMethodology(_StrictModel):
    id: Literal["market_concentration_v1"] = METHODOLOGY_ID
    version: Literal[1] = METHODOLOGY_VERSION
    smoothing_days: Literal[5] = SMOOTHING_DAYS
    calibration_days: Literal[252] = CALIBRATION_DAYS
    min_calibration_days: Literal[120] = MIN_CALIBRATION_DAYS
    t_lag: Literal[1] = T_LAG
    hidden_formula_replicated: Literal[False] = False
    formulas: dict[str, str] = Field(default_factory=lambda: dict(_FORMULAS))


class MarketStateMetrics(_StrictModel):
    return_std: float | None = None
    return_q90_q10: float | None = None
    turnover_hhi: float | None = None
    positive_return_hhi: float | None = None
    top3_contribution: float | None = None
    top5_contribution: float | None = None


class MarketStatePercentiles(_StrictModel):
    return_std: float | None = None
    turnover_hhi: float | None = None
    positive_return_hhi: float | None = None
    top3_contribution: float | None = None


class MarketStateCoverage(_StrictModel):
    stock_count: int = Field(default=0, ge=0)
    industry_count: int = Field(default=0, ge=0)
    symbol_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    turnover_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    calibration_days: int = Field(default=0, ge=0)


class MarketStateGates(_StrictModel):
    automatic_research_allowed: bool = False
    reasons: list[str] = Field(default_factory=list)


class MarketStateSource(_StrictModel):
    daily: Literal["canonical_enriched"] = "canonical_enriched"
    industry: Literal["pit_financial_snapshot"] = "pit_financial_snapshot"
    adjustment: Literal["raw_close"] = "raw_close"
    external_fallback: Literal[False] = False


class MarketStateSnapshot(_StrictModel):
    available: bool
    state: Literal["concentrated", "transition", "dispersed", "unavailable"]
    target_date: str
    signal_date: str | None = None
    methodology: MarketStateMethodology = Field(default_factory=MarketStateMethodology)
    metrics: MarketStateMetrics = Field(default_factory=MarketStateMetrics)
    percentiles: MarketStatePercentiles = Field(default_factory=MarketStatePercentiles)
    coverage: MarketStateCoverage = Field(default_factory=MarketStateCoverage)
    gates: MarketStateGates = Field(default_factory=MarketStateGates)
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    source: MarketStateSource = Field(default_factory=MarketStateSource)

    @model_validator(mode="after")
    def _validate_invariants(self) -> "MarketStateSnapshot":
        try:
            target = date.fromisoformat(self.target_date)
            signal = date.fromisoformat(self.signal_date) if self.signal_date else None
        except ValueError as exc:
            raise ValueError("invalid market state date") from exc
        if signal is not None and signal >= target:
            raise ValueError("signal_date must be strictly before target_date")
        if self.available and signal is None:
            raise ValueError("available market state requires signal_date")
        if self.available != (self.state != "unavailable"):
            raise ValueError("available/state mismatch")
        allowed = self.available and self.state == "dispersed"
        if self.gates.automatic_research_allowed != allowed:
            raise ValueError("market state gate mismatch")
        if self.available and (
            any(value is None for value in self.metrics.model_dump().values())
            or any(value is None for value in self.percentiles.model_dump().values())
        ):
            raise ValueError("available market state requires complete metrics")
        if self.available and (
            self.coverage.stock_count < MIN_STOCK_COUNT
            or self.coverage.industry_count < MIN_INDUSTRY_COUNT
            or self.coverage.symbol_coverage is None
            or self.coverage.symbol_coverage < MIN_SYMBOL_COVERAGE
            or self.coverage.turnover_coverage is None
            or self.coverage.turnover_coverage < MIN_TURNOVER_COVERAGE
            or self.coverage.calibration_days < MIN_CALIBRATION_DAYS
        ):
            raise ValueError("available market state requires minimum coverage")
        for value in self.percentiles.model_dump().values():
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("percentile outside [0,1]")
        for value in (self.coverage.symbol_coverage, self.coverage.turnover_coverage):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("coverage outside [0,1]")
        return self


# ── 纯计算函数(测试直接复算) ────────────────────────────────────
def _finite(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def quantile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数(与 numpy 默认口径一致); 输入必须已升序。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    h = (n - 1) * q
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return float(sorted_values[lo])
    return float(sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo]))


def median(values: list[float]) -> float | None:
    return quantile(sorted(values), 0.5)


def cross_section_std(values: list[float]) -> float | None:
    """截面总体标准差(ddof=0); 少于 2 个值 → None。"""
    nums = sorted(float(v) for v in values if _finite(v) is not None)
    n = len(nums)
    if n < 2:
        return None
    mean = sum(nums) / n
    return math.sqrt(sum((v - mean) ** 2 for v in nums) / n)


def cross_section_q90_q10(values: list[float]) -> float | None:
    nums = sorted(float(v) for v in values if _finite(v) is not None)
    q90 = quantile(nums, 0.9)
    q10 = quantile(nums, 0.1)
    if q90 is None or q10 is None:
        return None
    return q90 - q10


def normalized_hhi(values: list[float]) -> float | None:
    """归一化 HHI = (Σw²−1/K)/(1−1/K), w=share 归一化到 1; K<2 或 Σ<=0 → None。"""
    nums = [v for v in (float(x) for x in values) if math.isfinite(v) and v >= 0]
    k = len(nums)
    total = sum(nums)
    if k < 2 or total <= 0:
        return None
    weights = [v / total for v in nums]
    raw = sum(w * w for w in weights)
    value = (raw - 1.0 / k) / (1.0 - 1.0 / k)
    return min(1.0, max(0.0, value))


def empirical_percentile(current: float, history: list[float]) -> float | None:
    """包容经验 CDF: history 中 <= current 的占比; 空历史 → None。"""
    if not history:
        return None
    return sum(1 for v in history if v <= current) / len(history)


def classify_state(
    p_return_std: float | None,
    p_turnover_hhi: float | None,
    p_positive_return_hhi: float | None,
    p_top3_contribution: float | None,
) -> str:
    if any(
        v is None
        for v in (p_return_std, p_turnover_hhi, p_positive_return_hhi, p_top3_contribution)
    ):
        return "unavailable"
    if p_turnover_hhi >= CONCENTRATED_TURNOVER_PCT and (
        p_positive_return_hhi >= CONCENTRATED_POSITIVE_PCT
        or p_top3_contribution >= CONCENTRATED_TOP3_PCT
    ):
        return "concentrated"
    if (
        p_turnover_hhi <= DISPERSED_TURNOVER_PCT
        and p_positive_return_hhi <= DISPERSED_POSITIVE_PCT
        and p_top3_contribution <= DISPERSED_TOP3_PCT
        and p_return_std >= DISPERSED_RETURN_STD_PCT
    ):
        return "dispersed"
    return "transition"


def industry_contributions(
    returns: dict[str, float],
    amounts: dict[str, float],
    industries: dict[str, str],
) -> tuple[list[float], int] | None:
    """正超额贡献(归一化): c_i = 行业成交额占比 × max(行业收益−市场中位数, 0)。

    返回 (归一化贡献列表, 行业数); 贡献总和 <= 0 → None(fail-closed)。
    """
    if not returns:
        return None
    market_median = median(list(returns.values()))
    if market_median is None:
        return None
    mapped_amounts: dict[str, float] = {}
    for sym, amt in amounts.items():
        ind = industries.get(sym)
        if ind is not None and math.isfinite(amt) and amt >= 0:
            mapped_amounts[sym] = amt
    total_amount = sum(mapped_amounts.values())
    if total_amount <= 0:
        return None

    ind_returns: dict[str, list[float]] = {}
    ind_turnover: dict[str, float] = {}
    for sym, ret in returns.items():
        ind = industries.get(sym)
        if ind is None:
            continue
        ind_returns.setdefault(ind, []).append(ret)
    for sym, amt in mapped_amounts.items():
        ind = industries[sym]
        ind_turnover[ind] = ind_turnover.get(ind, 0.0) + amt

    raw: list[float] = []
    for ind, turnover in ind_turnover.items():
        member_returns = ind_returns.get(ind)
        if not member_returns:
            continue
        ind_return = median(member_returns)
        if ind_return is None:
            continue
        raw.append((turnover / total_amount) * max(ind_return - market_median, 0.0))
    industry_count = len(ind_returns)
    total_c = sum(raw)
    if industry_count < 1 or total_c <= 0:
        return None
    return [c / total_c for c in raw], industry_count


@dataclass
class DayMetrics:
    day: date
    stock_count: int = 0
    industry_count: int = 0
    symbol_coverage: float | None = None
    turnover_coverage: float | None = None
    return_std: float | None = None
    return_q90_q10: float | None = None
    turnover_hhi: float | None = None
    positive_return_hhi: float | None = None
    top3_contribution: float | None = None
    top5_contribution: float | None = None
    valid: bool = False
    reasons: list[str] = field(default_factory=list)

    def metric(self, key: str) -> float | None:
        return getattr(self, key)


def compute_day_metrics(
    returns: dict[str, float],
    amounts: dict[str, float | None],
    industries: dict[str, str],
    day: date | None = None,
) -> DayMetrics:
    """单日截面指标 + 有效性门槛; 任何门槛不过即 invalid(带原因码)。"""
    metrics = DayMetrics(day=day) if day is not None else DayMetrics(day=date.min)
    clean_returns = {
        symbol: value for symbol, raw in returns.items() if (value := _finite(raw)) is not None
    }
    metrics.stock_count = len(clean_returns)
    if not clean_returns:
        metrics.reasons.append("empty_cross_section")
        return metrics

    mapped = {
        symbol: industry
        for symbol in clean_returns
        if (industry := str(industries.get(symbol) or "").strip())
    }
    metrics.symbol_coverage = len(mapped) / metrics.stock_count

    positive_amounts = {
        symbol: value
        for symbol in clean_returns
        if (value := _finite(amounts.get(symbol))) is not None and value > 0
    }
    all_valid_amount = sum(positive_amounts.values())
    mapped_amounts = {
        symbol: amount for symbol, amount in positive_amounts.items() if symbol in mapped
    }
    mapped_amount = sum(mapped_amounts.values())
    metrics.turnover_coverage = mapped_amount / all_valid_amount if all_valid_amount > 0 else None

    ret_values = list(clean_returns.values())
    metrics.return_std = cross_section_std(ret_values)
    metrics.return_q90_q10 = cross_section_q90_q10(ret_values)

    industry_amounts: dict[str, float] = {}
    for symbol, amount in mapped_amounts.items():
        industry = mapped[symbol]
        industry_amounts[industry] = industry_amounts.get(industry, 0.0) + amount
    metrics.industry_count = len(industry_amounts)
    metrics.turnover_hhi = normalized_hhi(list(industry_amounts.values()))

    contrib = industry_contributions(clean_returns, mapped_amounts, mapped)
    if contrib is None:
        metrics.reasons.append("zero_positive_contribution")
    else:
        weights, contribution_industry_count = contrib
        metrics.industry_count = min(metrics.industry_count, contribution_industry_count)
        ordered = sorted(weights, reverse=True)
        metrics.positive_return_hhi = normalized_hhi(weights)
        metrics.top3_contribution = sum(ordered[:3])
        metrics.top5_contribution = sum(ordered[:5])
        if metrics.positive_return_hhi is None:
            metrics.reasons.append("single_industry_hhi_undefined")

    if metrics.stock_count < MIN_STOCK_COUNT:
        metrics.reasons.append("stock_count_below_minimum")
    if metrics.industry_count < MIN_INDUSTRY_COUNT:
        metrics.reasons.append("industry_count_below_minimum")
    if metrics.symbol_coverage < MIN_SYMBOL_COVERAGE:
        metrics.reasons.append("symbol_coverage_below_minimum")
    if metrics.turnover_coverage is None or metrics.turnover_coverage < MIN_TURNOVER_COVERAGE:
        metrics.reasons.append("turnover_coverage_below_minimum")
    for key in _METRIC_KEYS:
        value = metrics.metric(key)
        if value is None or not math.isfinite(value):
            metrics.reasons.append("metric_not_finite")

    metrics.valid = not metrics.reasons
    return metrics


# ── 编排 ────────────────────────────────────────────────────────
def _unavailable_snapshot(
    target_date: date,
    signal_date: date | None,
    reason_codes: list[str],
    warnings: list[str],
    coverage: MarketStateCoverage | None = None,
) -> MarketStateSnapshot:
    return MarketStateSnapshot(
        available=False,
        state="unavailable",
        target_date=target_date.isoformat(),
        signal_date=signal_date.isoformat() if signal_date is not None else None,
        coverage=coverage or MarketStateCoverage(),
        gates=MarketStateGates(
            automatic_research_allowed=False,
            reasons=[f"market_state_unavailable:{code}" for code in reason_codes],
        ),
        reason=";".join(reason_codes) if reason_codes else None,
        warnings=list(warnings),
    )


def _load_market_history(repo: Any, target: date) -> pl.DataFrame:
    try:
        start = target - timedelta(days=HISTORY_CALENDAR_DAYS)
        frame = repo.get_enriched_range(
            start,
            target,
            columns=["symbol", "date", "raw_close", "amount"],
        )
    except Exception as exc:  # noqa: BLE001 — 对外只暴露脱敏错误
        raise MarketStateDataError("canonical enriched history unreadable") from exc
    required = {"symbol", "date", "raw_close", "amount"}
    if frame is None or not required.issubset(frame.columns):
        raise MarketStateDataError("canonical enriched history unreadable")
    return frame


def _load_industry_events(data_dir: Any) -> list[tuple[date, str, str]]:
    from app.services.screener_financials import (
        FinancialSnapshotError,
        load_industry_announcements,
    )

    try:
        events = load_industry_announcements(data_dir)
    except FinancialSnapshotError as exc:
        raise MarketStateDataError("industry mapping source unavailable") from exc
    rows = events.select(["symbol", "industry", "notice_date"]).iter_rows()
    parsed = [
        (notice, str(symbol), str(industry))
        for symbol, industry, notice in rows
        if notice is not None
    ]
    parsed.sort(key=lambda item: (item[0], item[1]))
    return parsed


def compute_market_state(repo: Any, target_date: date | None = None) -> MarketStateSnapshot:
    """计算 target_date 的市场状态快照(严格 T-1)。

    target_date 为 None 时取 canonical 最新交易日；仓储读取失败抛
    MarketStateDataError，其余数据性问题返回 unavailable。
    """
    warnings: list[str] = []
    if target_date is None:
        try:
            _latest_frame, latest_date = repo.get_enriched_latest()
        except Exception as exc:  # noqa: BLE001 — 对外只暴露脱敏错误
            raise MarketStateDataError("canonical enriched latest date unreadable") from exc
        if latest_date is None:
            return _unavailable_snapshot(date.min, None, ["no_prior_trading_day"], warnings)
        target_date = latest_date

    frame = _load_market_history(repo, target_date)
    days = sorted(set(frame["date"].drop_nulls().to_list()))
    prior = [day for day in days if day < target_date]
    if not prior:
        return _unavailable_snapshot(target_date, None, ["no_prior_trading_day"], warnings)
    signal_date = prior[-1]
    if target_date not in days:
        warnings.append("target_date is not a trading day; using prior trading day")

    fetch_days = prior[-FETCH_TRADING_DAYS:]
    if len(fetch_days) < SMOOTHING_DAYS + 1:
        return _unavailable_snapshot(
            target_date,
            signal_date,
            ["insufficient_calibration"],
            warnings,
        )
    frame = frame.filter((pl.col("date") >= fetch_days[0]) & (pl.col("date") <= signal_date))

    # 按日分组: {date: {symbol: (raw_close, amount)}}
    per_day: dict[date, dict[str, tuple[float | None, float | None]]] = {}
    if not frame.is_empty():
        deduped = frame.sort(["date", "symbol"]).unique(subset=["date", "symbol"], keep="last")
        for symbol, day, raw_close, amount in deduped.iter_rows():
            slot = per_day.setdefault(day, {})
            slot[str(symbol)] = (_finite(raw_close), _finite(amount))

    events = _load_industry_events(repo.store.data_dir)
    event_idx = 0
    mapping: dict[str, str] = {}
    anchor_rows = per_day.get(fetch_days[0], {})
    prev_close: dict[str, float] = {
        symbol: close for symbol, (close, _amount) in anchor_rows.items() if close is not None
    }
    observed_sessions: dict[str, int] = {
        symbol: 1 for symbol, (close, _amount) in anchor_rows.items() if close is not None
    }
    daily: list[DayMetrics] = []
    for day in fetch_days[1:]:
        while event_idx < len(events) and events[event_idx][0] <= day:
            _, symbol, industry = events[event_idx]
            mapping[symbol] = industry
            event_idx += 1
        rows = per_day.get(day, {})
        returns: dict[str, float] = {}
        amounts: dict[str, float | None] = {}
        for symbol, (close, amount) in rows.items():
            prev = prev_close.get(symbol)
            if close is not None:
                observed_sessions[symbol] = observed_sessions.get(symbol, 0) + 1
            if (
                close is not None
                and prev is not None
                and prev > 0
                and close > 0
                and observed_sessions.get(symbol, 0) >= MIN_OBSERVATION_SESSIONS
            ):
                returns[symbol] = close / prev - 1.0
                amounts[symbol] = amount
        daily.append(compute_day_metrics(returns, amounts, mapping, day=day))
        prev_close = {s: close for s, (close, _a) in rows.items() if close is not None}

    if not daily:
        return _unavailable_snapshot(target_date, signal_date, ["no_metric_days"], warnings)

    window = daily[-SMOOTHING_DAYS:]
    invalid_days = [d for d in window if not d.valid]
    if len(window) < SMOOTHING_DAYS or invalid_days:
        codes = ["insufficient_smoothing_window"]
        codes.extend(sorted({code for d in invalid_days for code in d.reasons}))
        signal_day = daily[-1]
        coverage = MarketStateCoverage(
            stock_count=signal_day.stock_count,
            industry_count=signal_day.industry_count,
            symbol_coverage=signal_day.symbol_coverage,
            turnover_coverage=signal_day.turnover_coverage,
            calibration_days=0,
        )
        return _unavailable_snapshot(target_date, signal_date, codes, warnings, coverage)

    current = {key: median([d.metric(key) for d in window]) for key in _METRIC_KEYS}
    if any(value is None or not math.isfinite(value) for value in current.values()):
        return _unavailable_snapshot(target_date, signal_date, ["metric_not_finite"], warnings)

    smoothed_history: list[dict[str, float]] = []
    for end_index in range(SMOOTHING_DAYS - 1, len(daily) - 1):
        historical_window = daily[end_index - SMOOTHING_DAYS + 1 : end_index + 1]
        if not all(day_metrics.valid for day_metrics in historical_window):
            continue
        point = {
            key: median([day_metrics.metric(key) for day_metrics in historical_window])
            for key in _METRIC_KEYS
        }
        if all(value is not None and math.isfinite(value) for value in point.values()):
            smoothed_history.append(point)
    history = smoothed_history[-CALIBRATION_DAYS:]
    if len(history) < MIN_CALIBRATION_DAYS:
        return _unavailable_snapshot(
            target_date,
            signal_date,
            ["insufficient_calibration"],
            warnings,
            MarketStateCoverage(
                stock_count=window[-1].stock_count,
                industry_count=window[-1].industry_count,
                symbol_coverage=window[-1].symbol_coverage,
                turnover_coverage=window[-1].turnover_coverage,
                calibration_days=len(history),
            ),
        )

    percentiles = {
        key: empirical_percentile(current[key], [point[key] for point in history])
        for key in _PERCENTILE_KEYS
    }
    state = classify_state(
        percentiles["return_std"],
        percentiles["turnover_hhi"],
        percentiles["positive_return_hhi"],
        percentiles["top3_contribution"],
    )
    if state == "unavailable":  # 理论不可达(历史非空且指标有限), 防御性兜底
        return _unavailable_snapshot(target_date, signal_date, ["metric_not_finite"], warnings)

    signal_day = window[-1]
    allowed = state == "dispersed"
    reasons: list[str] = []
    if not allowed:
        reasons = [f"market_state_not_dispersed:{state}"]
    return MarketStateSnapshot(
        available=True,
        state=state,
        target_date=target_date.isoformat(),
        signal_date=signal_date.isoformat(),
        metrics=MarketStateMetrics(**current),
        percentiles=MarketStatePercentiles(**percentiles),
        coverage=MarketStateCoverage(
            stock_count=signal_day.stock_count,
            industry_count=signal_day.industry_count,
            symbol_coverage=signal_day.symbol_coverage,
            turnover_coverage=signal_day.turnover_coverage,
            calibration_days=len(history),
        ),
        gates=MarketStateGates(automatic_research_allowed=allowed, reasons=reasons),
        reason=None,
        warnings=warnings,
    )


# ── 轻量 TTL 缓存(同参数短窗口内复用, 避免重复全量扫描) ─────────
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 128
_cache: dict[tuple[str, str, Any], tuple[float, MarketStateSnapshot]] = {}
_cache_lock = threading.Lock()


def invalidate_market_state_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _prune_cache(now: float) -> None:
    expired = [
        key
        for key, (stored_at, _snapshot) in _cache.items()
        if now - stored_at >= _CACHE_TTL_SECONDS
    ]
    for key in expired:
        _cache.pop(key, None)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda key: _cache[key][0])
        _cache.pop(oldest, None)


def market_state_for_date(repo: Any, target_date: date | None = None) -> MarketStateSnapshot:
    target_key = target_date.isoformat() if target_date is not None else "latest"
    key = (
        str(repo.store.data_dir),
        target_key,
        getattr(repo, "cache_generation", 0),
    )
    now = time.monotonic()
    with _cache_lock:
        _prune_cache(now)
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    snapshot = compute_market_state(repo, target_date)
    with _cache_lock:
        stored_at = time.monotonic()
        _prune_cache(stored_at)
        _cache[key] = (stored_at, snapshot)
        _prune_cache(stored_at)
    return snapshot


__all__ = [
    "CALIBRATION_DAYS",
    "DayMetrics",
    "FETCH_TRADING_DAYS",
    "MarketStateCoverage",
    "MarketStateDataError",
    "MarketStateGates",
    "MarketStateMetrics",
    "MarketStateMethodology",
    "MarketStatePercentiles",
    "MarketStateSnapshot",
    "MarketStateSource",
    "MIN_CALIBRATION_DAYS",
    "SMOOTHING_DAYS",
    "T_LAG",
    "classify_state",
    "compute_day_metrics",
    "compute_market_state",
    "cross_section_q90_q10",
    "cross_section_std",
    "empirical_percentile",
    "industry_contributions",
    "invalidate_market_state_cache",
    "median",
    "market_state_for_date",
    "normalized_hhi",
    "quantile",
]
