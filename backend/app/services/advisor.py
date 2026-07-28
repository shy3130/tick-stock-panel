"""Deterministic recommendation gate built from audited data and strategy output.

The module does not call an LLM, predict policy events, place orders, or invent
missing observations.  Its GO label means only that a candidate may enter the
user's research list.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.data_providers.trust import validate_audit_receipt

_MIN_DAILY_COVERAGE = 0.95
_CONSENSUS_BONUS = 8.0
_GO_SCORE = 75.0
_WAIT_SCORE = 60.0
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
    by_dataset = {
        str(audit.get("dataset")): audit
        for audit in audits
        if isinstance(audit, dict) and audit.get("dataset")
    }
    reasons: list[str] = []
    next_actions: list[str] = []
    datasets: dict[str, dict] = {}

    if not as_of:
        reasons.append("尚无策略结果日期")
        next_actions.append("请先运行策略生成最新结果, 再重新查看研究清单。")

    for dataset in _REQUIRED_DATASETS:
        label = _DATASET_LABELS[dataset]
        audit = by_dataset.get(dataset)
        dataset_reasons: list[str] = []
        dataset_actions: list[str] = []
        if audit is None:
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


def _risk_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    status = str(row.get("status") or "").lower()
    if status in {"limit_up", "one_word_limit_up"}:
        reasons.append("当前处于涨停或一字涨停状态, 不作为可追入候选")
    if status in {"limit_down", "one_word_limit_down"}:
        reasons.append("当前处于跌停状态")
    return reasons


def build_advisor_recommendations(
    audits: list[dict],
    strategy_cache: dict | None,
    *,
    limit: int = 30,
) -> dict:
    """Aggregate deterministic strategy scores behind explicit data/risk gates."""
    cache = strategy_cache if isinstance(strategy_cache, dict) else {}
    as_of = str(cache.get("as_of")) if cache.get("as_of") else None
    data_gate = _data_gate(audits, as_of)
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
        risk_reasons = _risk_reasons(representative)

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
