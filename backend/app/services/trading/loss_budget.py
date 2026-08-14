"""损失预算约束 — 按维度累计已实现损失并裁定预算剩余。

来源: 移植自 YMOS 风险预算不变量 (strategy-profile.schema.json 的 ``risk.lossBudgetPct``
与 ymos-diagnosis 的 *Risk-bearing clarity* / *Rule relaxation after pain*)。

定位与红线 (与 risk_models.py 一致的单向收紧语义):
- 仅供风险门禁: 只读传入的已实现损失记录, 只输出预算裁定, 不调用任何外部数据源;
- 不生成订单、方向或执行动作; 不修改现有账户/交易 API 契约;
- 预算裁定单向收紧: 超预算 → deny; 数据不完整 → insufficient_data (fail-closed);
  亏损后的放宽请求 → 一律 deny (YMOS inconsistency_patterns #12);
- 非有限 realizedPnl 无法核定损失 → 该维度 insufficient_data, 绝不伪造数值;
- 盈亏在 *同一作用域内* 按净额抵扣 (标准日亏限额语义), 但净盈利不增加预算额度
  (预算额度只能收紧, 不能因盈利而放宽)。

输入约定: 一条已实现损失记录为 dict, 至少包含::

    {
        "date": "2026-08-14",      # 损益实现日 (ISO; 日/周维度归因用)
        "realizedPnl": -500.0,     # 已实现盈亏, 负=亏损 (非有限值触发 insufficient)
        "strategy": "general",     # 策略族 (可选; 策略维度过滤用)
        "accountId": "default",    # 账户 (可选; 默认 "default")
        "tradeId": "...",          # provenance (可选, 原样回显)
        "symbol": "...",           # provenance (可选, 原样回显)
    }

预算配置按维度给出绝对金额 (调用方负责把 ``lossBudgetPct * 资金基数`` 换算成绝对值,
本模块保持纯函数不触碰资金基数)::

    {"daily": 1000.0, "weekly": 3000.0, "strategy": 5000.0, "portfolio": 10000.0}

某维度值为 ``None`` 表示未配置 → 该维度不参与评估 (不强制)。零预算 (``0.0``) 合法,
含义为「该维度不容忍任何已实现亏损」, 任何 amount > 0 即 deny。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Sequence

SCHEMA_VERSION = 1

# ── 维度 ─────────────────────────────────────────────────
DIMENSION_DAILY = "daily"
DIMENSION_WEEKLY = "weekly"
DIMENSION_STRATEGY = "strategy"
DIMENSION_PORTFOLIO = "portfolio"
DIMENSIONS: tuple[str, ...] = (
    DIMENSION_DAILY,
    DIMENSION_WEEKLY,
    DIMENSION_STRATEGY,
    DIMENSION_PORTFOLIO,
)

# ── 裁定 ─────────────────────────────────────────────────
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"
_BLOCKING: tuple[str, ...] = (VERDICT_DENY, VERDICT_INSUFFICIENT_DATA)
_VERDICT_RANK: dict[str, int] = {
    VERDICT_DENY: 0,
    VERDICT_INSUFFICIENT_DATA: 1,
    VERDICT_ALLOW: 2,
}

_PROVENANCE_SOURCE = "realized_loss_records"


# ── 内部工具 ─────────────────────────────────────────────
def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _finite_or_none(value: Any) -> float | None:
    """有限 float → float; None / 非数值 / 非有限 → None。"""
    if _is_finite(value):
        return float(value)
    return None


def _round2(x: float) -> float:
    return round(float(x), 2)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _record_date(record: dict[str, Any]) -> date | None:
    return _parse_date(record.get("date"))


def _record_pnl(record: dict[str, Any]) -> float | None:
    return _finite_or_none(record.get("realizedPnl"))


def _account_id(record: dict[str, Any]) -> str:
    return str(record.get("accountId") or "default")


def _account_matches(record: dict[str, Any], account_id: str | None) -> bool:
    if not account_id:
        return True
    return _account_id(record) == account_id


def _iso_week_bounds(anchor: date) -> tuple[date, date]:
    """ISO 周一~周日 (含) 起止。Monday.weekday() == 0。"""
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _net_pnl(records: Sequence[dict[str, Any]]) -> tuple[float, int, int]:
    """净已实现盈亏。

    返回 (净盈亏, 非有限记录数, 有效记录数)。非有限 realizedPnl 既不计入净额,
    也标记 all_finite=False (由调用方据此判定 insufficient)。
    """
    total = 0.0
    non_finite = 0
    valid = 0
    for r in records:
        pnl = _record_pnl(r)
        if pnl is None:
            non_finite += 1
            continue
        total += pnl
        valid += 1
    return total, non_finite, valid


def _scope_records(
    records: Sequence[dict[str, Any]],
    dimension: str,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """按维度过滤记录, 返回 (作用域记录, scope 描述)。返回 None 表示缺少必要上下文。

    account 过滤对所有维度生效; date 仅用于日/周维度; strategy 仅用于策略维度。
    """
    account_id = context.get("accountId") or None
    anchor = _parse_date(context.get("date"))
    strategy = str(context.get("strategy") or "").strip() or None

    scope_desc: dict[str, Any] = {"accountId": account_id or "all"}

    if dimension == DIMENSION_PORTFOLIO:
        scoped = [r for r in records if _account_matches(r, account_id)]
        scope_desc["range"] = "all records (cumulative)"
        return scoped, scope_desc

    if dimension == DIMENSION_STRATEGY:
        if strategy is None:
            return None, {**scope_desc, "reason": "missing strategy"}
        scoped = [
            r
            for r in records
            if _account_matches(r, account_id)
            and str(r.get("strategy") or "").strip() == strategy
        ]
        scope_desc["strategy"] = strategy
        scope_desc["range"] = "all records for strategy (cumulative)"
        return scoped, scope_desc

    # 日 / 周维度都需要锚点日期
    if anchor is None:
        return None, {**scope_desc, "reason": "missing date anchor"}

    if dimension == DIMENSION_DAILY:
        scoped = [
            r
            for r in records
            if _account_matches(r, account_id) and _record_date(r) == anchor
        ]
        scope_desc["period"] = {"start": anchor.isoformat(), "end": anchor.isoformat()}
        scope_desc["range"] = f"date == {anchor.isoformat()}"
        return scoped, scope_desc

    # weekly
    monday, sunday = _iso_week_bounds(anchor)
    scoped = [
        r
        for r in records
        if _account_matches(r, account_id)
        and (_record_date(r) is not None)
        and monday <= _record_date(r) <= sunday  # type: ignore[operator]
    ]
    scope_desc["period"] = {"start": monday.isoformat(), "end": sunday.isoformat()}
    scope_desc["range"] = f"{monday.isoformat()} .. {sunday.isoformat()}"
    return scoped, scope_desc


def _evidence(
    dimension: str,
    scope_desc: dict[str, Any],
    scoped: Sequence[dict[str, Any]],
    net_pnl_value: float,
    non_finite: int,
) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "scope": scope_desc.get("range"),
        "period": scope_desc.get("period"),
        "strategy": scope_desc.get("strategy"),
        "accountId": scope_desc.get("accountId", "all"),
        "recordCount": len(scoped),
        "validRecordCount": len(scoped) - non_finite,
        "nonFiniteCount": non_finite,
        "netRealizedPnl": _round2(net_pnl_value),
        "source": _PROVENANCE_SOURCE,
    }


def _insufficient_result(
    dimension: str,
    budget: Any,
    scope_desc: dict[str, Any],
    scoped: Sequence[dict[str, Any]] | None,
    net_pnl_value: float,
    non_finite: int,
    reason: str,
) -> dict[str, Any]:
    scoped = scoped or []
    return {
        "dimension": dimension,
        "amount": None,
        "budget": _finite_or_none(budget),
        "remaining": None,
        "utilization": None,
        "verdict": VERDICT_INSUFFICIENT_DATA,
        "reason": reason,
        "evidence": _evidence(dimension, scope_desc, scoped, net_pnl_value, non_finite),
    }


def _evaluate_one(
    records: Sequence[dict[str, Any]],
    budget: Any,
    dimension: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    budget_f = _finite_or_none(budget)
    scoped, scope_desc = _scope_records(records, dimension, context)

    # 无效预算配置 → fail-closed
    if budget_f is None or budget_f < 0:
        net, nf, _ = _net_pnl(scoped or [])
        return _insufficient_result(
            dimension, budget, scope_desc, scoped, net, nf,
            reason=f"{dimension} 预算非有限或为负 ({budget!r}),无法核定",
        )

    # 缺少必要上下文 (date/strategy) → fail-closed
    if scoped is None:
        net, nf, _ = _net_pnl([])
        return _insufficient_result(
            dimension, budget_f, scope_desc, None, net, nf,
            reason=f"{dimension} 维度缺少必要上下文 ({scope_desc.get('reason', 'date/strategy')})",
        )

    net, non_finite, _valid = _net_pnl(scoped)

    # 作用域内存在非有限 realizedPnl → 无法核定损失 → fail-closed
    if non_finite > 0:
        return _insufficient_result(
            dimension, budget_f, scope_desc, scoped, net, non_finite,
            reason=f"{dimension} 作用域内存在 {non_finite} 条非有限 realizedPnl,损失无法核定",
        )

    # 净已实现损失: 净亏取绝对值, 净盈不计入 (不增加额度)
    amount = _round2(max(0.0, -net))
    remaining = _round2(budget_f - amount)
    utilization = (amount / budget_f) if budget_f > 0 else None

    # 刚好达到预算即视为耗尽 (fail-closed 边界): amount >= budget → deny
    breached = amount >= budget_f and budget_f >= 0
    if budget_f == 0.0:
        breached = amount > 0.0  # 零预算: 任何损失即超限; 无损失则 allow
    verdict = VERDICT_DENY if breached else VERDICT_ALLOW

    if breached:
        reason = (
            f"{dimension} 已实现损失 {amount:.2f} ≥ 预算 {budget_f:.2f},预算耗尽"
            if budget_f > 0
            else f"{dimension} 已实现损失 {amount:.2f} > 零预算 0.00"
        )
    else:
        headroom = "不容忍任何亏损" if budget_f == 0.0 else f"剩余 {remaining:.2f}"
        reason = f"{dimension} 已实现损失 {amount:.2f} < 预算 {budget_f:.2f},{headroom}"

    return {
        "dimension": dimension,
        "amount": amount,
        "budget": budget_f,
        "remaining": remaining,
        "utilization": utilization,
        "verdict": verdict,
        "reason": reason,
        "evidence": _evidence(dimension, scope_desc, scoped, net, non_finite),
    }


def _select_binding(dim_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """挑选最受限维度 (deny > insufficient > allow; 同级取利用率最高)。"""
    def key(res: dict[str, Any]) -> tuple[int, float]:
        u = res.get("utilization")
        u_sort = float("inf") if u is None else float(u)
        return (_VERDICT_RANK[res["verdict"]], -u_sort)

    return min(dim_results.values(), key=key)


def _overall_reason(overall: str, binding: dict[str, Any]) -> str:
    if overall == VERDICT_ALLOW:
        return f"全部已配置维度在预算内 (binding={binding['dimension']})"
    if overall == VERDICT_INSUFFICIENT_DATA:
        return f"存在无法核定损失的维度 (binding={binding['dimension']}): {binding['reason']}"
    return f"存在超预算维度 (binding={binding['dimension']}): {binding['reason']}"


# ── 公共 API ─────────────────────────────────────────────
def realized_loss_of(record: dict[str, Any]) -> float | None:
    """单条记录的已实现损失额 (>=0)。

    realizedPnl < 0 → 取绝对值; >= 0 → 0; 非有限 → None。
    """
    pnl = _record_pnl(record)
    if pnl is None:
        return None
    return max(0.0, -pnl)


def evaluate_dimension(
    records: Sequence[dict[str, Any]],
    budget: Any,
    dimension: str,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估单个维度的损失预算。纯函数, 不落盘。

    返回 ``{amount, budget, remaining, utilization, verdict, reason, evidence}``。
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"未知维度 {dimension!r}, 可选 {DIMENSIONS}")
    return _evaluate_one(list(records or []), budget, dimension, context or {})


def evaluate_loss_budget(
    records: Sequence[dict[str, Any]],
    budgets: dict[str, Any] | None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估全部已配置维度的损失预算, 返回汇总裁定。纯函数, 不落盘。

    - 仅评估 ``budgets`` 中非 None 的维度;
    - 任一维度 deny → 汇总 deny; 否则任一 insufficient → insufficient; 否则 allow;
    - 未配置任何维度预算 → insufficient_data (无法核定合规, fail-closed);
    - 顶层 amount/budget/remaining/utilization 取自最受限 (binding) 维度。
    """
    ctx = context or {}
    recs = list(records or [])
    budgets = budgets or {}

    dim_results: dict[str, dict[str, Any]] = {}
    for dim in DIMENSIONS:
        b = budgets.get(dim)
        if b is None:
            continue
        dim_results[dim] = _evaluate_one(recs, b, dim, ctx)

    if not dim_results:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "verdict": VERDICT_INSUFFICIENT_DATA,
            "reason": "未配置任何维度的损失预算,无法核定合规",
            "amount": None,
            "budget": None,
            "remaining": None,
            "utilization": None,
            "bindingDimension": None,
            "dimensions": {},
        }

    verdicts = [r["verdict"] for r in dim_results.values()]
    if VERDICT_DENY in verdicts:
        overall = VERDICT_DENY
    elif VERDICT_INSUFFICIENT_DATA in verdicts:
        overall = VERDICT_INSUFFICIENT_DATA
    else:
        overall = VERDICT_ALLOW

    binding = _select_binding(dim_results)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "verdict": overall,
        "reason": _overall_reason(overall, binding),
        "amount": binding["amount"],
        "budget": binding["budget"],
        "remaining": binding["remaining"],
        "utilization": binding["utilization"],
        "bindingDimension": binding["dimension"],
        "dimensions": dim_results,
    }


def check_budget_relaxation(
    current_budget: Any,
    requested_budget: Any,
    *,
    realized_loss_total: Any = 0.0,
) -> dict[str, Any]:
    """评估预算变更请求 — 单向收紧 (YMOS inconsistency_patterns #12)。

    一旦该维度已有已实现亏损 (``realized_loss_total > 0``), 任何放宽
    (``requested > current``) 的请求一律 deny。收紧 (``requested <= current``) 或
    无亏损时的变更不在此不变量覆盖范围, 返回 allow。

    非有限输入 → insufficient_data (无法核定变更方向)。
    """
    cur = _finite_or_none(current_budget)
    req = _finite_or_none(requested_budget)
    loss = _finite_or_none(realized_loss_total)

    if cur is None or req is None or loss is None:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "verdict": VERDICT_INSUFFICIENT_DATA,
            "reason": "预算或已实现损失含非有限值,无法核定变更方向",
            "currentBudget": cur,
            "requestedBudget": req,
            "realizedLossTotal": loss,
            "isRelaxation": None,
        }

    is_relaxation = req > cur
    if is_relaxation and loss > 0.0:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "verdict": VERDICT_DENY,
            "reason": (
                f"已实现亏损 {loss:.2f} > 0,禁止放宽预算 "
                f"({cur:.2f} → {req:.2f}); 仅允许收紧 (YMOS 单向收紧不变量)"
            ),
            "currentBudget": _round2(cur),
            "requestedBudget": _round2(req),
            "realizedLossTotal": _round2(loss),
            "isRelaxation": True,
        }

    direction = "放宽" if is_relaxation else ("持平" if req == cur else "收紧")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "verdict": VERDICT_ALLOW,
        "reason": f"预算{direction} ({cur:.2f} → {req:.2f}),不触发亏损后放宽禁令",
        "currentBudget": _round2(cur),
        "requestedBudget": _round2(req),
        "realizedLossTotal": _round2(loss),
        "isRelaxation": is_relaxation,
    }
