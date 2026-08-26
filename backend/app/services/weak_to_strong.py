"""弱转强涨停事件因子 — 独立 fail-closed 研究契约 (weak_to_strong_v1)。

设计契约 (docs/ISSUE-12/final-design.md、plan-v2.md、review-v2.md):

  - 本模块是独立研究契约, 不复用现有 provider、``signal_limit_up``、
    当前 instruments、事后题材映射或任何交易事实; 不接入 short_pool/Agent。
  - 完整事件主路径要求: run-level immutable manifest 固定 canonical 日线、
    T/T+1 分钟/集合竞价、实际使用的逐笔/盘口 route 的 generation/校验和/
    覆盖, 以及 PIT 制度/ST/流通股本输入的 generation/校验和/覆盖与每条
    记录的 ``available_at``; PIT 记录必须同时满足
    ``effective_at <= signal_time`` 与 ``available_at <= signal_time``。
  - 触板/炸板/回封需要可排序逐笔(同秒稳定序号), "封板"还需对应时点
    历史盘口/封单证据; 缺失时该变体只能输出 ``bar_touched`` 或
    unavailable/censored。集合竞价高开至涨停必须有 auction evidence。
  - 当前生产不存在满足上述能力的数据 reader, 因此本模块只交付显式
    unavailable 契约: 不伪造封板、题材核心、可达性或前向结果。
    即使未来注册了全能力 reader, 事件评估主路径也未实现
    (``status_reason=event_path_not_implemented``), 仍 fail-closed。
  - 所有输出只含证据、删失、manifest、版本与 observed_at; evidence key
    禁止出现交易语义词(方向/动作); core/reachability 状态只允许
    ``unavailable`` 或 ``bar_touched``。

模块导入无副作用, 不 import 任何现有行情/信号/instruments 模块。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── 协议标识与版本 ─────────────────────────────────────────────
WEAK_TO_STRONG_PROTOCOL_ID = "weak_to_strong_v1"
WEAK_TO_STRONG_SCHEMA_VERSION = 1

WEAK_TO_STRONG_DISCLAIMER = (
    "研究评估输出：仅结构化证据、删失与能力状态；"
    "不含交易指令、买卖方向、价格目标或投资建议"
)

MAX_SYMBOLS_PER_REQUEST = 100

# ── 完整事件主路径要求的 reader 能力(顺序即输出顺序) ────────────
REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "immutable_run_manifest",
    "canonical_daily_sealed_reader",
    "timestamped_minute_reader",
    "auction_evidence_reader",
    "sortable_tick_reader",
    "historical_order_book_reader",
    "pit_regime_records",
    "pit_st_records",
    "pit_float_shares_records",
)

# 缺失能力 → 删失原因代码(确定性映射)
_CENSORING_BY_CAPABILITY: dict[str, str] = {
    "immutable_run_manifest": "missing_run_manifest",
    "canonical_daily_sealed_reader": "missing_sealed_daily",
    "timestamped_minute_reader": "missing_timestamped_minute",
    "auction_evidence_reader": "censored_preopen",
    "sortable_tick_reader": "missing_sortable_tick",
    "historical_order_book_reader": "missing_order_book_evidence",
    "pit_regime_records": "missing_pit_regime",
    "pit_st_records": "missing_pit_st",
    "pit_float_shares_records": "missing_pit_float_shares",
}

# ── evidence key 禁交易词(子串匹配, 大小写不敏感) ───────────────
BANNED_EVIDENCE_KEY_TERMS: tuple[str, ...] = (
    # 方向/动作(英)
    "buy",
    "sell",
    "go_long",
    "go_short",
    "long",
    "short",
    "entry",
    "exit",
    "position",
    "stop_loss",
    "stop_profit",
    "take_profit",
    "target_price",
    "trade_signal",
    "order_action",
    # 方向/动作(中)
    "买",
    "卖",
    "加仓",
    "减仓",
    "建仓",
    "开仓",
    "平仓",
    "清仓",
    "止损",
    "止盈",
    "目标价",
    "下单",
    "挂单",
)

EvidenceValue = Union[str, int, float, bool, None]

# ── symbol 规范化 ──────────────────────────────────────────────
# canonical 形式: 6 位数字。输入可选 sh/sz/bj 前缀(可带点)或 .SH/.SZ/.BJ
# 后缀(大小写不敏感); 其余形式(含空白)一律拒绝, 不做静默清洗。
_SYMBOL_RE = re.compile(
    r"^(?:(?:SH|SZ|BJ)\.?)?(\d{6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE
)


def canonicalize_symbol(raw: str) -> str:
    """把输入 symbol 规范化为 6 位数字 canonical 形式; 非法输入 fail-closed。"""
    if not isinstance(raw, str):
        raise ValueError(f"symbol must be a string, got {type(raw).__name__}")
    match = _SYMBOL_RE.match(raw)
    if match is None:
        raise ValueError(
            f"non-canonical symbol {raw!r}: expected 6-digit code, "
            "optionally prefixed sh/sz/bj or suffixed .SH/.SZ/.BJ"
        )
    return match.group(1)


def validate_evidence_keys(keys: Iterable[str]) -> None:
    """evidence key 禁止包含交易语义词; 违反即抛错(fail-closed)。"""
    for key in keys:
        if not isinstance(key, str):
            raise ValueError(f"evidence key must be a string, got {type(key).__name__}")
        lowered = key.lower()
        for term in BANNED_EVIDENCE_KEY_TERMS:
            if term in lowered:
                raise ValueError(
                    f"evidence key {key!r} contains banned trading term {term!r}"
                )


# ── reader 协议与全能力 resolver ───────────────────────────────
class WeakToStrongReader(Protocol):
    """弱转强事件主路径所需数据 reader 的最小协议。

    真实实现必须提供 ``capabilities()`` 声明其满足的
    ``REQUIRED_CAPABILITIES`` 子集; manifest/逐笔/盘口/PIT 细节接口
    随事件主路径实现再定义。
    """

    def capabilities(self) -> frozenset[str]: ...


ReaderFactory = Callable[[], "WeakToStrongReader | None"]

# 生产注册表: 当前为空 — 无任何满足契约的生产 reader。
_READER_FACTORIES: list[ReaderFactory] = []


def register_reader_factory(factory: ReaderFactory) -> None:
    """注册 reader 工厂(测试/未来生产接入点); 不改变 fail-closed 语义。"""
    _READER_FACTORIES.append(factory)


def resolve_weak_to_strong_reader() -> WeakToStrongReader | None:
    """解析第一个具备完整能力的 reader；部分 reader 不遮蔽后续候选。"""
    for factory in tuple(_READER_FACTORIES):
        reader = factory()
        if reader is not None and set(REQUIRED_CAPABILITIES).issubset(reader.capabilities()):
            return reader
    return None


# ── 请求/响应模型(全部 extra=forbid) ───────────────────────────
class WeakToStrongEvaluateRequest(BaseModel):
    """评估请求: symbols 规范化为 6 位数字, 重复(规范化后)即拒绝。"""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS_PER_REQUEST)
    signal_date: date

    @field_validator("symbols")
    @classmethod
    def _canonicalize_symbols(cls, value: list[str]) -> list[str]:
        canonical = [canonicalize_symbol(raw) for raw in value]
        seen: set[str] = set()
        duplicates: list[str] = []
        for symbol in canonical:
            if symbol in seen and symbol not in duplicates:
                duplicates.append(symbol)
            seen.add(symbol)
        if duplicates:
            raise ValueError(
                f"duplicate canonical symbol(s) after normalization: {duplicates}"
            )
        return canonical

    @field_validator("signal_date")
    @classmethod
    def _reject_future_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError(f"signal_date {value.isoformat()} is in the future")
        return value


class ManifestStatus(BaseModel):
    """run-level manifest 状态: 当前契约下只可 unavailable。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["unavailable"]
    missing_capabilities: list[str]


class WeakToStrongSymbolEvaluation(BaseModel):
    """单 symbol 评估结果: fail-closed, 不含前向收益/交易语义。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    status: Literal["unavailable"]
    status_reason: Literal["reader_missing", "event_path_not_implemented"]
    missing_capabilities: list[str]
    core_status: Literal["unavailable", "bar_touched"]
    reachability_status: Literal["unavailable", "bar_touched"]
    censoring: list[str]
    evidence: dict[str, EvidenceValue] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_must_be_canonical(cls, value: str) -> str:
        return canonicalize_symbol(value)

    @field_validator("evidence")
    @classmethod
    def _evidence_keys_must_not_trade(cls, value: dict[str, EvidenceValue]) -> dict[str, EvidenceValue]:
        validate_evidence_keys(value.keys())
        return value


class WeakToStrongEvaluateResponse(BaseModel):
    """评估响应: 版本、observed_at、manifest、逐 symbol 评估与删失。"""

    model_config = ConfigDict(extra="forbid")

    protocol_id: str
    schema_version: int
    signal_date: date
    observed_at: datetime
    manifest: ManifestStatus
    evaluations: list[WeakToStrongSymbolEvaluation]
    disclaimer: str


# ── fail-closed 评估入口 ───────────────────────────────────────
def evaluate_weak_to_strong_v1(
    request: WeakToStrongEvaluateRequest,
) -> WeakToStrongEvaluateResponse:
    """评估弱转强因子: 缺能力/未实现主路径时输出结构化 unavailable。

    不读取任何行情数据, 不产生封板/题材核心/可达性/前向结论。
    """
    reader = resolve_weak_to_strong_reader()
    provided = reader.capabilities() if reader is not None else frozenset()
    missing = [cap for cap in REQUIRED_CAPABILITIES if cap not in provided]
    if missing:
        status_reason: Literal["reader_missing", "event_path_not_implemented"] = (
            "reader_missing"
        )
        censoring = [_CENSORING_BY_CAPABILITY[cap] for cap in missing]
    else:
        # 全能力 reader 也只说明数据就绪; 事件主路径未实现, 仍 fail-closed。
        status_reason = "event_path_not_implemented"
        censoring = ["event_path_not_implemented"]

    evaluations = [
        WeakToStrongSymbolEvaluation(
            symbol=symbol,
            status="unavailable",
            status_reason=status_reason,
            missing_capabilities=list(missing),
            core_status="unavailable",
            reachability_status="unavailable",
            censoring=list(censoring),
            evidence={},
        )
        for symbol in request.symbols
    ]
    return WeakToStrongEvaluateResponse(
        protocol_id=WEAK_TO_STRONG_PROTOCOL_ID,
        schema_version=WEAK_TO_STRONG_SCHEMA_VERSION,
        signal_date=request.signal_date,
        observed_at=datetime.now(UTC),
        manifest=ManifestStatus(
            status="unavailable",
            missing_capabilities=list(missing),
        ),
        evaluations=evaluations,
        disclaimer=WEAK_TO_STRONG_DISCLAIMER,
    )
