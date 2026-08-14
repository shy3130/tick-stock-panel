"""决策信号生命周期状态机 — 纯函数 (移植自 daily_stock_analysis DecisionSignal)。

来源: ``daily_stock_analysis`` 的 ``DecisionSignalService`` / ``DecisionSignalRepository``
生命周期语义 (状态流转、时间窗口过期、同源去重、不可逆终态、append-only 审计)。

定位与红线 (与本域 loss_budget / decision_window 一致的单向收紧语义):
- 纯函数: 不落盘、不写现有 trade_events / decision_audit、不创建交易事件、不调用外部数据源;
- 不生成订单、方向或执行动作; 不修改现有交易生命周期 API 契约;
- 终态不可逆: expired / consumed / rejected 一旦进入, 拒绝任何后续迁移 (幂等同态除外);
- 时间窗收紧: 窗口已过的活跃信号, 除过期本身外拒绝一切迁移 (fail-closed);
- append-only: 每次真实迁移追加一条不可变审计记录, 永不修改历史记录。

状态机::

    created    信号已生成, 尚未校验
      → validated          校验通过
      → rejected           校验/门禁拒绝            [终态]
      → expired            时间窗已过               [终态]

    validated  已通过校验
      → eligible           满足可执行前置条件
      → suppressed         被抑制 (去重/风险/对向)
      → rejected           门禁明确拒绝             [终态]
      → expired            时间窗已过               [终态]

    eligible   可被消费
      → consumed           已被交易流程消费         [终态]
      → suppressed         被抑制
      → expired            时间窗已过               [终态]

    suppressed 非软终态, 可恢复
      → eligible           抑制解除, 重新评估
      → rejected           最终拒绝                 [终态]
      → expired            时间窗已过               [终态]

终态 (不可逆): expired / consumed / rejected
活跃态 (可迁移): created / validated / eligible / suppressed

幂等: 迁移到当前已有状态 = 无操作 (不追加审计记录, 不抛错), 对终态同样安全。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_TZ",
    "DEFAULT_INTRADAY_HOURS",
    "HORIZON_DAYS",
    # states
    "STATE_CREATED",
    "STATE_VALIDATED",
    "STATE_ELIGIBLE",
    "STATE_SUPPRESSED",
    "STATE_EXPIRED",
    "STATE_CONSUMED",
    "STATE_REJECTED",
    "ALL_STATES",
    "TERMINAL_STATES",
    "ACTIVE_STATES",
    "TRANSITIONS",
    # reasons
    "REASON_CREATED",
    "REASON_VALIDATED",
    "REASON_ELIGIBLE",
    "REASON_SUPPRESSED",
    "REASON_DEDUP",
    "REASON_RISK_GATE",
    "REASON_EXPIRED",
    "REASON_EXPIRED_AT_CREATION",
    "REASON_CONSUMED",
    "REASON_REJECTED",
    # error
    "SignalLifecycleError",
    # core API
    "create_signal",
    "transition",
    "expire_due",
    # queries
    "is_terminal",
    "is_active",
    "is_past_window",
    "current_state",
    # dedup
    "dedup_key",
    "find_duplicate",
    "create_or_dedup",
    # serialization
    "serialize",
]

SCHEMA_VERSION = 1
DEFAULT_TZ = "Asia/Shanghai"

# 盘中信号默认 TTL (小时); 与 DSA DEFAULT_INTRADAY_TTL_HOURS 对齐。
DEFAULT_INTRADAY_HOURS: dict[str, float] = {"cn": 4.0, "hk": 4.0, "us": 4.0}

# 水平 → 天数 (DSA _horizon_days); swing/long 无自动过期。
HORIZON_DAYS: dict[str, int] = {"1d": 1, "3d": 3, "5d": 5, "10d": 10}

# ── 状态 ─────────────────────────────────────────────────
STATE_CREATED = "created"
STATE_VALIDATED = "validated"
STATE_ELIGIBLE = "eligible"
STATE_SUPPRESSED = "suppressed"
STATE_EXPIRED = "expired"
STATE_CONSUMED = "consumed"
STATE_REJECTED = "rejected"

TERMINAL_STATES: frozenset[str] = frozenset({
    STATE_EXPIRED,
    STATE_CONSUMED,
    STATE_REJECTED,
})
ACTIVE_STATES: frozenset[str] = frozenset({
    STATE_CREATED,
    STATE_VALIDATED,
    STATE_ELIGIBLE,
    STATE_SUPPRESSED,
})
ALL_STATES: frozenset[str] = ACTIVE_STATES | TERMINAL_STATES

# 允许的迁移边 (state → 可达目标集)。
TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_CREATED: frozenset({STATE_VALIDATED, STATE_REJECTED, STATE_EXPIRED}),
    STATE_VALIDATED: frozenset({STATE_ELIGIBLE, STATE_SUPPRESSED, STATE_REJECTED, STATE_EXPIRED}),
    STATE_ELIGIBLE: frozenset({STATE_CONSUMED, STATE_SUPPRESSED, STATE_EXPIRED}),
    STATE_SUPPRESSED: frozenset({STATE_ELIGIBLE, STATE_EXPIRED, STATE_REJECTED}),
    STATE_EXPIRED: frozenset(),
    STATE_CONSUMED: frozenset(),
    STATE_REJECTED: frozenset(),
}

# ── 迁移原因 (默认值, 调用方可覆盖) ──────────────────────
REASON_CREATED = "signal_created"
REASON_VALIDATED = "validation_passed"
REASON_ELIGIBLE = "eligibility_confirmed"
REASON_SUPPRESSED = "suppressed"
REASON_DEDUP = "dedup_hit"
REASON_RISK_GATE = "risk_gate_blocked"
REASON_EXPIRED = "window_expired"
REASON_EXPIRED_AT_CREATION = "expired_at_creation"
REASON_CONSUMED = "consumed"
REASON_REJECTED = "rejected"


class SignalLifecycleError(ValueError):
    """非法迁移或时间窗违规 (终态不可逆 / 窗口已过 / 非法目标)。"""


# ══════════════════════════════════════════════════════════
# 内部时间工具 (与 decision_window.py 一致的 tz-aware 语义)
# ══════════════════════════════════════════════════════════
def _tz(name: str | None) -> ZoneInfo:
    tz_name = (name or DEFAULT_TZ).strip()
    return ZoneInfo(tz_name)


def _now_aware(now: datetime | None, tz: ZoneInfo) -> datetime:
    """``now`` 缺失取当前时刻; naive 按 tz 本地化。"""
    if now is None:
        return datetime.now(tz=tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_iso(value: Any, tz: ZoneInfo) -> datetime | None:
    """解析 ISO 字符串或 datetime 为 tz-aware datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    return None


def _is_past_window(signal: dict[str, Any], now: datetime) -> bool:
    """时间窗是否已过。无 expiresAt → 永不过期。"""
    expires_at = signal.get("expiresAt")
    if expires_at is None:
        return False
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo(DEFAULT_TZ)
    parsed = _parse_iso(expires_at, tz)
    if parsed is None:
        return False
    return now >= parsed


# ══════════════════════════════════════════════════════════
# 过期时间计算 (DSA _expires_at_from_base)
# ══════════════════════════════════════════════════════════
def _compute_expiry(
    *,
    horizon: str | None,
    market: str,
    base: datetime,
    expires_at: datetime | None,
) -> datetime | None:
    """显式 expires_at 优先; 否则按 horizon 推导; 无匹配 → None (无自动过期)。"""
    if expires_at is not None:
        return expires_at
    if horizon in HORIZON_DAYS:
        return base + timedelta(days=HORIZON_DAYS[horizon])
    if horizon == "intraday":
        return base + timedelta(hours=DEFAULT_INTRADAY_HOURS.get(market, 4.0))
    return None


# ══════════════════════════════════════════════════════════
# 审计记录 (append-only)
# ══════════════════════════════════════════════════════════
def _make_record(
    *,
    seq: int,
    from_state: str | None,
    to_state: str,
    at: datetime,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "from": from_state,
        "to": to_state,
        "at": _to_iso(at),
        "reason": reason,
        "actor": actor,
    }


# ══════════════════════════════════════════════════════════
# 核心创建
# ══════════════════════════════════════════════════════════
def create_signal(
    *,
    signal_id: str,
    symbol: str,
    market: str,
    action: str,
    source_type: str,
    horizon: str | None = None,
    market_phase: str | None = None,
    source_ref: str | None = None,
    trace_id: str | None = None,
    expires_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """创建一个 ``created`` 态信号, 返回不可变投影 (不落盘)。

    若创建时时间窗已过, 自动追加 ``created → expired`` 审计记录。

    ``source_ref`` (对应 DSA source_report_id) 和 ``trace_id`` 至少提供一个,
    用于同源去重; 都缺失时去重仅按维度匹配 (不推荐)。
    """
    zone = _tz(tz)
    now_dt = _now_aware(now, zone)
    horizon_str = str(horizon).strip() if horizon is not None else None
    market_str = str(market).strip() or "cn"
    expiry_dt = _compute_expiry(
        horizon=horizon_str,
        market=market_str,
        base=now_dt,
        expires_at=expires_at,
    )

    signal: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "signalId": signal_id,
        "state": STATE_CREATED,
        "createdAt": _to_iso(now_dt),
        "updatedAt": _to_iso(now_dt),
        "expiresAt": _to_iso(expiry_dt),
        # 去重维度 (DSA identity dimensions)
        "sourceType": str(source_type).strip(),
        "symbol": str(symbol).strip(),
        "market": market_str,
        "action": str(action).strip(),
        "horizon": horizon_str,
        "marketPhase": str(market_phase).strip() if market_phase else None,
        # 身份锚点 (DSA source_report_id / trace_id)
        "sourceRef": str(source_ref).strip() if source_ref else None,
        "traceId": str(trace_id).strip() if trace_id else None,
        # 业务载荷 (原样回显, 不解释)
        "payload": dict(payload) if payload else {},
        # append-only 审计
        "transitions": [
            _make_record(
                seq=0,
                from_state=None,
                to_state=STATE_CREATED,
                at=now_dt,
                reason=REASON_CREATED,
                actor=actor,
            ),
        ],
    }

    # 创建时窗口已过 → 自动过期 (保留审计链 created → expired)。
    if _is_past_window(signal, now_dt):
        signal = transition(
            signal,
            STATE_EXPIRED,
            reason=REASON_EXPIRED_AT_CREATION,
            actor="system",
            now=now_dt,
            tz=tz,
        )
    return signal


# ══════════════════════════════════════════════════════════
# 核心迁移
# ══════════════════════════════════════════════════════════
def transition(
    signal: dict[str, Any],
    target: str,
    *,
    reason: str = "",
    actor: str = "system",
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """应用一次状态迁移, 返回新投影 (不修改原 dict, 不落盘)。

    规则:
    - 幂等: target == 当前状态 → 原样返回 (不追加记录)。
    - 终态拒绝: 当前在终态且 target 不同 → 抛 :class:`SignalLifecycleError`。
    - 非法迁移: target 不在允许集 → 抛 :class:`SignalLifecycleError`。
    - 时间窗收紧: 活跃信号窗口已过且 target != expired → 抛 :class:`SignalLifecycleError`。
    """
    if target not in ALL_STATES:
        raise SignalLifecycleError(f"未知目标状态: {target!r}")

    zone = _tz(tz)
    now_dt = _now_aware(now, zone)
    current = signal.get("state")

    # 幂等: 同态无操作。
    if current == target:
        return signal

    # 终态不可逆。
    if current in TERMINAL_STATES:
        raise SignalLifecycleError(
            f"终态 {current!r} 不可迁移到 {target!r}"
        )

    # 非法迁移。
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise SignalLifecycleError(
            f"非法迁移: {current!r} → {target!r} (允许: {sorted(allowed) or '无'})"
        )

    # 时间窗收紧: 窗口已过时只允许过期本身。
    if target != STATE_EXPIRED and _is_past_window(signal, now_dt):
        raise SignalLifecycleError(
            f"信号时间窗已过, 拒绝迁移到 {target!r}; 请先 expire_due"
        )

    seq = len(signal.get("transitions", []))
    record = _make_record(
        seq=seq,
        from_state=current,
        to_state=target,
        at=now_dt,
        reason=reason or target,
        actor=actor,
    )
    new_signal = dict(signal)
    new_signal["state"] = target
    new_signal["updatedAt"] = _to_iso(now_dt)
    new_signal["transitions"] = [*signal.get("transitions", []), record]
    return new_signal


def expire_due(
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """若活跃信号时间窗已过, 迁移到 ``expired``; 否则原样返回 (幂等)。

    终态信号不受影响。
    """
    zone = _tz(tz)
    now_dt = _now_aware(now, zone)
    if signal.get("state") in TERMINAL_STATES:
        return signal
    if not _is_past_window(signal, now_dt):
        return signal
    return transition(
        signal,
        STATE_EXPIRED,
        reason=REASON_EXPIRED,
        actor="system",
        now=now_dt,
        tz=tz,
    )


# ══════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════
def current_state(signal: dict[str, Any]) -> str:
    return signal.get("state", STATE_CREATED)


def is_terminal(signal: dict[str, Any]) -> bool:
    return signal.get("state") in TERMINAL_STATES


def is_active(signal: dict[str, Any]) -> bool:
    return signal.get("state") in ACTIVE_STATES


def is_past_window(
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
) -> bool:
    """时间窗是否已过。终态信号 (无窗口或已过期) 同样判断。"""
    zone = _tz(tz)
    now_dt = _now_aware(now, zone)
    return _is_past_window(signal, now_dt)


# ══════════════════════════════════════════════════════════
# 去重 (DSA create_if_absent identity matching)
# ══════════════════════════════════════════════════════════
_SEP = "\x1f"  # ASCII unit separator — 不会出现在正常字段值中


def dedup_key(signal: dict[str, Any]) -> str:
    """计算确定性去重键。

    维度: ``sourceType, market, symbol, action, horizon, marketPhase``;
    身份锚点: ``sourceRef`` 或 ``traceId`` (至少一个, 否则仅维度匹配)。
    """
    parts = [
        str(signal.get("sourceType") or ""),
        str(signal.get("market") or ""),
        str(signal.get("symbol") or ""),
        str(signal.get("action") or ""),
        str(signal.get("horizon") or ""),
        str(signal.get("marketPhase") or ""),
    ]
    ref = signal.get("sourceRef") or signal.get("traceId")
    if ref:
        parts.append(str(ref))
    return _SEP.join(parts)


def dedup_key_hash(signal: dict[str, Any]) -> str:
    """去重键的稳定 SHA-256 摘要 (用于日志/持久化索引, 16 字符)。"""
    return hashlib.sha256(dedup_key(signal).encode("utf-8")).hexdigest()[:16]


def find_duplicate(
    new_signal: dict[str, Any],
    existing: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """在已有活跃信号中查找同源重复 (DSA create_if_absent 语义)。

    匹配条件:
    - 候选必须处于活跃态 (终态信号不抑制新信号);
    - 去重键完全一致 (维度 + 身份锚点)。

    无身份锚点的新信号不做去重 (与 DSA ``_find_existing_in_session`` 一致)。
    """
    if not new_signal.get("sourceRef") and not new_signal.get("traceId"):
        return None
    new_key = dedup_key(new_signal)
    for sig in existing:
        if not is_active(sig):
            continue
        if dedup_key(sig) == new_key:
            return sig
    return None


def create_or_dedup(
    *,
    existing: Sequence[dict[str, Any]],
    on_duplicate: str = REASON_SUPPRESSED,
    **create_kwargs: Any,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    """创建信号; 若已有同源活跃信号则去重。

    返回 ``(signal, created, duplicate)``:
    - ``created=True``  → 新建信号, duplicate=None;
    - ``created=False`` → 返回已有信号, duplicate=被命中的旧信号。

    调用方可据 ``duplicate`` 决定是否将新信号标记为 suppressed。
    """
    new_signal = create_signal(**create_kwargs)
    duplicate = find_duplicate(new_signal, existing)
    if duplicate is not None:
        return duplicate, False, duplicate
    return new_signal, True, None


# ══════════════════════════════════════════════════════════
# 序列化
# ══════════════════════════════════════════════════════════
def serialize(signal: dict[str, Any]) -> dict[str, Any]:
    """返回 JSON 安全的信号投影 (可直接 json.dumps)。

    验证所有字段可序列化; transitions 按 seq 升序。
    """
    transitions = sorted(
        (dict(r) for r in signal.get("transitions", [])),
        key=lambda r: r.get("seq", 0),
    )
    projection = {
        "schemaVersion": signal.get("schemaVersion", SCHEMA_VERSION),
        "signalId": signal.get("signalId"),
        "state": signal.get("state"),
        "createdAt": signal.get("createdAt"),
        "updatedAt": signal.get("updatedAt"),
        "expiresAt": signal.get("expiresAt"),
        "sourceType": signal.get("sourceType"),
        "symbol": signal.get("symbol"),
        "market": signal.get("market"),
        "action": signal.get("action"),
        "horizon": signal.get("horizon"),
        "marketPhase": signal.get("marketPhase"),
        "sourceRef": signal.get("sourceRef"),
        "traceId": signal.get("traceId"),
        "dedupKey": dedup_key_hash(signal),
        "payload": dict(signal.get("payload") or {}),
        "transitions": transitions,
        "transitionCount": len(transitions),
    }
    # 确保可 JSON 序列化 (fail-fast: 不可序列化的 payload 立即暴露)。
    json.dumps(projection, ensure_ascii=False)
    return projection
