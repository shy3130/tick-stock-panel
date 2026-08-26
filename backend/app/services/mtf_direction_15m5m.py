"""MTF 方向研究 (15m/5m) — 严格 fail-closed 的能力契约服务 (Issue #10)。

设计契约 (docs/ISSUE-10/final-design.md):

  - 独立因子入口 ``mtf_direction_15m5m_v1``。在生产环境没有任何真实
    immutable 分钟 reader 之前, evaluate 恒返回结构化 ``unavailable``,
    绝不伪造方向命中、不输出任何交易语义 (buy/sell/target/stop/
    action/entry/exit/order)。
  - 唯一合法数据源是实现了 :class:`ImmutableMinuteReader` 协议的真实
    分钟 reader: 必须提供 immutable catalog manifest、generation 标识、
    market_days/session 能力、真实 OHLCV minute_bars、以及 sealed
    (point-in-time) cutoff。缺任何一个方法, resolver 返回 ``None``。
  - 现有 price/volume 重建分钟接口 (get_minute/MinuteExecutionData 等)
    不得作为替代输入: 本模块不导入、不回退到任何既有 provider, 也
    不读原始 CSV / 外部行情。
  - 即使注入完整 reader, 当前方向标注器仍返回
    ``direction_evaluator_pending`` 的结构化 unavailable；不会调用 reader
    深层链路、伪造 capability_verified 或宣称 OOS 研究完成。
  - 结果只含方向研究证据与删失原因, 不进入 short_pool / Agent,
    不落盘。

模块导入无副作用; 不依赖 app.* 运行时状态。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

# ── 固定协议标识 (逐字锁定) ───────────────────────────────────────
MTF_DIRECTION_FACTOR_ID = "mtf_direction_15m5m_v1"
MTF_DIRECTION_SCHEMA_VERSION = 1
MTF_DIRECTION_NAME = "15m/5m 多周期方向研究"

MIN_SYMBOLS = 1
MAX_SYMBOLS = 50
MAX_WINDOW_DAYS = 370  # 含最多 1 个闰日的一年内

#: canonical A 股代码: 6 位 ASCII 数字 + SH/SZ/BJ (与 research_analysis 一致)。
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")

#: reader 必须提供的全部能力 (方法名 -> 语义), 顺序即报告顺序。
READER_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("catalog_manifest", "immutable catalog manifest"),
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

    生产环境当前没有实现方; 任何缺方法/非 callable 的候选都视为不存在,
    绝不降级到 price/volume 重建链路。
    """

    def catalog_manifest(self) -> Mapping[str, Any]:
        """immutable catalog manifest (至少含 generation 标识)。"""
        ...

    def generation(self) -> str:
        """数据集不可变 generation 标识 (非空字符串)。"""
        ...

    def market_days(self, start: date, end: date) -> Sequence[date]:
        """[start, end] 内的实际交易日历 (升序)。"""
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


# ── reader registry / resolver (生产恒 None, 仅显式注入) ───────────
_REGISTERED_READER: list[ImmutableMinuteReader] = []


def register_minute_reader(reader: ImmutableMinuteReader) -> None:
    """显式注册真实 reader (测试/未来生产接入点; 当前生产无调用方)。"""
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
    """evaluate 请求: 严格 schema, extra=forbid, 符号去重 + 日期校验。"""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[StrictStr] = Field(min_length=MIN_SYMBOLS, max_length=MAX_SYMBOLS)

    @model_validator(mode="after")
    def _validate_window_and_symbols(self) -> "MTFDirectionEvaluateIn":
        if self.start > self.end:
            raise ValueError("start must be <= end")
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
        "window": {"start": params.start.isoformat(), "end": params.end.isoformat()},
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


def _validate_reader_contracts(
    reader: ImmutableMinuteReader, params: MTFDirectionEvaluateIn
) -> tuple[list[dict[str, Any]], str | None]:
    """逐契约验证注入 reader; 返回 (evidence, failure_reason)。

    任一契约失败即返回具体原因码 — 不抛异常、不部分成功。
    """
    evidence: list[dict[str, Any]] = []

    # 1. immutable catalog manifest: 非空 Mapping。
    try:
        manifest = reader.catalog_manifest()
    except Exception:  # noqa: BLE001 — reader 自身故障按契约缺失处理
        return evidence, "manifest_unreadable"
    if not isinstance(manifest, Mapping) or not dict(manifest):
        evidence.append(_evidence("reader_manifest", "failed", "manifest empty or not a mapping"))
        return evidence, "manifest_missing"
    evidence.append(
        _evidence("reader_manifest", "verified", {"manifest_keys": sorted(map(str, manifest.keys()))[:20]})
    )

    # 2. generation: 非空 str, 且与 manifest 内声明一致 (如声明)。
    try:
        generation = reader.generation()
    except Exception:  # noqa: BLE001
        return evidence, "generation_unreadable"
    if not isinstance(generation, str) or not generation.strip():
        evidence.append(_evidence("generation_id", "failed", "generation empty or not a string"))
        return evidence, "manifest_missing_generation"
    declared = manifest.get("generation")
    if isinstance(declared, str) and declared.strip() and declared != generation:
        evidence.append(
            _evidence(
                "generation_id",
                "failed",
                {"reader_generation": generation, "manifest_generation": declared},
            )
        )
        return evidence, "generation_mismatch_with_manifest"
    evidence.append(_evidence("generation_id", "verified", {"generation": generation}))

    # 3. sealed cutoff: 必须先于验证 bar 之前可读。
    try:
        cutoff = reader.sealed_cutoff()
    except Exception:  # noqa: BLE001
        return evidence, "sealed_cutoff_unreadable"
    if not isinstance(cutoff, datetime):
        evidence.append(_evidence("sealed_cutoff", "failed", "cutoff is not a datetime"))
        return evidence, "sealed_cutoff_invalid"
    evidence.append(_evidence("sealed_cutoff", "verified", {"cutoff": cutoff.isoformat()}))

    # 4. market calendar: 升序、去重、落在请求窗口内。
    try:
        market_days = list(reader.market_days(params.start, params.end))
    except Exception:  # noqa: BLE001
        return evidence, "market_days_unreadable"
    days_sorted = sorted(set(market_days))
    if not market_days or market_days != days_sorted:
        evidence.append(
            _evidence("market_days", "failed", {"days": len(market_days), "sorted_unique": market_days == days_sorted})
        )
        return evidence, "market_days_invalid"
    evidence.append(_evidence("market_days", "verified", {"days": len(market_days)}))

    # 5. 逐 symbol × day 验证 session / OHLCV / timestamp / cutoff。
    covered: dict[str, int] = {symbol: 0 for symbol in params.symbols}
    for symbol in params.symbols:
        for day in market_days:
            try:
                spec = reader.session(symbol, day)
            except Exception:  # noqa: BLE001
                evidence.append(
                    _evidence("session_continuity", "failed", {"symbol": symbol, "day": day.isoformat(), "error": "session_unreadable"})
                )
                return evidence, "session_unreadable"
            if not isinstance(spec, SessionSpec) or spec.open_time >= spec.close_time:
                evidence.append(
                    _evidence("session_continuity", "failed", {"symbol": symbol, "day": day.isoformat(), "error": "invalid_session_spec"})
                )
                return evidence, "session_invalid"

            try:
                bars = list(reader.minute_bars(symbol, day))
            except Exception:  # noqa: BLE001
                evidence.append(
                    _evidence("ohlcv_integrity", "failed", {"symbol": symbol, "day": day.isoformat(), "error": "bars_unreadable"})
                )
                return evidence, "minute_bars_unreadable"
            if not bars:
                evidence.append(
                    _evidence("window_coverage", "failed", {"symbol": symbol, "day": day.isoformat(), "error": "no_bars"})
                )
                return evidence, "minute_bars_empty"

            prev_ts: datetime | None = None
            for bar in bars:
                if not isinstance(bar, MinuteBar):
                    evidence.append(
                        _evidence("ohlcv_integrity", "failed", {"symbol": symbol, "day": day.isoformat(), "error": "non MinuteBar row"})
                    )
                    return evidence, "ohlcv_row_invalid"
                values = (bar.open, bar.high, bar.low, bar.close)
                if not all(_is_finite_positive(v) for v in values):
                    evidence.append(
                        _evidence("ohlcv_integrity", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "non-finite/non-positive price"})
                    )
                    return evidence, "ohlcv_integrity_violation"
                if (
                    bar.high < max(bar.open, bar.close)
                    or bar.low > min(bar.open, bar.close)
                    or bar.high < bar.low
                ):
                    evidence.append(
                        _evidence("ohlcv_integrity", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "high/low inconsistency"})
                    )
                    return evidence, "ohlcv_integrity_violation"
                if (
                    not isinstance(bar.volume, (int, float))
                    or isinstance(bar.volume, bool)
                    or not math.isfinite(float(bar.volume))
                    or float(bar.volume) < 0.0
                ):
                    evidence.append(
                        _evidence("ohlcv_integrity", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "invalid volume"})
                    )
                    return evidence, "ohlcv_integrity_violation"
                if bar.symbol != symbol or bar.ts.date() != day:
                    evidence.append(
                        _evidence("timestamp_index", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "bar outside requested symbol/day"})
                    )
                    return evidence, "timestamp_day_mismatch"
                if not (spec.open_time <= bar.ts.time() <= spec.close_time):
                    evidence.append(
                        _evidence("session_continuity", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "bar outside session"})
                    )
                    return evidence, "session_mismatch"
                if prev_ts is not None and bar.ts <= prev_ts:
                    evidence.append(
                        _evidence("timestamp_index", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "error": "not strictly increasing"})
                    )
                    return evidence, "timestamp_not_monotonic"
                if bar.ts > cutoff:
                    evidence.append(
                        _evidence("sealed_cutoff", "failed", {"symbol": symbol, "day": day.isoformat(), "ts": bar.ts.isoformat(), "cutoff": cutoff.isoformat(), "error": "bar after sealed cutoff"})
                    )
                    return evidence, "bar_after_sealed_cutoff"
                prev_ts = bar.ts
            covered[symbol] += len(bars)

    # 6. 覆盖率汇总 (到达这里说明逐 bar 全部通过)。
    evidence.append(_evidence("session_continuity", "verified", {"symbol_day_pairs": len(params.symbols) * len(market_days)}))
    evidence.append(_evidence("ohlcv_integrity", "verified", {"bars_by_symbol": covered}))
    evidence.append(_evidence("timestamp_index", "verified", {"strictly_increasing": True}))
    evidence.append(
        _evidence("symbol_coverage", "verified", {"symbols": len(params.symbols), "bars_total": sum(covered.values())})
    )
    evidence.append(
        _evidence("window_coverage", "verified", {"market_days": len(market_days), "start": params.start.isoformat(), "end": params.end.isoformat()})
    )
    return evidence, None


def evaluate_mtf_direction(
    params: MTFDirectionEvaluateIn, *, reader: ImmutableMinuteReader | None = None
) -> dict[str, Any]:
    """只在方向标注器真实落地后运行；当前一律 fail-closed。"""
    resolved = reader if reader is not None else resolve_minute_reader()
    if resolved is None:
        return _unavailable(
            params,
            "minute_reader_unavailable",
            missing_capabilities=[name for name, _ in READER_CAPABILITIES],
        )
    if not _reader_satisfies_protocol(resolved):
        missing = [name for name in READER_METHODS if not callable(getattr(resolved, name, None))]
        return _unavailable(
            params,
            "minute_reader_protocol_incomplete",
            missing_capabilities=missing,
        )
    return _unavailable(
        params,
        "direction_evaluator_pending",
        missing_capabilities=["direction_evaluator"],
    )


def _assert_no_trading_terms(payload: Mapping[str, Any]) -> None:
    """递归校验 payload 所有 dict key 不含交易词 (fail-closed 守门)。"""

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key in node.keys():
                validate_result_key(str(key))
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(payload)


__all__ = [
    "BAR_PRECISION",
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
