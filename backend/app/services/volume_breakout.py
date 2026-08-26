"""量价序列突破研究因子 — volume_breakout_v1（独立、只读、fail-closed）。

设计定稿见 docs/ISSUE-14/final-design.md 与 docs/ISSUE-14/plan-v2.md。
本次交付仅为显式 fail-closed 契约：生产 generation-pinned canonical reader、
PIT eligible-universe 快照与版本化交易所 calendar 在当前仓库不存在，事件
状态机与 OOS walk-forward 亦未实现；任何评估请求都返回结构化 unavailable，
不产出事件、基线或 OOS 效果结论。

边界：

- 读取边界：只接受 generation-pinned sealed reader（构造注入）；禁止
  ``get_enriched_range`` 合并 overlay、当前 universe 或日线近似替代。
- 能力门禁（优先于一切输出）：pinned reader、PIT eligible-universe、
  版本化 calendar 任一缺失 → 整份评估 ``unavailable`` + reasons，不降级、
  不猜口径。
- 诚实声明：即使三项能力齐备，事件状态机/OOS 未实现前状态保持
  ``unavailable``（UNIMPLEMENTED_REASONS 恒定携带），绝不编造命中。
- 输出边界：固定字段 envelope；证据/事件键禁止交易语义（buy/sell/
  target/stop/action/entry/exit/position/order/long/short/hold/trade）。
- 产品边界：不接 short_pool、不进 Agent 工具、不改交易事实流、
  不给任何交易建议。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal, Protocol

import polars as pl
from pydantic import BaseModel, ConfigDict


logger = logging.getLogger(__name__)

FACTOR_ID = "volume_breakout_v1"
FACTOR_VERSION = 1
FACTOR_NAME = "量价序列突破（研究）"
FACTOR_DESCRIPTION = (
    "放量（raw volume 与 amount 双 P90）后 3-15 市场日箱体整理冻结，"
    "raw_close 越过冻结箱体上/下沿确认突破的日线事件研究因子契约；"
    "当前仅交付 fail-closed 契约，仅输出能力状态与删失原因，无任何交易语义"
)
REACHABILITY = "daily_price_only"

# ── 冻结契约参数（final-design 逐字锁定，实现 reader 前不得改动） ──────────
REFERENCE_WINDOW = 20            # 放量事件日 E 前严格 20 个有效市场日（分位参考窗）
VOLUME_PERCENTILE = 0.90         # raw volume 与 amount 各自 P90，须同时满足
CONSOLIDATION_MIN_DAYS = 3       # 整理窗口下限（完整市场日，自 E+1 起）
CONSOLIDATION_MAX_DAYS = 15      # 整理窗口上限，超过未冻结 → 事件失败不重开
BOX_WIDTH_MAX = 0.12             # 整理日 raw_high-low 箱体宽度上限（12%）
FORWARD_HORIZONS = (1, 5, 10, 20)  # 评价 horizon（自 T+1 下一可交易 bar 起）

#: 事件变体（同一冻结箱体可各自独立成立）
VARIANT_UP_BREAKOUT = "up_breakout"
VARIANT_DOWN_BREAKOUT = "down_breakout"
VARIANTS = (VARIANT_UP_BREAKOUT, VARIANT_DOWN_BREAKOUT)

#: sealed 日线必须提供的 raw 字段；缺任一/非正值删失（不假设 raw_open）。
REQUIRED_RAW_COLUMNS = ("raw_high", "raw_low", "raw_close", "volume", "amount")

#: 恒定未实现声明：状态机与 OOS 未落地前，即使能力齐备也不产出事件。
UNIMPLEMENTED_REASONS = (
    "event_state_machine_not_implemented",
    "oos_walkforward_not_implemented",
)

# 证据/事件字段禁用的交易语义词（子串匹配，小写）。
_BANNED_TRADING_TOKENS = (
    "buy", "sell", "target", "stop", "action", "entry", "exit",
    "position", "order", "long", "short", "hold", "trade",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class VolumeBreakoutFactor(_StrictModel):
    factor_id: str
    version: int
    name: str
    description: str
    reachability: Literal["daily_price_only"]


class VolumeBreakoutRequest(_StrictModel):
    start: date
    end: date
    symbols: list[str] | None = None


class VolumeBreakoutCapabilities(_StrictModel):
    generation_pinned_reader: bool = False
    pit_eligible_universe: bool = False
    versioned_exchange_calendar: bool = False


class VolumeBreakoutResponse(_StrictModel):
    factor: VolumeBreakoutFactor
    status: Literal["unavailable"]
    unavailable_reasons: list[str]
    request: VolumeBreakoutRequest
    capabilities: VolumeBreakoutCapabilities
    parameters: dict[str, Any]
    provenance: dict[str, Any]
    coverage: None
    events: list[Any]
    clusters: list[Any]
    censored: list[Any]
    note: str


class GenerationPinnedDailyReader(Protocol):
    """generation-pinned sealed reader 契约（当前仓库尚无实现 → fail-closed）。

    ``daily_bars`` 必须提供 ``date`` 与 ``REQUIRED_RAW_COLUMNS`` 全部 raw 字段；
    ``generation`` 返回 manifest 字节哈希等代标识，进入 provenance。
    """

    def generation(self) -> str: ...

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...


class PitEligibleUniverse(Protocol):
    """PIT eligible-universe 快照契约（当前仓库尚无实现 → fail-closed）。

    事件日 E 的口径：``effective_from <= E <= effective_to`` 且
    ``available_at <= E`` 的最新快照；无唯一快照删失并记录逐事件 hash。
    """

    def as_of(self) -> date: ...

    def snapshot_hash(self) -> str: ...

    def eligible_symbols(self, event_date: date) -> list[str]: ...


class VersionedExchangeCalendar(Protocol):
    """版本化交易所 calendar 契约（当前仓库尚无实现 → fail-closed）。

    标的 status（停牌/未上市）另由 PIT listing/trading records 给出，
    不能从 bars 推导；市场开市缺 bar 与停牌/未上市分别计数。
    """

    def version(self) -> str: ...

    def market_days(self, start: date, end: date) -> list[date]: ...


#: repository 上用于发现能力的 duck-type 属性名（均不存在 → unavailable）。
PINNED_READER_ATTR = "generation_pinned_daily_reader"
PIT_UNIVERSE_ATTR = "pit_eligible_universe"
CALENDAR_ATTR = "versioned_exchange_calendar"


def _resolve_capability(repo: Any, attr: str, required: tuple[str, ...]) -> Any | None:
    """按属性名 + 方法形状解析能力；缺属性或方法不齐即 None。"""
    capability = getattr(repo, attr, None)
    if capability is None:
        return None
    return capability if all(callable(getattr(capability, name, None)) for name in required) else None


def resolve_pinned_reader(repo: Any) -> GenerationPinnedDailyReader | None:
    """从 repository 解析完整 generation-pinned reader；缺能力即 None。"""
    return _resolve_capability(repo, PINNED_READER_ATTR, ("generation", "daily_bars"))


def resolve_pit_universe(repo: Any) -> PitEligibleUniverse | None:
    """从 repository 解析完整 PIT eligible-universe；缺能力即 None。"""
    return _resolve_capability(
        repo, PIT_UNIVERSE_ATTR, ("as_of", "snapshot_hash", "eligible_symbols")
    )


def resolve_versioned_calendar(repo: Any) -> VersionedExchangeCalendar | None:
    """从 repository 解析完整版本化 calendar；缺能力即 None。"""
    return _resolve_capability(repo, CALENDAR_ATTR, ("version", "market_days"))


# ── 交易语义禁令 ──────────────────────────────────────────────────────────


def assert_no_trading_tokens(name: str) -> None:
    """字段/键名含交易语义词时 fail-closed（内部契约守卫）。"""
    lowered = name.lower()
    for token in _BANNED_TRADING_TOKENS:
        if token in lowered:
            raise ValueError(f"trading semantics token {token!r} forbidden in field {name!r}")


def _validate_keys_no_trading_tokens(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_trading_tokens(str(key))
            _validate_keys_no_trading_tokens(value)
    elif isinstance(payload, list):
        for item in payload:
            _validate_keys_no_trading_tokens(item)


# ── 能力门禁 envelope ─────────────────────────────────────────────────────


def _factor_meta() -> dict[str, Any]:
    return {
        "factor_id": FACTOR_ID,
        "version": FACTOR_VERSION,
        "name": FACTOR_NAME,
        "description": FACTOR_DESCRIPTION,
        "reachability": REACHABILITY,
    }


def _locked_parameters() -> dict[str, Any]:
    return {
        "reference_window_market_days": REFERENCE_WINDOW,
        "volume_percentile": VOLUME_PERCENTILE,
        "consolidation_window_days": [CONSOLIDATION_MIN_DAYS, CONSOLIDATION_MAX_DAYS],
        "box_width_max": BOX_WIDTH_MAX,
        "forward_horizons_market_days": list(FORWARD_HORIZONS),
        "variants": list(VARIANTS),
        "required_raw_columns": list(REQUIRED_RAW_COLUMNS),
        "price_scale": "raw",
    }


def unavailable_envelope(
    *,
    start: date,
    end: date,
    reasons: list[str],
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """能力缺失/未实现的结构化 unavailable 载荷（研究状态，非 HTTP 错误）。"""
    payload = {
        "factor": _factor_meta(),
        "status": "unavailable",
        "unavailable_reasons": list(reasons),
        "request": {"start": start, "end": end},
        "capabilities": {
            "generation_pinned_reader": False,
            "pit_eligible_universe": False,
            "versioned_exchange_calendar": False,
            **dict(capabilities or {}),
        },
        "parameters": _locked_parameters(),
        "provenance": {},
        "coverage": None,
        "events": [],
        "clusters": [],
        "censored": [],
        "note": (
            "当前仓库没有生产 generation-pinned canonical reader、PIT eligible-universe"
            " 快照与版本化交易所 calendar；事件状态机与 OOS walk-forward 亦未实现。"
            "本契约显式返回 unavailable，不产出事件/基线/OOS 结论，不以合并 overlay"
            " 或当前 universe 替代，也不构成任何交易建议"
        ),
    }
    return VolumeBreakoutResponse.model_validate(payload).model_dump(mode="json")


# ── 评估入口 ──────────────────────────────────────────────────────────────


def evaluate_volume_breakout(
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    pinned_reader: GenerationPinnedDailyReader | None,
    pit_universe: PitEligibleUniverse | None,
    calendar: VersionedExchangeCalendar | None,
) -> dict[str, Any]:
    """评估 volume_breakout_v1；能力门禁与未实现声明优先于一切输出。"""
    if start > end:
        raise ValueError("start must be <= end")

    reasons: list[str] = []
    if pinned_reader is None:
        reasons.append("generation_pinned_reader_missing")
    if pit_universe is None:
        reasons.append("pit_eligible_universe_missing")
    if calendar is None:
        reasons.append("versioned_exchange_calendar_missing")
    reasons.extend(UNIMPLEMENTED_REASONS)

    payload = unavailable_envelope(
        start=start,
        end=end,
        reasons=reasons,
        capabilities={
            "generation_pinned_reader": pinned_reader is not None,
            "pit_eligible_universe": pit_universe is not None,
            "versioned_exchange_calendar": calendar is not None,
        },
    )
    payload["request"]["symbols"] = list(symbols) if symbols is not None else None
    _validate_keys_no_trading_tokens(payload)
    return payload


__all__ = [
    "FACTOR_ID",
    "FACTOR_VERSION",
    "FACTOR_NAME",
    "REACHABILITY",
    "REFERENCE_WINDOW",
    "VOLUME_PERCENTILE",
    "CONSOLIDATION_MIN_DAYS",
    "CONSOLIDATION_MAX_DAYS",
    "BOX_WIDTH_MAX",
    "FORWARD_HORIZONS",
    "VARIANT_UP_BREAKOUT",
    "VARIANT_DOWN_BREAKOUT",
    "VARIANTS",
    "REQUIRED_RAW_COLUMNS",
    "UNIMPLEMENTED_REASONS",
    "PINNED_READER_ATTR",
    "PIT_UNIVERSE_ATTR",
    "CALENDAR_ATTR",
    "GenerationPinnedDailyReader",
    "VolumeBreakoutFactor",
    "VolumeBreakoutRequest",
    "VolumeBreakoutCapabilities",
    "VolumeBreakoutResponse",
    "PitEligibleUniverse",
    "VersionedExchangeCalendar",
    "resolve_pinned_reader",
    "resolve_pit_universe",
    "resolve_versioned_calendar",
    "assert_no_trading_tokens",
    "unavailable_envelope",
    "evaluate_volume_breakout",
]
