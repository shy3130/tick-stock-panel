"""证据新鲜度判定 (移植自 YMOS 证据新鲜度规则)。

纯函数: 对 evidence 的 as_of / source / TTL / required 字段做
fresh / stale / missing / unknown 四态判定, 输出结构化 verdict。

设计依据 (YMOS):
- 输入新鲜度表 (示例_策略分析_DEMO §2): 每项输入标注状态 / 截止时间 /
  是否可用于动作判断; 账户快照缺失 → data_incomplete, 不能计算仓位。
- 数据口径 (SOP_策略分析 §数据口径): 快照必须显示 asOf; 过期数据可用于
  恢复展示, 不能用于声称实时触发。
- D 类根因 (SOP_内核周期审计 Step 4): 驱动数据缺失、过期或失真。
- 证据分类 (ymos-diagnosis Step A2): 前提分为 verified / stale / missing / opinion。
- fail-closed (SOP_内核周期审计 §防止系统学会同意自己): 失效证据只能保持或收紧
  门禁, 永不自动放行; 放宽类修改需额外举证。

安全不变量:
1. 非 fresh verdict → required_action ≠ "use" (永不自动放行)。
2. 多证据聚合取最保守 (最严格) 的个体结果。
3. 未来时间 / 不可解析 / 无 TTL → unknown (不可判定, fail-closed)。
4. now 可注入, 保证纯函数确定性和可测试。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── verdict / action 常量 ──────────────────────────────────
VERDICT_FRESH = "fresh"        # 在 TTL 窗口内, 来源完整, 必填字段齐全
VERDICT_STALE = "stale"        # as_of 存在但已超出 TTL
VERDICT_MISSING = "missing"    # 必填字段 / as_of / source 缺失
VERDICT_UNKNOWN = "unknown"    # 无法判定 (未来时间 / 不可解析 / 无 TTL)
VERDICTS = (VERDICT_FRESH, VERDICT_STALE, VERDICT_MISSING, VERDICT_UNKNOWN)

ACTION_USE = "use"              # fresh → 可用于动作判断
ACTION_REFRESH = "refresh"     # stale → 刷新后再用
ACTION_COLLECT = "collect"     # missing → 采集缺失数据
ACTION_INVESTIGATE = "investigate"  # unknown → 排查问题

# verdict → required_action (不可逆映射, 非 fresh 永不 "use")
_ACTION: dict[str, str] = {
    VERDICT_FRESH: ACTION_USE,
    VERDICT_STALE: ACTION_REFRESH,
    VERDICT_MISSING: ACTION_COLLECT,
    VERDICT_UNKNOWN: ACTION_INVESTIGATE,
}

# 严重度 (聚合时取最高): missing > unknown > stale > fresh
_SEVERITY: dict[str, int] = {
    VERDICT_FRESH: 0,
    VERDICT_STALE: 1,
    VERDICT_UNKNOWN: 2,
    VERDICT_MISSING: 3,
}

MODULE_VERSION = "evidence-freshness-v1"


# ── 输入 / 输出数据结构 ───────────────────────────────────
@dataclass(frozen=True)
class EvidenceItem:
    """单条证据条目。

    Attributes:
        id: 证据唯一标识 (如 "market_insight", "account_snapshot")。
        as_of: 采集 / 截止时间 (ISO 字符串或 datetime; naive 视为 UTC)。
        source: 来源标识 (provenance); 缺失则证据不可信。
        ttl_seconds: 有效期 (秒); 超出则 stale; 无效则 unknown。
        required_fields: 必须在 ``fields`` 中存在且非 None 的字段名列表。
        fields: 实际数据载荷, 用于校验 required_fields。
    """

    id: str
    as_of: str | datetime | None = None
    source: str | None = None
    ttl_seconds: float | None = None
    required_fields: list[str] | None = None
    fields: dict[str, Any] | None = None


@dataclass(frozen=True)
class FreshnessVerdict:
    """单条证据的新鲜度判定结果 (不可变, 可序列化)。"""

    id: str
    verdict: str               # fresh | stale | missing | unknown
    reason: str                # 人可读的判定理由
    age_seconds: float | None  # 距 as_of 的秒数 (fresh/stale 有值, 否则 None)
    required_action: str       # use | refresh | collect | investigate
    source: str | None         # provenance 回显
    as_of: str | None          # 归一化 ISO 字符串 (不可解析时为 None)
    ttl_seconds: float | None  # TTL 回显
    missing_fields: list[str] = field(default_factory=list)  # 缺失的必填字段名

    @property
    def usable_for_action(self) -> bool:
        """是否可用于动作判断 — 仅 fresh 为 True。"""
        return self.verdict == VERDICT_FRESH

    def to_dict(self) -> dict[str, Any]:
        """序列化为 plain dict (保留 provenance 字段)。"""
        return asdict(self)


@dataclass(frozen=True)
class AggregateVerdict:
    """多证据聚合判定结果。

    取最保守 (最严格) 的个体 verdict; usable_for_action 仅当全部 fresh。
    """

    verdict: str                  # 最严重的个体 verdict
    required_action: str          # 对应 required_action
    total: int                    # 证据总数
    by_verdict: dict[str, int]    # 各 verdict 的计数
    usable_for_action: bool       # 是否全部 fresh
    worst_items: list[str]        # 最严重 verdict 的证据 id
    details: list[FreshnessVerdict]  # 个体明细
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 plain dict (递归序列化 details)。"""
        d = asdict(self)
        d["details"] = [v.to_dict() for v in self.details]
        return d


# ── 工具函数 ───────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clean_str(v: Any) -> str | None:
    """非空字符串 → strip 后返回; 否则 None。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _positive_float(v: Any) -> float | None:
    """正浮点 → 返回; None / 非数值 / ≤ 0 / bool → None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None


def _parse_as_of(raw: Any) -> datetime | None:
    """解析 as_of 为 timezone-aware datetime。

    - datetime 对象: naive → 视为 UTC; aware → 原样。
    - ISO 字符串: 用 ``fromisoformat`` 解析 (Python 3.11+ 支持 'Z')。
    - 其他类型 / 解析失败 → None。
    """
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _check_required_fields(item: EvidenceItem) -> list[str]:
    """返回缺失 (不存在或值为 None) 的必填字段名列表。"""
    req = item.required_fields
    if not req:
        return []
    fields = item.fields or {}
    return [f for f in req if f not in fields or fields[f] is None]


def _coerce_evidence(item: EvidenceItem | dict[str, Any]) -> EvidenceItem:
    """dict → EvidenceItem; 已是 dataclass 则原样返回。"""
    if isinstance(item, EvidenceItem):
        return item
    if isinstance(item, dict):
        return EvidenceItem(
            id=str(item.get("id", "")),
            as_of=item.get("as_of") or item.get("asOf"),
            source=item.get("source"),
            ttl_seconds=item.get("ttl_seconds") or item.get("ttlSeconds"),
            required_fields=item.get("required_fields") or item.get("requiredFields"),
            fields=item.get("fields"),
        )
    raise TypeError(f"EvidenceItem | dict expected, got {type(item).__name__}")


def _make_verdict(
    *,
    item: EvidenceItem,
    verdict: str,
    reason: str,
    age_seconds: float | None = None,
    as_of_iso: str | None = None,
    missing_fields: list[str] | None = None,
) -> FreshnessVerdict:
    return FreshnessVerdict(
        id=item.id,
        verdict=verdict,
        reason=reason,
        age_seconds=age_seconds,
        required_action=_ACTION[verdict],
        source=item.source,
        as_of=as_of_iso,
        ttl_seconds=_positive_float(item.ttl_seconds),
        missing_fields=list(missing_fields or []),
    )


# ── 核心: 单条证据判定 ─────────────────────────────────────
def assess_evidence(
    item: EvidenceItem | dict[str, Any],
    *,
    now: datetime | None = None,
) -> FreshnessVerdict:
    """判定单条证据的新鲜度。

    纯函数: 相同输入 (含相同 ``now``) 必定产出相同 verdict。
    判定优先级 (先命中先返回, 均为 fail-closed):

    1. source 缺失         → missing  (无 provenance)
    2. as_of 缺失          → missing  (无采集时间)
    3. 必填字段缺失        → missing  (报告缺失字段)
    4. as_of 不可解析      → unknown  (无法判定)
    5. as_of 晚于 now      → unknown  (未来时间, 可疑)
    6. TTL 缺失 / 无效     → unknown  (无新鲜度规则)
    7. age > TTL           → stale    (过期)
    8. 全部通过            → fresh    (可用)

    Args:
        item: 证据条目 (EvidenceItem 或等价 dict)。
        now:  当前时间 (默认 UTC now; 测试注入以保证确定性)。

    Returns:
        FreshnessVerdict — 不可变, 可序列化, 保留 provenance。
    """
    now = now or _utcnow()
    item = _coerce_evidence(item)

    # 1. source 缺失 → missing
    source = _clean_str(item.source)
    if not source:
        return _make_verdict(
            item=item, verdict=VERDICT_MISSING,
            reason=f"证据 {item.id} 缺少来源 (source)",
        )

    # 2. as_of 缺失 (None 或空字符串) → missing
    as_of_raw = item.as_of
    if isinstance(as_of_raw, str) and not as_of_raw.strip():
        as_of_raw = None
    if as_of_raw is None:
        return _make_verdict(
            item=item, verdict=VERDICT_MISSING,
            reason=f"证据 {item.id} 缺少采集时间 (as_of)",
        )

    # 3. 必填字段缺失 → missing
    missing_fields = _check_required_fields(item)
    if missing_fields:
        return _make_verdict(
            item=item, verdict=VERDICT_MISSING,
            reason=f"证据 {item.id} 必填字段缺失: {', '.join(missing_fields)}",
            missing_fields=missing_fields,
        )

    # 4. as_of 不可解析 → unknown
    as_of_dt = _parse_as_of(as_of_raw)
    if as_of_dt is None:
        return _make_verdict(
            item=item, verdict=VERDICT_UNKNOWN,
            reason=f"证据 {item.id} 的 as_of 不可解析: {as_of_raw!r}",
        )
    as_of_iso = as_of_dt.astimezone(UTC).isoformat()

    # 5. as_of 晚于 now → unknown (未来时间, 可疑)
    if as_of_dt > now:
        return _make_verdict(
            item=item, verdict=VERDICT_UNKNOWN,
            reason=f"证据 {item.id} 的 as_of ({as_of_iso}) 晚于当前时间, 无法判定新鲜度",
            as_of_iso=as_of_iso,
        )

    # 6. TTL 缺失 / 无效 → unknown (无新鲜度规则)
    ttl = _positive_float(item.ttl_seconds)
    if ttl is None:
        return _make_verdict(
            item=item, verdict=VERDICT_UNKNOWN,
            reason=f"证据 {item.id} 缺少有效的 TTL 规则 (ttl_seconds), 无法判定新鲜度",
            as_of_iso=as_of_iso,
        )

    # 7-8. age vs TTL
    age = (now - as_of_dt).total_seconds()
    if age > ttl:
        return _make_verdict(
            item=item, verdict=VERDICT_STALE,
            reason=f"证据 {item.id} 已过期: age {age:.0f}s > TTL {ttl:.0f}s",
            age_seconds=age, as_of_iso=as_of_iso,
        )

    return _make_verdict(
        item=item, verdict=VERDICT_FRESH,
        reason=f"证据 {item.id} 新鲜: age {age:.0f}s ≤ TTL {ttl:.0f}s",
        age_seconds=age, as_of_iso=as_of_iso,
    )


# ── 核心: 多证据聚合 ───────────────────────────────────────
def assess_evidences(
    items: list[EvidenceItem | dict[str, Any]],
    *,
    now: datetime | None = None,
) -> AggregateVerdict:
    """聚合判定多条证据的新鲜度。

    取最保守 (最严重) 的个体 verdict 作为聚合结果。
    ``usable_for_action`` 仅当 **全部** 证据为 fresh。

    空列表 → unknown (无证据可评估, fail-closed)。

    Args:
        items: 证据条目列表。
        now:  当前时间 (默认 UTC now)。

    Returns:
        AggregateVerdict — 含个体明细和聚合统计。
    """
    now = now or _utcnow()

    if not items:
        return AggregateVerdict(
            verdict=VERDICT_UNKNOWN,
            required_action=ACTION_INVESTIGATE,
            total=0,
            by_verdict={},
            usable_for_action=False,
            worst_items=[],
            details=[],
            reason="无证据可评估",
        )

    details = [assess_evidence(item, now=now) for item in items]
    by_verdict: dict[str, int] = dict(Counter(d.verdict for d in details))

    # 最严重 verdict; 全部 fresh 时 worst_items 为空 (无需标记)
    worst_verdict = max(by_verdict, key=lambda v: _SEVERITY[v])
    worst_items = (
        [] if worst_verdict == VERDICT_FRESH
        else [d.id for d in details if d.verdict == worst_verdict]
    )

    usable = all(d.verdict == VERDICT_FRESH for d in details)

    shown = worst_items[:5]
    reason = (
        f"全部 {len(details)} 条证据新鲜"
        if usable
        else f"{len(worst_items)} 条证据为 {worst_verdict}: {', '.join(shown)}"
    )

    return AggregateVerdict(
        verdict=worst_verdict,
        required_action=_ACTION[worst_verdict],
        total=len(details),
        by_verdict=by_verdict,
        usable_for_action=usable,
        worst_items=worst_items,
        details=details,
        reason=reason,
    )


__all__ = [
    # 常量
    "VERDICT_FRESH", "VERDICT_STALE", "VERDICT_MISSING", "VERDICT_UNKNOWN", "VERDICTS",
    "ACTION_USE", "ACTION_REFRESH", "ACTION_COLLECT", "ACTION_INVESTIGATE",
    "MODULE_VERSION",
    # 数据结构
    "EvidenceItem", "FreshnessVerdict", "AggregateVerdict",
    # 函数
    "assess_evidence", "assess_evidences",
]
