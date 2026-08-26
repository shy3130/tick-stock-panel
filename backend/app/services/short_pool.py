"""AI 短线池 — 固定 preset 的确定性短线观察池服务。

设计契约(研究观察用途, 不产生任何交易指令):

  - 策略完全固定: preset ``short_momentum_quality_v1`` 的条件集与排序在
    本模块内逐字锁定, 调用方(Agent)只能选择 ``limit``(5..12, 默认 8)。
    候选由 QueryService 确定性筛选产生; 模型只解释 evidence, 不得
    生成/删除/重排候选, 不给买卖方向、价格或仓位。
  - 复用 canonical 数据链: 只通过 screener_query.QueryService /
    ScreenerQueryRequest 查询, 不直连 DuckDB、不发 HTTP、无外部 fallback。
  - 结果只在请求内返回，``pool_id`` 是规范内容 sha256 前缀(16 hex)，
    用于显式确认时由服务端重算比对；观察池本身不落盘。只有用户确认且
    服务端重新验证 ``dispersed`` 状态后，才写入既有研究假设存储。
  - 所有进入模型上下文的值只来自 QueryService 返回值(rows)与
    validate_query 的 applied conditions, 不引入第二数据源。

模块导入无副作用: app.* 依赖全部在函数体内延迟导入。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

# ── 固定 preset(逐字锁定, 测试断言其不可漂移) ─────────────────────
SHORT_POOL_PRESET_ID = "short_momentum_quality_v1"
SHORT_POOL_PRESET_VERSION = 1
SHORT_POOL_PRESET_NAME = "短线动量质量观察"
SHORT_POOL_PRESET_DESCRIPTION = (
    "以流动性、趋势位置、温和动量、波动与涨停风险约束形成的固定研究观察池"
)
SHORT_POOL_SCHEMA_VERSION = 1

MIN_LIMIT = 5
MAX_LIMIT = 12
DEFAULT_LIMIT = 8

SHORT_POOL_DISCLAIMER = "研究观察池，非投资建议"
SHORT_POOL_AI_ROLE = "AI 只解释证据；不得生成、删除或重排候选；不提供买卖方向、价格或仓位建议"

# 做T研究固定协议: 研究协议标识, 不是既有策略, 也不代表任何可执行信号。
T_RESEARCH_PROTOCOL_ID = "bollinger_volatility_t_research_v1"
T_RESEARCH_PROTOCOL: dict[str, Any] = {
    "protocol_id": T_RESEARCH_PROTOCOL_ID,
    "bar_precision": "5m",
    "lookback_sessions": 120,
    "min_events": 30,
    "signal_lag": "T-1",
    "validation": "strict_walk_forward",
    "baseline": "all_eligible_days",
    "filtered": "market_state=dispersed",
    "round_trip_cost_bps": 20,
    "cost_sensitivity_bps": [10, 20, 30],
    "automatic_run": False,
}
T_RESEARCH_AI_ROLE = (
    "t_research 仅是研究协议草案：protocol_id 是研究协议标识而非既有策略；"
    "仅当市场状态为 dispersed 时允许用户显式确认创建研究假设，"
    "不得自动运行回测，不得给出买卖方向、价格或仓位"
)

T_RESEARCH_RESERVED_TAGS = (
    "做T研究",
    "AI短线研究池",
    "market_concentration_v1",
)

# 条件顺序即 evidence 顺序, 与契约逐字一致。
SHORT_POOL_CONDITIONS: tuple[dict[str, Any], ...] = (
    {"field": "exclude_st", "op": "=", "value": True},
    {"field": "listing_days", "op": ">=", "value": 120},
    {"field": "amount", "op": ">=", "value": 300000000},
    {"field": "turnover_rate", "op": "between", "value": [2, 18]},
    {"field": "above_ma20", "op": "=", "value": True},
    {"field": "momentum_20d", "op": "between", "value": [0.03, 0.25]},
    {"field": "distance_to_60d_high", "op": "between", "value": [-15, 0]},
    {"field": "atr_pct_14", "op": "between", "value": [2, 9]},
    {"field": "vol_ratio_5d", "op": ">=", "value": 1},
    {"field": "change_pct", "op": "between", "value": [-0.03, 0.08]},
    {"field": "limit_up", "op": "=", "value": False},
    {"field": "broken_limit_up", "op": "=", "value": False},
)
SHORT_POOL_ORDER_BY: dict[str, str] = {"field": "momentum_20d", "direction": "desc"}


class ShortPoolLimit(BaseModel):
    """preset 分支唯一可调参数: limit(5..12, 默认 8), 其余一律 forbid。"""

    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)


def build_query_request(limit: int = DEFAULT_LIMIT):
    """按固定 preset 构造 ScreenerQueryRequest；越界或非整数输入 fail-closed。"""
    from app.services.screener_query import QueryOrder, ScreenerQueryRequest

    parsed = ShortPoolLimit(limit=limit)
    return ScreenerQueryRequest(
        conditions=[dict(cond) for cond in SHORT_POOL_CONDITIONS],
        order_by=QueryOrder(**dict(SHORT_POOL_ORDER_BY)),
        limit=parsed.limit,
    )


# ── 展示格式化(确定性, 不引入外部数据) ────────────────────────────
def _display(field: str, value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if field in ("momentum_20d", "change_pct"):
        return f"{float(value) * 100:.2f}%"
    if field in ("turnover_rate", "distance_to_60d_high", "atr_pct_14"):
        return f"{float(value):.2f}%"
    if field == "amount":
        return f"{float(value) / 1e8:.2f}亿元"
    if field == "vol_ratio_5d":
        return f"{float(value):.2f}倍"
    if field == "listing_days":
        return f"{float(value):.0f}天"
    return str(value)


def _criterion(label: str, op: str, target: Any) -> str:
    if isinstance(target, list):
        return f"{label} ∈ [{target[0]}, {target[1]}]"
    if isinstance(target, bool):
        return f"{label} = {'是' if target else '否'}"
    return f"{label} {op} {target}"


def _evidence(row: dict[str, Any], applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐股证据: actual 只取 QueryService 返回行, op/target 来自 applied conditions。"""
    from app.services.screener_query import get_field_spec

    entries: list[dict[str, Any]] = []
    for cond in applied:
        spec = get_field_spec(cond["field"])
        label = spec.label if spec is not None else cond["field"]
        unit = spec.unit if spec is not None and spec.unit is not None else ""
        entries.append(
            {
                "field": cond["field"],
                "label": label,
                "actual": row.get(cond["field"]),
                "display": _display(cond["field"], row.get(cond["field"])),
                "op": cond["op"],
                "target": cond["value"],
                "criterion": _criterion(label, cond["op"], cond["value"]),
                "unit": unit,
            }
        )
    return entries


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _checksum_hex(content: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _pool_id_hex(content: dict[str, Any]) -> str:
    return _checksum_hex(content)[:16]


def run_short_pool(
    app_state: Any,
    limit: int = DEFAULT_LIMIT,
    *,
    market_state_provider: Any = None,
) -> dict[str, Any]:
    """运行固定 preset 的确定性短线观察池；不落盘，返回完整证据封套。"""
    from app.services.agent_research_tools import _require_repo
    from app.services.screener_query import (
        QueryService,
        ScreenerDataUnavailableError,
        ScreenerSemanticError,
        validate_query,
    )

    parsed = ShortPoolLimit(limit=limit)
    req = build_query_request(parsed.limit)
    applied, _order = validate_query(req)
    repo = _require_repo(app_state)
    try:
        result = QueryService(repo).query(req)
    except ScreenerDataUnavailableError as exc:
        raise ValueError(f"短线池数据不可用(字段: {','.join(exc.fields)})") from exc
    except ScreenerSemanticError as exc:
        raise ValueError(f"短线池条件无效 {exc.location}: {exc.reason}") from exc

    rows = [row for row in (result.get("rows") or []) if row.get("symbol")][: parsed.limit]
    applied = list(result.get("applied") or applied)
    as_of = str(result["as_of"])
    total = int(result.get("total") or len(rows))
    count = len(rows)
    candidates = [
        {
            "rank": rank,
            "symbol": str(row["symbol"]),
            "name": str(row.get("name") or row["symbol"]),
            "evidence": _evidence(row, applied),
        }
        for rank, row in enumerate(rows, start=1)
    ]

    # 市场状态(严格 T-1)进入内容寻址输入与返回封套：快照变 → pool_id 变。
    # 读取失败不吞错——观察池依赖它判定研究可用性；错误消息已脱敏无路径。
    from app.services.market_concentration import (
        MarketStateDataError,
        MarketStateSnapshot,
        market_state_for_date,
    )

    try:
        target = date.fromisoformat(as_of)
    except ValueError:
        target = None
    provider = market_state_provider or (
        lambda: market_state_for_date(repo, target) if target else None
    )
    try:
        snapshot = provider()
    except MarketStateDataError as exc:
        raise ValueError(f"市场状态数据不可用: {exc}") from exc
    raw_snapshot = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else snapshot
    try:
        snapshot_dict = MarketStateSnapshot.model_validate(raw_snapshot).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 — 对外固定脱敏契约
        raise ValueError("市场状态快照无效") from exc
    state_value = snapshot_dict["state"]
    t_research = {
        **T_RESEARCH_PROTOCOL,
        "status": (
            "ready_for_confirmation"
            if count > 0 and snapshot_dict.get("available") and state_value == "dispersed"
            else "blocked_by_market_state"
        ),
    }

    content = {
        "schema_version": SHORT_POOL_SCHEMA_VERSION,
        "preset": {
            "preset_id": SHORT_POOL_PRESET_ID,
            "version": SHORT_POOL_PRESET_VERSION,
            "name": SHORT_POOL_PRESET_NAME,
            "description": SHORT_POOL_PRESET_DESCRIPTION,
        },
        "as_of": as_of,
        "count": count,
        "total": total,
        "limit": parsed.limit,
        "conditions": applied,
        "order_by": dict(SHORT_POOL_ORDER_BY),
        "candidates": candidates,
        "market_state": snapshot_dict,
        "t_research": t_research,
    }
    pool_id = _pool_id_hex(content)

    return {
        "status": "success",
        "summary": (
            f"{SHORT_POOL_PRESET_NAME}池(确定性筛选): 命中 {total} 只, "
            f"输出 {count} 只, as_of={as_of}; AI 只解释证据"
        ),
        "pool_id": pool_id,
        "as_of": as_of,
        "count": count,
        "total": total,
        "preset": dict(content["preset"]),
        "candidates": candidates,
        "disclaimer": SHORT_POOL_DISCLAIMER,
        "selection_basis": {
            "conditions": applied,
            "order_by": dict(SHORT_POOL_ORDER_BY),
            "limit": parsed.limit,
            "deterministic": True,
        },
        "ai_role": SHORT_POOL_AI_ROLE,
        "t_research": t_research,
        "market_state": snapshot_dict,
        "next_actions": (
            ["view_stock_detail", "add_to_watchlist", "stage_strategy_backtest"]
            if candidates
            else []
        ),
    }


def build_t_research_hypothesis(pool: dict[str, Any]) -> dict[str, Any]:
    """从服务端重算的分散市场观察池构造唯一可写研究假设。"""
    from app.services.market_concentration import MarketStateSnapshot

    try:
        snapshot = MarketStateSnapshot.model_validate(pool["market_state"])
    except Exception as exc:  # noqa: BLE001 — 对外由 API 转为固定错误
        raise ValueError("市场状态快照无效") from exc
    if (
        not snapshot.available
        or snapshot.state != "dispersed"
        or not snapshot.gates.automatic_research_allowed
        or pool.get("t_research") != {**T_RESEARCH_PROTOCOL, "status": "ready_for_confirmation"}
    ):
        raise ValueError("当前市场状态不允许创建做T研究假设")

    candidates = pool.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("当前观察池没有可研究候选")
    candidate_text = "、".join(
        f"{candidate['name']}（{candidate['symbol']}）" for candidate in candidates
    )
    as_of = str(pool["as_of"])
    return {
        "title": f"做T研究 · AI短线研究池 · {as_of}",
        "thesis": (
            f"候选：{candidate_text}。"
            f"市场状态：分散；严格 T-1：{snapshot.signal_date}。"
            f"研究协议：{T_RESEARCH_PROTOCOL_ID}；"
            "5m、120 个交易日、至少 30 个事件、严格 walk-forward。"
            "仅创建研究假设，不自动运行回测，不输出买卖点。"
        ),
        "status": "exploring",
        "tags": [
            *T_RESEARCH_RESERVED_TAGS,
            f"short_pool:{pool['pool_id']}",
        ],
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "SHORT_POOL_AI_ROLE",
    "SHORT_POOL_CONDITIONS",
    "SHORT_POOL_DISCLAIMER",
    "SHORT_POOL_ORDER_BY",
    "SHORT_POOL_PRESET_ID",
    "SHORT_POOL_PRESET_NAME",
    "SHORT_POOL_PRESET_DESCRIPTION",
    "SHORT_POOL_PRESET_VERSION",
    "ShortPoolLimit",
    "build_query_request",
    "build_t_research_hypothesis",
    "run_short_pool",
    "T_RESEARCH_AI_ROLE",
    "T_RESEARCH_PROTOCOL",
    "T_RESEARCH_PROTOCOL_ID",
    "T_RESEARCH_RESERVED_TAGS",
]
