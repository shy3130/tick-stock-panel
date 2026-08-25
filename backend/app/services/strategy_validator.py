"""策略结构诊断 — 照搬 ymos-diagnosis 八项体检的可代码化部分。

纯函数; ledger 由调用方 (API 层) 从 ``trade_journal.store.read_ledger`` 读入后传入,
validator 本身不读写文件 / 不触达行情, 便于单测。

检查项 (P4 范围):
    - field_completeness   字段完整性 (委托 strategy_profile.validate_profile)
    - cadence_horizon_match 内部一致性 (复盘节奏 vs 论点周期)
    - horizon_drift        期限漂移 (台账实际持仓天数 vs 声明 horizon)

(隐藏共享敞口检查需要 ext_data 概念表, 不在 P4 切片内, 暂不实现。)
"""
from __future__ import annotations

from statistics import median
from typing import Any

from app.services.strategy_profile import FAMILY_MIX_KEYS, validate_profile

# 高频复盘节奏与长周期论点的错配阈值 (照搬 YMOS 节奏规则)
_HIGH_FREQ_REVIEWS = ("daily", "weekly")
_LONG_HORIZON_MONTHS = 12
# family → 复盘节奏倾向 (源自 strategy_family_map.md; 仅机械可定义的高/低频族)
# trend/short_horizon 高频, value/growth 低频; event/relative_value/mixed 节奏事件驱动, 不机械判定
_FAMILY_CADENCE: dict[str, str] = {
    "trend": "high",
    "short_horizon": "high",
    "value": "low",
    "growth": "low",
}
_HIGH_FREQ_MAX_MEDIAN_DAYS = 20  # 高频策略实际持仓中位数 > 20 → warn (言行不符)
_LOW_FREQ_MIN_MEDIAN_DAYS = 5    # 低频策略实际持仓中位数 < 5 → warn


def validate_strategy(
    strategy_id: str,
    profile: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """机械体检; 返回 ``{"checks": [{id, name, status, detail}]}``。

    status ∈ pass | partial | fail | insufficient_evidence。
    proposals 缺省 None 时 proposal_governance 检查跳过 (insufficient_evidence), 向后兼容。
    """
    checks = [
        _check_field_completeness(profile),
        _check_cadence_horizon_match(profile),
        _check_horizon_drift(profile, ledger),
        _check_playbook_declared(profile),
        _check_family_conflict(profile),
        _check_family_behavior_conflict(profile, ledger),
        _check_proposal_governance(strategy_id, proposals),
    ]
    return {"checks": checks}


# ── 检查项 ───────────────────────────────────────────────
def _check_field_completeness(profile: dict[str, Any] | None) -> dict[str, Any]:
    cid, name = "field_completeness", "字段完整性"
    if profile is None:
        return _check(cid, name, "fail", "策略尚未声明风险 profile (失效信号/风险预算/期限)")
    problems = validate_profile(profile)
    if not problems:
        return _check(cid, name, "pass", "失效信号三要素齐全, 风险数值边界合规")
    return _check(cid, name, "fail", "; ".join(problems))


def _check_cadence_horizon_match(profile: dict[str, Any] | None) -> dict[str, Any]:
    cid, name = "cadence_horizon_match", "复盘节奏与论点周期匹配"
    risk = profile.get("risk") if isinstance(profile, dict) else None
    cadence = profile.get("cadence") if isinstance(profile, dict) else None
    horizon = risk.get("thesisHorizonMonths") if isinstance(risk, dict) else None
    review = cadence.get("review") if isinstance(cadence, dict) else None
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not isinstance(review, str):
        return _check(
            cid, name, "insufficient_evidence",
            "缺 risk.thesisHorizonMonths 或 cadence.review, 无法判断节奏匹配",
        )
    review_norm = review.strip().lower()
    if review_norm in _HIGH_FREQ_REVIEWS and horizon > _LONG_HORIZON_MONTHS:
        return _check(
            cid, name, "partial",
            f"{review_norm} 复盘但论点周期 {horizon} 月 > {_LONG_HORIZON_MONTHS} 月: "
            "节奏错配 (复盘过频或论点周期过长)",
        )
    return _check(cid, name, "pass", f"复盘节奏 {review_norm} 与论点周期 {horizon} 月匹配")


def _check_horizon_drift(
    profile: dict[str, Any] | None, ledger: dict[str, Any] | None
) -> dict[str, Any]:
    cid, name = "horizon_drift", "期限漂移"
    risk = profile.get("risk") if isinstance(profile, dict) else None
    horizon = risk.get("thesisHorizonMonths") if isinstance(risk, dict) else None
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        return _check(cid, name, "insufficient_evidence", "缺合法的 risk.thesisHorizonMonths")

    holding_days = _extract_holding_days(ledger)
    if holding_days is None:
        return _check(cid, name, "insufficient_evidence", "台账为空, 无实际持仓天数证据")

    declared_days = horizon * 30
    med = float(median(holding_days))
    ratio = med / declared_days if declared_days else 0.0
    # 偏差 > 2 倍 (任一方向) → fail: 实际持仓与声明周期严重背离
    if ratio > 2.0 or ratio < 0.5:
        direction = "远长于" if ratio > 2.0 else "远短于"
        return _check(
            cid, name, "fail",
            f"实际持仓中位数 {med:.0f} 天 {direction}声明周期 {declared_days} 天 (偏差 > 2 倍)",
        )
    return _check(
        cid, name, "pass",
        f"实际持仓中位数 {med:.0f} 天, 声明周期 {declared_days} 天, 偏差可接受",
    )


# ── P6.2 新增检查项 ─────────────────────────────────────
def _check_playbook_declared(profile: dict[str, Any] | None) -> dict[str, Any]:
    cid, name = "playbook_declared", "策略剧本声明"
    if not isinstance(profile, dict):
        return _check(cid, name, "insufficient_evidence", "策略尚未声明 profile, 无法评估 playbook")
    playbook = profile.get("playbook")
    if not isinstance(playbook, dict):
        return _check(cid, name, "partial", "未声明 playbook (scope/entry/exit 三文本缺失)")
    missing = [
        k for k in ("scope", "entry", "exit")
        if not isinstance(playbook.get(k), str) or not playbook[k].strip()
    ]
    if missing:
        return _check(cid, name, "partial", f"playbook 文本缺失/为空: {', '.join(missing)}")
    return _check(cid, name, "pass", "playbook 三文本齐全 (scope/entry/exit)")


def _check_family_conflict(profile: dict[str, Any] | None) -> dict[str, Any]:
    cid, name = "family_conflict", "混合策略冲突裁决"
    if not isinstance(profile, dict):
        return _check(cid, name, "insufficient_evidence", "策略尚未声明 profile, 无法评估 family")
    family = profile.get("family")
    if family != "mixed":
        return _check(
            cid, name, "insufficient_evidence",
            "非混合策略或未声明 family, 无冲突裁决要素需检验",
        )
    mix = profile.get("familyMix")
    if not isinstance(mix, dict):
        return _check(
            cid, name, "fail",
            "family=mixed 但缺 familyMix 四要素 (入场裁判/失效权/仓位期限/冲突裁决)",
        )
    missing = [
        k for k in FAMILY_MIX_KEYS
        if not isinstance(mix.get(k), str) or not mix[k].strip()
    ]
    if missing:
        return _check(
            cid, name, "fail",
            f"family=mixed 但 familyMix 要素缺失/为空: {', '.join(missing)}",
        )
    return _check(cid, name, "pass", "family=mixed 且四要素裁决齐全")


def _check_family_behavior_conflict(
    profile: dict[str, Any] | None, ledger: dict[str, Any] | None
) -> dict[str, Any]:
    cid, name = "family_behavior_conflict", "自称 family 与实际持仓节奏冲突"
    if not isinstance(profile, dict):
        return _check(cid, name, "insufficient_evidence", "策略尚未声明 profile, 无法评估 family")
    family = profile.get("family")
    cadence = _FAMILY_CADENCE.get(family) if isinstance(family, str) else None
    if cadence is None:
        return _check(
            cid, name, "insufficient_evidence",
            f"family={family!r} 节奏倾向未机械定义, 跳过行为冲突判定",
        )
    holding_days = _extract_holding_days(ledger)
    if holding_days is None:
        return _check(cid, name, "insufficient_evidence", "台账为空, 无实际持仓天数证据")
    med = float(median(holding_days))
    if cadence == "high" and med > _HIGH_FREQ_MAX_MEDIAN_DAYS:
        return _check(
            cid, name, "partial",
            f"family={family} 为高频策略, 但实际持仓中位数 {med:.0f} 天 > "
            f"{_HIGH_FREQ_MAX_MEDIAN_DAYS} 天: 短周期交易被长期持有 (言行不符)",
        )
    if cadence == "low" and med < _LOW_FREQ_MIN_MEDIAN_DAYS:
        return _check(
            cid, name, "partial",
            f"family={family} 为低频策略, 但实际持仓中位数 {med:.0f} 天 < "
            f"{_LOW_FREQ_MIN_MEDIAN_DAYS} 天: 长周期论点被短频交易 (言行不符)",
        )
    return _check(
        cid, name, "pass",
        f"family={family} 节奏倾向与持仓中位数 {med:.0f} 天一致",
    )


def _check_proposal_governance(
    strategy_id: str, proposals: list[dict[str, Any]] | None
) -> dict[str, Any]:
    cid, name = "proposal_governance", "策略变更提案治理"
    if proposals is None:
        return _check(cid, name, "insufficient_evidence", "未传入提案数据, 跳过治理核查")
    related = _associated_proposals(strategy_id, proposals)
    if not related:
        return _check(cid, name, "insufficient_evidence", "无关联提案, 无治理核查项")
    incomplete: list[str] = []
    for prop in related:
        pid = prop.get("id", "?")
        falsifier = prop.get("falsifier")
        sample_size = prop.get("sampleSize")
        miss: list[str] = []
        if not isinstance(falsifier, str) or not falsifier.strip():
            miss.append("falsifier(反证条件)")
        if (
            isinstance(sample_size, bool)
            or not isinstance(sample_size, int | float)
            or sample_size <= 0
        ):
            miss.append("sampleSize(复核样本)")
        if miss:
            incomplete.append(f"{pid}: {', '.join(miss)}")
    if incomplete:
        return _check(
            cid, name, "partial",
            "关联提案治理字段缺失: " + "; ".join(incomplete),
        )
    return _check(cid, name, "pass", f"关联提案 {len(related)} 项, 反证条件与复核样本齐全")


# ── 工具 ─────────────────────────────────────────────────
def _extract_holding_days(ledger: dict[str, Any] | None) -> list[float] | None:
    """从 ledger 提取持仓天数样本; 无证据返回 None。

    优先用 trips/roundtrips 的 holding_days; 退化口径用 summary.avg_holding_days 单点。
    """
    if not isinstance(ledger, dict):
        return None
    trips = ledger.get("trips") or ledger.get("roundtrips")
    if isinstance(trips, list) and trips:
        days = [
            t.get("holding_days")
            for t in trips
            if isinstance(t, dict) and isinstance(t.get("holding_days"), int | float)
        ]
        days = [float(d) for d in days if d > 0]  # type: ignore[comparison-overlap]
        if days:
            return days
    summary = ledger.get("summary")
    if isinstance(summary, dict):
        avg = summary.get("avg_holding_days") or summary.get("holding_days")
        if isinstance(avg, int | float) and avg > 0:
            return [float(avg)]
    return None


def _associated_proposals(
    strategy_id: str, proposals: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """筛选与 strategy_id 关联的提案 (target 字段含策略标识)。"""
    if not proposals:
        return []
    out: list[dict[str, Any]] = []
    for prop in proposals:
        if not isinstance(prop, dict):
            continue
        target = prop.get("target")
        if isinstance(target, str) and strategy_id in target:
            out.append(prop)
    return out


def _check(check_id: str, name: str, status: str, detail: str) -> dict[str, Any]:
    return {"id": check_id, "name": name, "status": status, "detail": detail}
