"""Deterministic recommendation gate built from audited data and strategy output.

The module does not call an LLM, predict policy events, place orders, or invent
missing observations.  Its GO label means only that a candidate may enter the
user's research list.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.data_providers.trust import validate_audit_receipt

_MIN_DAILY_COVERAGE = 0.95
_CONSENSUS_BONUS = 8.0
_GO_SCORE = 75.0
_WAIT_SCORE = 60.0
_RISK_FLAG_ORDER = (
    "ADJUSTMENT_EVENT_ON_AS_OF",
    "ABNORMAL_DAILY_RETURN",
    "INVALID_PRICE",
    "LIMIT_UP",
    "LIMIT_DOWN",
)
_REQUIRED_DATASETS = ("instruments", "daily", "adj_factor", "daily_enriched")
_DATASET_LABELS = {
    "instruments": "证券主表",
    "daily": "日K",
    "adj_factor": "除权因子",
    "daily_enriched": "派生日K",
}

_DECISION_LABELS = {
    "GO": "可进入研究清单",
    "WAIT": "等待更多确认",
    "NO-GO": "暂不纳入",
}


def _data_gate(audits: list[dict], as_of: str | None) -> dict:
    by_dataset: dict[str, list[dict]] = defaultdict(list)
    for audit in audits:
        if isinstance(audit, dict) and audit.get("dataset"):
            by_dataset[str(audit["dataset"])].append(audit)
    reasons: list[str] = []
    next_actions: list[str] = []
    datasets: dict[str, dict] = {}

    if not as_of:
        reasons.append("尚无策略结果日期")
        next_actions.append("请先运行策略生成最新结果, 再重新查看研究清单。")

    for dataset in _REQUIRED_DATASETS:
        label = _DATASET_LABELS[dataset]
        matching_audits = by_dataset.get(dataset, [])
        dataset_reasons: list[str] = []
        dataset_actions: list[str] = []
        if not matching_audits:
            dataset_reasons.append(f"缺少{label}可信度回执")
            dataset_actions.append(
                f"请重新运行{label}同步, 生成最新可信度回执后再试。"
            )
            datasets[dataset] = {
                "status": "missing",
                "provider": None,
                "coverage_ratio": 0.0,
                "observed_start": None,
                "observed_end": None,
                "reasons": dataset_reasons,
                "next_actions": dataset_actions,
            }
            reasons.extend(dataset_reasons)
            _extend_unique(next_actions, dataset_actions)
            continue
        if len(matching_audits) > 1:
            dataset_reasons.append(
                f"{label}收到 {len(matching_audits)} 份重复回执, 无法确定唯一可信版本"
            )
            dataset_actions.append(
                f"请清理重复的{label}回执并重新同步, "
                "确保只保留一份最新可信度回执。"
            )
            for duplicate in matching_audits:
                duplicate_errors = validate_audit_receipt(duplicate)
                _extend_unique(
                    dataset_reasons,
                    [
                        f"{label}回执字段异常: {error}"
                        for error in duplicate_errors
                    ],
                )
            if len(dataset_reasons) > 1:
                dataset_actions.append(
                    f"请检查{label}数据源配置并重新同步, "
                    "以重新生成有效可信度回执。"
                )
            datasets[dataset] = {
                "status": "duplicate",
                "provider": None,
                "coverage_ratio": 0.0,
                "observed_start": None,
                "observed_end": None,
                "reasons": dataset_reasons,
                "next_actions": dataset_actions,
            }
            reasons.extend(dataset_reasons)
            _extend_unique(next_actions, dataset_actions)
            continue

        audit = matching_audits[0]
        schema_errors = validate_audit_receipt(audit)
        if schema_errors:
            dataset_reasons.extend(
                f"{label}回执字段异常: {error}" for error in schema_errors
            )
            dataset_actions.append(
                f"请检查{label}数据源配置并重新同步, 以重新生成有效可信度回执。"
            )
        raw_coverage = audit.get("coverage_ratio")
        coverage_valid = not any(
            error.startswith("coverage_ratio ") for error in schema_errors
        )
        coverage = float(raw_coverage) if coverage_valid else 0.0
        status = audit.get("status")
        if status in {"error", "invalid", "empty"}:
            dataset_reasons.append(f"{label}回执状态为 {status}")
            dataset_actions.append(
                f"请重新运行{label}同步; 若仍失败, 请检查数据源配置与同步日志。"
            )
        if dataset != "instruments":
            if isinstance(audit.get("fallback_used"), bool) and audit.get("fallback_used"):
                dataset_reasons.append(f"{label}发生了未授权的数据源回退")
                dataset_actions.append(
                    f"请检查{label}数据源配置, 关闭未授权回退后重新同步。"
                )
            if isinstance(audit.get("synthetic"), bool) and audit.get("synthetic"):
                dataset_reasons.append(f"{label}回执标记为伪造或合成数据")
                dataset_actions.append(
                    f"请切换到真实授权数据源并重新同步{label}。"
                )
            if coverage_valid and coverage < _MIN_DAILY_COVERAGE:
                dataset_reasons.append(
                    f"{label}覆盖率仅 {coverage * 100:.1f}%, "
                    f"低于 {_MIN_DAILY_COVERAGE * 100:.1f}% 门槛"
                )
                dataset_actions.append(
                    f"请补齐缺失标的后重新运行{label}同步, "
                    f"使覆盖率达到{_MIN_DAILY_COVERAGE * 100:.1f}%以上。"
                )
        observed_end = audit.get("observed_end")
        if dataset in {"daily", "daily_enriched"} and as_of and observed_end != as_of:
            dataset_reasons.append(
                f"{label}截止日 {observed_end or '未知'} 与策略日期 {as_of} 不一致"
            )
            dataset_actions.append(
                f"请将{label}同步到策略日期 {as_of}, 再重新生成策略结果。"
            )
        dataset_actions = list(dict.fromkeys(dataset_actions))
        datasets[dataset] = {
            "status": status,
            "provider": audit.get("provider"),
            "coverage_ratio": coverage,
            "observed_start": audit.get("observed_start"),
            "observed_end": observed_end,
            "reasons": dataset_reasons,
            "next_actions": dataset_actions,
        }
        reasons.extend(dataset_reasons)
        _extend_unique(next_actions, dataset_actions)

    daily = datasets["daily"]

    return {
        "decision": "BLOCK" if reasons else "PASS",
        "provider": daily["provider"],
        "coverage_ratio": daily["coverage_ratio"],
        "observed_end": daily["observed_end"],
        "reasons": reasons,
        "next_actions": next_actions,
        "datasets": datasets,
    }


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _risk_flags(
    row: dict[str, Any],
    *,
    adjustment_event_on_as_of: bool,
) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    if adjustment_event_on_as_of:
        flags.append(
            {
                "code": "ADJUSTMENT_EVENT_ON_AS_OF",
                "message": "策略日期发生除权除息事件, 已隔离并等待人工复核",
            }
        )

    try:
        change_pct = float(row.get("change_pct"))
    except (TypeError, ValueError, OverflowError):
        change_pct = None
    if change_pct is not None and math.isfinite(change_pct) and abs(change_pct) > 0.30:
        flags.append(
            {
                "code": "ABNORMAL_DAILY_RETURN",
                "message": "当日涨跌幅绝对值超过 30%, 已隔离并等待人工复核",
            }
        )

    try:
        close = float(row.get("close"))
    except (TypeError, ValueError, OverflowError):
        close = None
    if close is None or not math.isfinite(close) or close <= 0:
        flags.append(
            {
                "code": "INVALID_PRICE",
                "message": "收盘价缺失、非有限数或不大于 0, 已隔离并等待人工复核",
            }
        )

    status = str(row.get("status") or "").lower()
    if status in {"limit_up", "one_word_limit_up"}:
        flags.append(
            {
                "code": "LIMIT_UP",
                "message": "当前处于涨停或一字涨停状态, 不作为可追入候选",
            }
        )
    if status in {"limit_down", "one_word_limit_down"}:
        flags.append({"code": "LIMIT_DOWN", "message": "当前处于跌停状态"})
    return flags


def build_advisor_recommendations(
    audits: list[dict],
    strategy_cache: dict | None,
    *,
    limit: int = 30,
    adjustment_event_symbols: set[str] | None = None,
    adjustment_factor_problem: dict[str, str] | None = None,
) -> dict:
    """Aggregate deterministic strategy scores behind explicit data/risk gates."""
    cache = strategy_cache if isinstance(strategy_cache, dict) else {}
    as_of = str(cache.get("as_of")) if cache.get("as_of") else None
    data_gate = _data_gate(audits, as_of)
    data_gate["runtime_problems"] = []
    if adjustment_factor_problem is not None:
        problem = {
            "code": adjustment_factor_problem["code"],
            "reason": adjustment_factor_problem["reason"],
            "next_action": adjustment_factor_problem["next_action"],
        }
        data_gate["runtime_problems"].append(problem)
        data_gate["decision"] = "BLOCK"
        _extend_unique(data_gate["reasons"], [problem["reason"]])
        _extend_unique(data_gate["next_actions"], [problem["next_action"]])
        _extend_unique(
            data_gate["datasets"]["adj_factor"]["reasons"],
            [problem["reason"]],
        )
        _extend_unique(
            data_gate["datasets"]["adj_factor"]["next_actions"],
            [problem["next_action"]],
        )
    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    for strategy_id, result in (cache.get("results") or {}).items():
        if not isinstance(result, dict) or str(result.get("as_of") or "") != str(as_of or ""):
            continue
        for row in result.get("rows") or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            grouped[str(row["symbol"])].append((str(strategy_id), row))

    candidates: list[dict] = []
    for symbol, matches in grouped.items():
        strategy_scores = {
            strategy_id: float(row.get("score") or 0.0)
            for strategy_id, row in matches
        }
        scores = list(strategy_scores.values())
        highest = max(scores) if scores else 0.0
        average = sum(scores) / len(scores) if scores else 0.0
        consensus_bonus = min(24.0, max(0, len(matches) - 1) * _CONSENSUS_BONUS)
        score = round(min(100.0, highest * 0.7 + average * 0.3 + consensus_bonus), 1)
        representative = max(matches, key=lambda item: float(item[1].get("score") or 0.0))[1]
        flags_by_code: dict[str, dict[str, str]] = {}
        for _, row in matches:
            for flag in _risk_flags(
                row,
                adjustment_event_on_as_of=symbol in (adjustment_event_symbols or set()),
            ):
                flags_by_code[flag["code"]] = flag
        risk_flags = [
            flags_by_code[code]
            for code in _RISK_FLAG_ORDER
            if code in flags_by_code
        ]
        risk_reasons = [flag["message"] for flag in risk_flags]

        if data_gate["decision"] == "BLOCK" or risk_reasons:
            decision = "NO-GO"
        elif len(matches) >= 2 and score >= _GO_SCORE:
            decision = "GO"
        elif score >= _WAIT_SCORE:
            decision = "WAIT"
        else:
            decision = "NO-GO"

        candidates.append(
            {
                "symbol": symbol,
                "name": representative.get("name") or symbol,
                "as_of": as_of,
                "close": representative.get("close"),
                "change_pct": representative.get("change_pct"),
                "decision": decision,
                "decision_label": _DECISION_LABELS[decision],
                "score": score,
                "score_method": "0.7x最高策略分 + 0.3x平均策略分 + 共识加分",
                "strategy_count": len(matches),
                "strategies": sorted(strategy_scores),
                "strategy_scores": dict(sorted(strategy_scores.items())),
                "risk_flags": risk_flags,
                "risk_reasons": risk_reasons,
                "ai_generated": False,
            }
        )

    candidates.sort(
        key=lambda item: (
            {"GO": 2, "WAIT": 1, "NO-GO": 0}[item["decision"]],
            item["score"],
            item["strategy_count"],
            item["symbol"],
        ),
        reverse=True,
    )
    return {
        "as_of": as_of,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_gate": data_gate,
        "method": {
            "kind": "deterministic",
            "policy_factors_included": False,
            "ai_can_change_score": False,
            "auto_trading": False,
        },
        "candidates": candidates[: max(0, limit)],
        "disclaimer": "仅供个人研究; GO 仅表示进入研究清单, 不等于买入指令或收益承诺。",
    }


def build_beginner_daily_brief(recommendations: dict | None) -> dict:
    """Turn an existing deterministic recommendation result into a beginner brief."""
    report = recommendations if isinstance(recommendations, dict) else {}
    data_gate = report.get("data_gate") if isinstance(report.get("data_gate"), dict) else {}
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    research_candidates = [
        _beginner_candidate(candidate)
        for candidate in candidates[:3]
        if isinstance(candidate, dict)
    ]

    if data_gate.get("decision") != "PASS":
        action_state = "OBSERVE_ONLY"
        today_message = "数据检查尚未通过, 今天只观察, 不进行模拟或研究筛选。"
        next_step = "先处理数据检查中的问题, 再重新生成日报。"
    elif any(candidate.get("research_decision") == "GO" for candidate in research_candidates):
        action_state = "RESEARCH_ONLY"
        today_message = "数据检查已通过, 存在通过硬风险检查的研究候选; 今天只做规则化研究。"
        next_step = "逐项核对候选的规则条件和风险标记, 再决定是否继续跟踪。"
    else:
        action_state = "SIMULATE_ONLY"
        today_message = "数据检查已通过, 但没有通过硬风险检查的研究候选; 今天只做模拟复盘。"
        next_step = "用历史或模拟环境复盘候选, 等待下一次规则复核。"

    return {
        "as_of": report.get("as_of"),
        "generated_at": report.get("generated_at"),
        "action_state": action_state,
        "today_message": today_message,
        "next_step": next_step,
        "data_gate": data_gate,
        "method": report.get("method") if isinstance(report.get("method"), dict) else {},
        "candidates": research_candidates,
        "disclaimer": report.get("disclaimer"),
    }


def _beginner_candidate(candidate: dict) -> dict:
    decision = str(candidate.get("decision") or "NO-GO")
    decision_label = candidate.get("decision_label")
    if decision_label not in _DECISION_LABELS.values():
        decision_label = _DECISION_LABELS.get(decision, "结论待复核")
    strategies = [str(strategy) for strategy in candidate.get("strategies") or []]
    reasons = [f"研究判断: {decision_label}"]
    if strategies:
        reasons.append(f"策略共识: {len(strategies)}条独立策略给出了同向结果")

    return {
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name"),
        "research_decision": decision,
        "deterministic_reasons": reasons,
        "observation_conditions": [
            "下次复核后, 数据检查仍然合格",
            f"下次复核后, 研究结论仍为{decision_label}",
        ],
        "invalidation_conditions": [
            "任一必需数据回执异常或运行时校验失败",
            f"下次复核后, 研究结论不再是{decision_label}",
            "下次复核后出现任一风险标记",
        ],
        "risk_flags": candidate.get("risk_flags")
        if isinstance(candidate.get("risk_flags"), list)
        else [],
    }
