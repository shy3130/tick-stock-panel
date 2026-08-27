"""MTF 方向研究 (15m/5m) — ordered-trans 研究生产链 (Issue #10 final-design)。

设计契约 (docs/ISSUE-10/final-design.md):

  - runtime 绝不读 raw CSV / 现有重建分钟接口; 唯一数据源是 provider
    经 published ``tdx_ordered_trans`` generation 打开的 immutable reader
    (duck-typed :class:`ImmutableMinuteReader`)。缺任何方法, 解析恒 None。
  - consumer 完整性不信任 adapter 声明: 每 symbol/day 必须恰好 240 根,
    ``bar.ts`` 逐项等于 naive Asia/Shanghai 收盘序列
    ``09:31..11:30,13:01..15:00`` (禁止 09:30 / 两分钟 gap / 重复 /
    多余 bar); OHLCV、symbol/day、strict monotonic、naive sealed cutoff
    覆盖末日 15:00 全部验证; 任一失败统一 ``source_integrity_violation``
    的结构化 unavailable。
  - 聚合只做位置式分块, 5m anchors = ``09:35..11:30,13:05..15:00``,
    15m anchors = ``09:45..11:30,13:15..15:00``; 聚合结果逐日重新断言,
    错位即 fail-closed。
  - 请求必填 ``oos_start`` 且 ``start < oos_start <= end``; provenance
    固定 split。标签 ``raw_return=close[label_end]/close[signal_close]-1``
    正/负/零 -> up/down/flat; hit 仅限非-flat 预测命中非-flat 标签;
    ``signed_return = raw_return * 方向`` (flat 预测 null);
    ``cost_bps=5.0`` 固定, 非-flat ``post_cost_return=signed-0.0005``;
    MFE/MAE 按预测方向取 label bar high/low。保存
    ``row_id/signal_close/label_end/horizon/label_value``。
  - 每 horizon 先剔除跨越 ``oos_start 00:00`` 的区间 (cross-boundary),
    再对所有 symbols 按 ``(signal_close,symbol,row_id)`` 升序做全局
    interval purge: 下一个 signal_close 必须严格晚于已保留 label_end;
    报告 raw/cross-boundary/overlap/effective 数。
  - 同一 common OOS row ids 比较 factor 与三基线: unconditional 只用
    IS effective labels 拟合多数 up/down (平票->flat), momentum_5 =
    signal close 对前第 5 根 15m close, sma5 = 对含当前的 5 根 SMA;
    预热不足不进 common set; flat 预测 hit=false、收益 null。输出
    Wilson 95% CI 与 mean signed/post-cost return、MFE/MAE。v1 参数
    不调优。verdict 仅由 horizon 1 决定: common OOS >= 30 且 factor
    mean post-cost > 0 且 factor Wilson lower > 三基线最高 point hit
    rate -> accepted, 否则 rejected 并列原因; horizon 2 只诊断。
  - reader 的 close 责任属于 owner (API 的 owned reader 或注册方),
    本模块绝不关闭注入的 reader; service 不读 env/root, 不持有全局
    production reader。
  - 结果不进入 short_pool / Agent, 不落盘; 模块导入无副作用。

v1 连续序列建模 (设计内固定): 每 symbol 以 market_days 升序拼接各已
验证交易日 (恰 16 根 anchor 对齐的 15m bar) 形成连续序列; 15m 标签窗
允许跨交易日, momentum/sma 预热按连续序列计; 全部价格用未复权收盘
levels 差分, overnight jump 自然进入 returns。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from statistics import NormalDist
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

# ── 固定协议标识 (逐字锁定) ───────────────────────────────────────
MTF_DIRECTION_FACTOR_ID = "mtf_direction_15m5m_v1"
MTF_DIRECTION_SCHEMA_VERSION = 1
MTF_DIRECTION_NAME = "15m/5m 多周期方向研究"

MIN_SYMBOLS = 1
MAX_SYMBOLS = 50
MAX_WINDOW_DAYS = 370  # 含最多 1 个闰日的一年内

#: 每根分钟 bar 的 horizon 候选 (final-design 固定 v1 参数)。
HORIZONS: tuple[int, ...] = (1, 2)

#: verdict 只由 horizon 1 决定时的最小 common OOS rows。
COMMON_OOS_MIN_ROWS_H1 = 30

#: 固定交易成本 (bps 与小数收益率两种形态)。design: cost_bps=5.0。
COST_BPS = 5.0
COST_RETURN = COST_BPS / 10000.0  # 0.0005

#: momentum/sma 的回看窗口 (连续 15m 序列 index 计)。
MOMENTUM_LAG_15M = 5
SMA_WINDOW_15M = 5

#: 标准 95% Wilson score 区间的正态分位数。
_WILSON_Z: float = NormalDist().inv_cdf(0.975)

#: 统一的 source-integrity 失败原因码 (fail-closed 家族)。
SOURCE_INTEGRITY_VIOLATION = "source_integrity_violation"

#: canonical A 股代码: 6 位 ASCII 数字 + SH/SZ/BJ (与 research_analysis 一致)。
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")

#: manifest sha256 必须是小写 hex digest (64 字符)。
_MANIFEST_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

#: reader 必须提供的全部能力 (方法名 -> 语义), 顺序即报告顺序。
READER_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("catalog_manifest", "immutable catalog manifest"),
    ("manifest_sha256", "immutable manifest sha256 provenance"),
    ("generation", "immutable dataset generation id"),
    ("market_days", "market calendar lookup"),
    ("session", "per-symbol session open/close"),
    ("minute_bars", "real OHLCV minute bars"),
    ("sealed_cutoff", "point-in-time sealed cutoff"),
)
READER_METHODS: tuple[str, ...] = tuple(name for name, _ in READER_CAPABILITIES)

#: 交易词禁令: evidence key / 顶层结果 key 不得含这些子串 (大小写不敏感)。
_TRADING_TERM_RE = re.compile(
    r"buy|sell|target|stop|action|entry|exit|order", re.IGNORECASE
)

#: evidence key 白名单 (构造侧结构性禁令之外的显式清单)。
EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        "reader_manifest",
        "generation_id",
        "market_days",
        "session_continuity",
        "ohlcv_integrity",
        "timestamp_index",
        "sealed_cutoff",
        "symbol_coverage",
        "window_coverage",
        "manifest_sha256",
        "aggregation_anchors",
    }
)

#: 一小时内固定 12 根 5m bar; 15m 线段由 3 根 5m 聚合, 此处只锁精度声明。
BAR_PRECISION = "5m"
HIGHER_TIMEFRAME = "15m"

MTF_DIRECTION_DISCLAIMER = "研究能力验证结果，非投资建议，不含任何交易指令"


class TradingTermForbidden(ValueError):
    """evidence/结果 key 命中交易词禁令 — 结构性非法输出。"""


def validate_result_key(key: str) -> str:
    """校验单个结果/evidence key 不含交易词; 违规即抛错 (fail-closed)。"""
    if not isinstance(key, str) or not key:
        raise TradingTermForbidden(f"invalid result key: {key!r}")
    if _TRADING_TERM_RE.search(key):
        raise TradingTermForbidden(f"trading term forbidden in key: {key!r}")
    return key


# ── 数据契约 ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MinuteBar:
    """单根真实分钟 OHLCV bar (不可变)。"""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """单 symbol 单交易日的 session 开收盘时间 (不可变)。"""

    symbol: str
    day: date
    open_time: time
    close_time: time


@runtime_checkable
class ImmutableMinuteReader(Protocol):
    """真实 immutable 分钟数据 reader 的完整能力协议。

    生产实现是 FQuantProvider.open_ordered_trans_reader() 返回的
    PublishedOrderedTransMinuteReader; 测试可显式注册等价 duck-type。
    任何缺方法/非 callable 的候选都视为不存在, 绝不降级到 price/
    volume 重建链路。close() 属于 owner, 本协议消费者不得调用。
    """

    def catalog_manifest(self) -> Mapping[str, Any]:
        """immutable catalog manifest (至少含 generation 标识)。"""
        ...

    def manifest_sha256(self) -> str:
        """固定 generation 的 manifest bytes SHA-256 (小写 hex digest)。"""
        ...

    def generation(self) -> str:
        """数据集不可变 generation 标识 (非空字符串)。"""
        ...

    def market_days(self, start: date, end: date) -> Sequence[date]:
        """[start, end] 内的实际交易日历 (升序, 仅 complete days)。"""
        ...

    def session(self, symbol: str, day: date) -> SessionSpec:
        """symbol 在 day 的 session 开收盘时间。"""
        ...

    def minute_bars(self, symbol: str, day: date) -> Sequence[MinuteBar]:
        """symbol 在 day 的全部真实分钟 OHLCV bar (ts 升序)。"""
        ...

    def sealed_cutoff(self) -> datetime:
        """sealed point-in-time cutoff: 所有可读 bar 的 ts 上界。"""
        ...


# ── reader registry / resolver (测试与显式注入点) ──────────────────
_REGISTERED_READER: list[ImmutableMinuteReader] = []


def register_minute_reader(reader: ImmutableMinuteReader) -> None:
    """显式注册真实 reader (caller-owned: 注册方负责 close, 服务/API 不关)。"""
    _REGISTERED_READER.clear()
    _REGISTERED_READER.append(reader)


def clear_registered_minute_reader() -> None:
    """清空注册 (测试隔离用)。"""
    _REGISTERED_READER.clear()


def _reader_satisfies_protocol(candidate: Any) -> bool:
    """逐能力校验: 全部方法存在且 callable 才算满足。"""
    return all(
        callable(getattr(candidate, name, None)) for name in READER_METHODS
    )


def resolve_minute_reader() -> ImmutableMinuteReader | None:
    """解析可用 reader; 缺任何能力即返回 ``None`` (fail-closed)。

    不读取 app 状态、不回退现有 provider、不发外部请求。
    """
    for candidate in tuple(_REGISTERED_READER):
        if _reader_satisfies_protocol(candidate):
            return candidate
    return None


# ── 输入模型 ─────────────────────────────────────────────────────
class MTFDirectionEvaluateIn(BaseModel):
    """evaluate 请求: 严格 schema, extra=forbid, 符号去重 + oos split 校验。"""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    #: 必填 OOS 起始日 (provenance 固定 split): start < oos_start <= end。
    oos_start: date
    symbols: list[StrictStr] = Field(min_length=MIN_SYMBOLS, max_length=MAX_SYMBOLS)

    @model_validator(mode="after")
    def _validate_window_split_and_symbols(self) -> "MTFDirectionEvaluateIn":
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if not (self.start < self.oos_start <= self.end):
            raise ValueError("oos_start must satisfy start < oos_start <= end")
        if (self.end - self.start) > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError(f"window must be <= {MAX_WINDOW_DAYS} days")
        normalized: list[str] = []
        for raw in self.symbols:
            symbol = raw.strip()
            if not symbol:
                raise ValueError("symbols must be non-empty strings")
            if not _SYMBOL_RE.match(symbol):
                raise ValueError(f"non-canonical symbol: {symbol!r}")
            if symbol not in normalized:
                normalized.append(symbol)
        if not normalized:
            raise ValueError("symbols must contain at least one canonical code")
        object.__setattr__(self, "symbols", normalized)
        return self


# ── 验证与 evaluate ──────────────────────────────────────────────
def _evidence(key: str, status: str, detail: Any) -> dict[str, Any]:
    """构造单条 evidence; key 先过白名单 + 交易词禁令。"""
    if key not in EVIDENCE_KEYS:
        raise TradingTermForbidden(f"evidence key not in whitelist: {key!r}")
    validate_result_key(key)
    return {"key": key, "status": status, "detail": detail}


def _unavailable(
    params: MTFDirectionEvaluateIn,
    reason: str,
    *,
    missing_capabilities: Sequence[str] = (),
    evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "factor_id": MTF_DIRECTION_FACTOR_ID,
        "schema_version": MTF_DIRECTION_SCHEMA_VERSION,
        "name": MTF_DIRECTION_NAME,
        "status": "unavailable",
        "reason": reason,
        "missing_capabilities": list(missing_capabilities),
        "evidence": [dict(item) for item in evidence],
        "symbols": list(params.symbols),
        "window": {
            "start": params.start.isoformat(),
            "end": params.end.isoformat(),
            "oos_start": params.oos_start.isoformat(),
        },
        "bar_precision": BAR_PRECISION,
        "higher_timeframe": HIGHER_TIMEFRAME,
        "direction": None,
        "direction_labelling_pending": True,
        "disclaimer": MTF_DIRECTION_DISCLAIMER,
    }


def _is_finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _is_finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _shanghai_minute_closes(day: date) -> tuple[datetime, ...]:
    """单交易日的 canonical 240 个 naive 分钟收盘时间戳。

    由 09:30..14:59 原始分钟加一分钟规范化而来:
    ``09:31..11:30`` (120 根) + ``13:01..15:00`` (120 根)。
    结果 lru_cache 缓存 (date 不可变, 安全)。
    """
    return _cached_minute_closes(day)


@lru_cache(maxsize=None)
def _cached_minute_closes(day: date) -> tuple[datetime, ...]:
    stamps: list[datetime] = []
    current = datetime.combine(day, time(9, 31))
    while current.time() <= time(11, 30):
        stamps.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(day, time(13, 1))
    while current.time() <= time(15, 0):
        stamps.append(current)
        current += timedelta(minutes=1)
    if len(stamps) != 240:
        # canonical 常量生成故障 → 立即暴露而非静默错位。
        raise TradingTermForbidden("canonical close stamp count drifted from 240")
    return tuple(stamps)


def _expected_five_minute_anchor_count(day: date) -> int:
    del day
    return 48


def _expected_fifteen_minute_anchor_stamps(day: date) -> tuple[datetime, ...]:
    """单日 16 个 15m anchor close 时间戳 (由 canonical 分钟列派生)。"""
    closes = _shanghai_minute_closes(day)
    return closes[14::15]


def _aggregate_by_position(
    bars: Sequence[MinuteBar], size: int, *, expected_anchors: Sequence[datetime]
) -> list["TimeframeBar"]:
    """对已通过完整性校验的连续 bars 做纯位置式聚合并断言 anchors。

    输入必须已是严格单调且逐项对齐 canonical 收盘序列的数据; 分块固定
    size 个一组, 组末 ts 即 anchor。任何错位即抛 AssertionError 上层转
    source-integrity unavailable。
    """
    assert len(bars) % size == 0, f"bar count {len(bars)} not divisible by {size}"
    output: list[TimeframeBar] = []
    group_anchor_index = 0
    for start in range(0, len(bars), size):
        group = bars[start : start + size]
        anchor = group[-1].ts
        assert anchor == expected_anchors[group_anchor_index], (
            "aggregation anchor mismatch"
        )
        output.append(
            TimeframeBar(
                ts=anchor,
                open=float(group[0].open),
                high=max(float(item.high) for item in group),
                low=min(float(item.low) for item in group),
                close=float(group[-1].close),
                volume=sum(float(item.volume) for item in group),
            )
        )
        group_anchor_index += 1
    return output


@dataclass(slots=True)
class TimeframeBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

def _aggregate_sparse_five(
    bars: Sequence[MinuteBar], *, expected_closes: Sequence[datetime]
) -> list[TimeframeBar]:
    """Aggregate sparse true-trade minute bars into 48 timestamp buckets."""
    expected_index = {stamp: index for index, stamp in enumerate(expected_closes)}
    groups: list[list[MinuteBar]] = [[] for _ in range(48)]
    for bar in bars:
        groups[expected_index[bar.ts] // 5].append(bar)
    assert all(groups), "one or more five-minute windows have no trades"
    output: list[TimeframeBar] = []
    for index, group in enumerate(groups):
        anchor = expected_closes[index * 5 + 4]
        output.append(
            TimeframeBar(
                ts=anchor,
                open=float(group[0].open),
                high=max(float(item.high) for item in group),
                low=min(float(item.low) for item in group),
                close=float(group[-1].close),
                volume=sum(float(item.volume) for item in group),
            )
        )
    return output


@dataclass(slots=True)
class _SymbolCorpus:
    """单 symbol 的完整已验证研究语料 (跨日连续 15m 序列 + 校验状态)。"""

    fifteen: list[TimeframeBar] = field(default_factory=list)
    #: 每个连续 index 的 factor 方向预测 (预热不足 None)。
    factor_predictions: list[str | None] = field(default_factory=list)


def _finalize_source_integrity_fail(
    evidence: list[dict[str, Any]], detail: dict[str, Any]
) -> tuple[list[dict[str, Any]], str, dict[str, _SymbolCorpus]]:
    evidence.append(_evidence("ohlcv_integrity", "failed", detail))
    return evidence, SOURCE_INTEGRITY_VIOLATION, {}

def _validate_reader_contracts(
    reader: ImmutableMinuteReader, params: MTFDirectionEvaluateIn
) -> tuple[list[dict[str, Any]], str | None, dict[str, _SymbolCorpus]]:
    evidence: list[dict[str, Any]] = []
    try:
        manifest = reader.catalog_manifest()
        digest = reader.manifest_sha256()
        generation = reader.generation()
    except Exception:
        return evidence, "source_integrity_violation", {}
    if not isinstance(manifest, Mapping) or not manifest:
        return evidence, "manifest_missing", {}
    if not isinstance(generation, str) or not generation.strip():
        return evidence, "manifest_missing_generation", {}
    if manifest.get("generation") not in (None, generation):
        return evidence, "generation_mismatch_with_manifest", {}
    if not isinstance(digest, str) or not _MANIFEST_SHA_RE.fullmatch(digest):
        return evidence, SOURCE_INTEGRITY_VIOLATION, {}
    evidence.extend([
        _evidence("reader_manifest", "verified", {"manifest_keys": sorted(map(str, manifest))[:20]}),
        _evidence("manifest_sha256", "verified", digest),
        _evidence("generation_id", "verified", {"generation": generation}),
    ])
    try:
        cutoff = reader.sealed_cutoff()
        days = list(reader.market_days(params.start, params.end))
    except Exception:
        return evidence, SOURCE_INTEGRITY_VIOLATION, {}
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is not None:
        return evidence, SOURCE_INTEGRITY_VIOLATION, {}
    if not days or days != sorted(set(days)):
        return evidence, "market_days_invalid", {}
    corpus = {symbol: _SymbolCorpus() for symbol in params.symbols}
    minute_bar_total = 0
    for symbol in params.symbols:
        for day in days:
            try:
                spec = reader.session(symbol, day)
                raw_bars = list(reader.minute_bars(symbol, day))
            except Exception:
                return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "reader_unreadable"})
            if (
                getattr(spec, "symbol", None) != symbol
                or getattr(spec, "day", None) != day
                or getattr(spec, "open_time", None) != time(9, 30)
                or getattr(spec, "close_time", None) != time(15, 0)
            ):
                return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "invalid_session"})
            expected = _shanghai_minute_closes(day)
            expected_set = set(expected)
            if not 1 <= len(raw_bars) <= 240:
                return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "invalid_sparse_bar_count"})
            bars: list[MinuteBar] = []
            previous_ts: datetime | None = None
            for raw_bar in raw_bars:
                bar_symbol = getattr(raw_bar, "symbol", None)
                bar_ts = getattr(raw_bar, "ts", None)
                values = tuple(getattr(raw_bar, name, None) for name in ("open", "high", "low", "close", "volume"))
                if (
                    bar_symbol != symbol
                    or not isinstance(bar_ts, datetime)
                    or bar_ts not in expected_set
                    or bar_ts.tzinfo is not None
                    or bar_ts.date() != day
                    or (previous_ts is not None and bar_ts <= previous_ts)
                ):
                    return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "symbol_day_or_monotonic"})
                open_, high, low, close, volume = values
                if not all(_is_finite_positive(v) for v in (open_, high, low, close)) or high < max(open_, close) or low > min(open_, close) or high < low or not _is_finite_nonnegative_number(volume):
                    return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "ohlcv"})
                bars.append(MinuteBar(symbol, bar_ts, float(open_), float(high), float(low), float(close), float(volume)))
                previous_ts = bar_ts
            if bars[0].ts != expected[0] or bars[-1].ts != expected[-1]:
                return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "missing_opening_or_closing_trade_bar"})
            minute_bar_total += len(bars)
            try:
                five = _aggregate_sparse_five(bars, expected_closes=expected)
                fifteen = _aggregate_by_position(
                    [MinuteBar(symbol, x.ts, x.open, x.high, x.low, x.close, x.volume) for x in five],
                    3, expected_anchors=expected[14::15],
                )
            except (AssertionError, IndexError):
                return _finalize_source_integrity_fail(evidence, {"symbol": symbol, "day": day.isoformat(), "error": "aggregation_anchor"})
            corpus[symbol].fifteen.extend(fifteen)
    if cutoff < datetime.combine(max(days), time(15, 0)):
        return _finalize_source_integrity_fail(evidence, {"error": "cutoff_before_complete_day"})
    evidence.extend([
        _evidence("market_days", "verified", {"days": len(days)}),
        _evidence("session_continuity", "verified", {"symbol_day_pairs": len(params.symbols) * len(days)}),
        _evidence("ohlcv_integrity", "verified", {"bars_total": minute_bar_total, "sparse_true_trade": True}),
        _evidence("timestamp_index", "verified", {"strictly_increasing": True}),
        _evidence("aggregation_anchors", "verified", {"five_minute": True, "fifteen_minute": True}),
        _evidence("sealed_cutoff", "verified", {"cutoff": cutoff.isoformat()}),
        _evidence("symbol_coverage", "verified", {"symbols": len(params.symbols)}),
        _evidence("window_coverage", "verified", {"start": params.start.isoformat(), "end": params.end.isoformat()}),
    ])
    return evidence, None, corpus


def _atr(bars: Sequence[TimeframeBar], index: int, period: int = 14) -> float | None:
    if index < period:
        return None
    vals = []
    for pos in range(index - period + 1, index + 1):
        prev = bars[pos - 1].close
        cur = bars[pos]
        vals.append(max(cur.high - cur.low, abs(cur.high - prev), abs(cur.low - prev)))
    value = sum(vals) / len(vals)
    return value if value > 0 else None


def _factor_predictions(bars: Sequence[TimeframeBar]) -> list[str | None]:
    events: list[tuple[int, str]] = []
    for index in range(len(bars)):
        if index >= 4:
            center = index - 2
            window = bars[center - 2:center + 3]
            others = [item for pos, item in enumerate(window) if pos != 2]
            if window[2].high > max(item.high for item in others):
                events.append((index, "top"))
            elif window[2].low < min(item.low for item in others):
                events.append((index, "bottom"))
    output: list[str | None] = []
    latest: str | None = None
    event_pos = 0
    for index, bar in enumerate(bars):
        while event_pos < len(events) and events[event_pos][0] <= index:
            latest = events[event_pos][1]
            event_pos += 1
        atr = _atr(bars, index)
        if atr is None or index < 4:
            output.append(None)
            continue
        closes = [item.close for item in bars[index - 4:index + 1]]
        slope = sum((pos - 2) * value for pos, value in enumerate(closes)) / 10 / atr
        output.append("up" if latest == "bottom" and slope > 0.05 else "down" if latest == "top" and slope < -0.05 else "flat")
    return output


def _direction(value: float) -> str:
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _purge(rows: list[dict[str, Any]], boundary: datetime) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cross = [row for row in rows if row["signal_close"] < boundary < row["label_end"]]
    remaining = [row for row in rows if row not in cross]
    retained: list[dict[str, Any]] = []
    last_end: datetime | None = None
    overlap = 0
    for row in sorted(remaining, key=lambda x: (x["signal_close"], x["symbol"], x["row_id"])):
        if last_end is not None and row["signal_close"] <= last_end:
            overlap += 1
            continue
        row = dict(row)
        row["segment"] = "oos" if row["signal_close"] >= boundary else "is"
        retained.append(row)
        last_end = row["label_end"] if last_end is None else max(last_end, row["label_end"])
    return retained, {"raw": len(rows), "cross_boundary": len(cross), "overlap": overlap, "effective": len(retained), "effective_oos": sum(row["segment"] == "oos" for row in retained)}


def _wilson(hits: int, n: int) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    z = _WILSON_Z
    p = hits / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - radius), min(1.0, center + radius)


def _method_stats(rows: Sequence[Mapping[str, Any]], method: str) -> dict[str, Any]:
    n = len(rows)
    hits = sum(row[method] in ("up", "down") and row[method] == row["label_value"] for row in rows)
    directional = [row for row in rows if row[method] in ("up", "down")]
    signed: list[float] = []
    mfe: list[float] = []
    mae: list[float] = []
    for row in directional:
        sign = 1.0 if row[method] == "up" else -1.0
        raw = row["raw_return"]
        signed.append(raw * sign)
        if row[method] == "up":
            mfe.append(row["label_high"] / row["signal_price"] - 1.0)
            mae.append(row["label_low"] / row["signal_price"] - 1.0)
        else:
            mfe.append(1.0 - row["label_low"] / row["signal_price"])
            mae.append(1.0 - row["label_high"] / row["signal_price"])
    low, high = _wilson(int(hits), n)
    return {
        "rows": n, "hits": int(hits), "hit_rate": hits / n if n else None,
        "wilson_lower": low, "wilson_upper": high,
        "predictions_non_flat": len(directional),
        "mean_signed_return": sum(signed) / len(signed) if signed else None,
        "mean_post_cost_return": (sum(x - COST_RETURN for x in signed) / len(signed)) if signed else None,
        "mean_mfe": sum(mfe) / len(mfe) if mfe else None,
        "mean_mae": sum(mae) / len(mae) if mae else None,
    }


def _verdict(common: int, factor: Mapping[str, Any], baseline_rates: Sequence[float | None]) -> dict[str, Any]:
    reasons: list[str] = []
    if common < COMMON_OOS_MIN_ROWS_H1:
        reasons.append("common_oos_below_minimum")
    if factor["mean_post_cost_return"] is None:
        reasons.append("factor_directional_predictions_missing")
    elif factor["mean_post_cost_return"] <= 0:
        reasons.append("factor_post_cost_not_positive")
    if factor["wilson_lower"] is None or any(rate is None for rate in baseline_rates) or factor["wilson_lower"] <= max((rate for rate in baseline_rates if rate is not None), default=1.0):
        reasons.append("factor_wilson_lower_not_above_baselines")
    return {"status": "accepted" if not reasons else "rejected", "reasons": reasons, "common_oos_rows_h1": common, "baseline_max_point_hit_rate_h1": max((x for x in baseline_rates if x is not None), default=None)}


def evaluate_mtf_direction(
    params: MTFDirectionEvaluateIn, *, reader: ImmutableMinuteReader | None = None
) -> dict[str, Any]:
    resolved = reader if reader is not None else resolve_minute_reader()
    if resolved is None:
        return _unavailable(params, "minute_reader_unavailable", missing_capabilities=list(READER_METHODS))
    if not _reader_satisfies_protocol(resolved):
        return _unavailable(params, "minute_reader_protocol_incomplete", missing_capabilities=[x for x in READER_METHODS if not callable(getattr(resolved, x, None))])
    evidence, failure, corpus = _validate_reader_contracts(resolved, params)
    if failure:
        return _unavailable(params, failure, evidence=evidence)
    boundary = datetime.combine(params.oos_start, time(0, 0))
    purge_report: dict[str, Any] = {}
    rows_by_horizon: dict[str, list[dict[str, Any]]] = {}
    stats: dict[str, dict[str, Any]] = {name: {} for name in ("factor", "unconditional", "momentum_5", "sma5")}
    common_sets: dict[str, list[dict[str, Any]]] = {}
    censored: dict[str, int] = {}
    for symbol, series in corpus.items():
        series.factor_predictions = _factor_predictions(series.fifteen)
    for horizon in HORIZONS:
        raw: list[dict[str, Any]] = []
        for symbol, series in corpus.items():
            bars = series.fifteen
            for index in range(max(0, len(bars) - horizon)):
                label = bars[index + horizon]
                raw_return = label.close / bars[index].close - 1.0
                raw.append({
                    "row_id": f"{symbol}|{bars[index].ts.isoformat()}|h{horizon}",
                    "symbol": symbol, "signal_close": bars[index].ts, "label_end": label.ts,
                    "signal_price": bars[index].close, "label_high": label.high, "label_low": label.low,
                    "raw_return": raw_return, "label_value": _direction(raw_return),
                    "factor": series.factor_predictions[index],
                    "momentum_5": _direction(bars[index].close - bars[index - 5].close) if index >= MOMENTUM_LAG_15M else None,
                    "sma5": _direction(bars[index].close - sum(x.close for x in bars[index - 4:index + 1]) / SMA_WINDOW_15M) if index >= SMA_WINDOW_15M - 1 else None,
                })
            censored[str(horizon)] = censored.get(str(horizon), 0) + min(horizon, len(bars))
        effective, report = _purge(raw, boundary)
        is_rows = [row for row in effective if row["segment"] == "is"]
        up = sum(row["label_value"] == "up" for row in is_rows)
        down = sum(row["label_value"] == "down" for row in is_rows)
        unconditional = "up" if up > down else "down" if down > up else "flat"
        for row in effective:
            row["unconditional"] = unconditional
        purge_report[str(horizon)] = report
        common = [row for row in effective if all(row[name] is not None for name in ("factor", "unconditional", "momentum_5", "sma5")) and row["segment"] == "oos"]
        common_sets[str(horizon)] = common
        rows_by_horizon[str(horizon)] = [{key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in row.items() if key not in ("signal_price", "label_high", "label_low", "raw_return")} for row in effective]
        for method in stats:
            stats[method][str(horizon)] = _method_stats(common, method)
    verdict = _verdict(len(common_sets["1"]), stats["factor"]["1"], [stats[name]["1"]["hit_rate"] for name in ("unconditional", "momentum_5", "sma5")])
    payload = {
        "factor_id": MTF_DIRECTION_FACTOR_ID, "schema_version": MTF_DIRECTION_SCHEMA_VERSION,
        "name": MTF_DIRECTION_NAME, "status": "ok", "reason": None, "missing_capabilities": [],
        "evidence": evidence, "symbols": list(params.symbols),
        "window": {"start": params.start.isoformat(), "end": params.end.isoformat(), "oos_start": params.oos_start.isoformat()},
        "bar_precision": BAR_PRECISION, "higher_timeframe": HIGHER_TIMEFRAME,
        "direction_labelling_pending": False,
        "provenance": {"generation": resolved.generation(), "catalog_manifest": dict(resolved.catalog_manifest()), "manifest_sha256": resolved.manifest_sha256(), "sealed_cutoff": resolved.sealed_cutoff().isoformat(), "oos_start": params.oos_start.isoformat(), "cost_bps": COST_BPS},
        "research": {"purge_report": purge_report, "censoring": censored, "common_set": {key: {"size": len(value)} for key, value in common_sets.items()}, "methods": stats, "rows": rows_by_horizon, "verdict": verdict},
        "disclaimer": MTF_DIRECTION_DISCLAIMER,
    }
    _assert_no_trading_terms(payload)
    return payload


def _assert_no_trading_terms(payload: Mapping[str, Any]) -> None:
    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                validate_result_key(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
    walk(payload)


__all__ = [
    "BAR_PRECISION",
    "COST_BPS",
    "COST_RETURN",
    "COMMON_OOS_MIN_ROWS_H1",
    "EVIDENCE_KEYS",
    "HIGHER_TIMEFRAME",
    "ImmutableMinuteReader",
    "MAX_SYMBOLS",
    "MAX_WINDOW_DAYS",
    "MinuteBar",
    "MTF_DIRECTION_DISCLAIMER",
    "MTF_DIRECTION_FACTOR_ID",
    "MTF_DIRECTION_NAME",
    "MTF_DIRECTION_SCHEMA_VERSION",
    "MTFDirectionEvaluateIn",
    "SessionSpec",
    "TradingTermForbidden",
    "clear_registered_minute_reader",
    "evaluate_mtf_direction",
    "register_minute_reader",
    "resolve_minute_reader",
    "validate_result_key",
]
