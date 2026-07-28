"""Deterministic recommendation gate built from audited data and strategy output.

The module does not call an LLM, predict policy events, place orders, or invent
missing observations.  Its GO label means only that a candidate may enter the
user's research list.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

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
    datasets: dict[str, dict] = {}

    if not as_of:
        reasons.append("尚无策略结果日期")

    for dataset in _REQUIRED_DATASETS:
        label = _DATASET_LABELS[dataset]
        audit = by_dataset.get(dataset)
        dataset_reasons: list[str] = []
        if audit is None:
            dataset_reasons.append(f"缺少{label}可信度回执")
            datasets[dataset] = {
                "status": "missing",
                "provider": None,
                "coverage_ratio": 0.0,
                "observed_start": None,
                "observed_end": None,
                "reasons": dataset_reasons,
            }
            reasons.extend(dataset_reasons)
            continue

        coverage = float(audit.get("coverage_ratio") or 0.0)
        status = audit.get("status")
        if status in {"error", "invalid", "empty"}:
            dataset_reasons.append(f"{label}回执状态为 {status}")
        if dataset != "instruments":
            if audit.get("fallback_used"):
                dataset_reasons.append(f"{label}发生了未授权的数据源回退")
            if audit.get("synthetic"):
                dataset_reasons.append(f"{label}回执标记为伪造或合成数据")
            if coverage < _MIN_DAILY_COVERAGE:
                dataset_reasons.append(
                    f"{label}覆盖率仅 {coverage * 100:.1f}%, "
                    f"低于 {_MIN_DAILY_COVERAGE * 100:.1f}% 门槛"
                )
        observed_end = audit.get("observed_end")
        if dataset in {"daily", "daily_enriched"} and as_of and observed_end != as_of:
            dataset_reasons.append(
                f"{label}截止日 {observed_end or '未知'} 与策略日期 {as_of} 不一致"
            )
        datasets[dataset] = {
            "status": status,
            "provider": audit.get("provider"),
            "coverage_ratio": coverage,
            "observed_start": audit.get("observed_start"),
            "observed_end": observed_end,
            "reasons": dataset_reasons,
        }
        reasons.extend(dataset_reasons)

    daily = by_dataset.get("daily")

    return {
        "decision": "BLOCK" if reasons else "PASS",
        "provider": daily.get("provider") if daily else None,
        "coverage_ratio": float(daily.get("coverage_ratio") or 0.0) if daily else 0.0,
        "observed_end": daily.get("observed_end") if daily else None,
        "reasons": reasons,
        "datasets": datasets,
    }


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
