"""Deterministic recommendation gate built from audited data and strategy output.

The module does not call an LLM, predict policy events, place orders, or invent
missing observations.  Its GO label means only that a candidate may enter the
user's research list.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from app.data_providers.trust import validate_audit_receipt

_MIN_DAILY_COVERAGE = 0.95
_CONSENSUS_BONUS = 8.0
_GO_SCORE = 75.0
_WAIT_SCORE = 60.0
_BEGINNER_MARKET_SCORE = 45.0
_BEGINNER_LOT_SIZE = 100
_BEGINNER_POSITION_RATIO = 0.30
_MODEL_HEALTH_WINDOW = 10
_RISK_FLAG_ORDER = (
    "ADJUSTMENT_EVENT_ON_AS_OF",
    "ABNORMAL_DAILY_RETURN",
    "INVALID_PRICE",
    "INVALID_STRATEGY_SCORE",
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


def evaluate_data_gate(audits: list[dict], as_of: str | None) -> dict:
    """Evaluate one persisted audit set against a strategy result date."""
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
            if observed_end and str(observed_end) > str(as_of):
                dataset_reasons.append(
                    f"策略结果仍为 {as_of}, {label}已更新至 {observed_end}"
                )
                dataset_actions.append(
                    "请重新运行盘后刷新, 并等待策略重算校验通过。"
                )
            else:
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


def _validated_strategy_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(score) or not 0.0 <= score <= 100.0:
        return None
    return score


def _optional_finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


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
    if row.get("signal_limit_up") is True or status in {
        "limit_up",
        "one_word_limit_up",
    }:
        flags.append(
            {
                "code": "LIMIT_UP",
                "message": "当前处于涨停或一字涨停状态, 不作为可追入候选",
            }
        )
    if row.get("signal_limit_down") is True or status in {
        "limit_down",
        "one_word_limit_down",
    }:
        flags.append({"code": "LIMIT_DOWN", "message": "当前处于跌停状态"})
    return flags


def build_advisor_recommendations(
    audits: list[dict],
    strategy_cache: dict | None,
    *,
    limit: int = 30,
    adjustment_event_symbols: set[str] | None = None,
    adjustment_factor_problem: dict[str, str] | None = None,
    research_snapshot_problem: dict[str, str] | None = None,
) -> dict:
    """Aggregate deterministic strategy scores behind explicit data/risk gates."""
    cache = strategy_cache if isinstance(strategy_cache, dict) else {}
    as_of = str(cache.get("as_of")) if cache.get("as_of") else None
    data_gate = evaluate_data_gate(audits, as_of)
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
    if research_snapshot_problem is not None:
        problem = {
            "code": research_snapshot_problem["code"],
            "reason": research_snapshot_problem["reason"],
            "next_action": research_snapshot_problem["next_action"],
        }
        data_gate["runtime_problems"].append(problem)
        data_gate["decision"] = "BLOCK"
        _extend_unique(data_gate["reasons"], [problem["reason"]])
        _extend_unique(data_gate["next_actions"], [problem["next_action"]])
    missing_market_symbols = {
        str(symbol)
        for audit in audits
        if data_gate["decision"] == "PASS"
        and isinstance(audit, dict)
        and audit.get("dataset") in {"daily", "daily_enriched"}
        and audit.get("status") == "partial"
        and isinstance(audit.get("missing_symbols"), list)
        for symbol in audit["missing_symbols"]
    }
    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    for strategy_id, result in (cache.get("results") or {}).items():
        if not isinstance(result, dict) or str(result.get("as_of") or "") != str(as_of or ""):
            continue
        for row in result.get("rows") or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            symbol = str(row["symbol"])
            if symbol in missing_market_symbols:
                continue
            grouped[symbol].append((str(strategy_id), row))

    candidates: list[dict] = []
    for symbol, matches in grouped.items():
        strategy_scores: dict[str, float | None] = {}
        valid_matches: list[tuple[str, dict, float]] = []
        invalid_score = False
        for strategy_id, row in matches:
            parsed_score = _validated_strategy_score(row.get("score"))
            strategy_scores[strategy_id] = parsed_score
            if parsed_score is None:
                invalid_score = True
            else:
                valid_matches.append((strategy_id, row, parsed_score))

        scores = [parsed_score for _, _, parsed_score in valid_matches]
        highest = max(scores) if scores else 0.0
        average = sum(scores) / len(scores) if scores else 0.0
        consensus_bonus = min(
            24.0,
            max(0, len(valid_matches) - 1) * _CONSENSUS_BONUS,
        )
        score = round(min(100.0, highest * 0.7 + average * 0.3 + consensus_bonus), 1)
        representative = (
            max(valid_matches, key=lambda item: item[2])[1]
            if valid_matches
            else matches[0][1]
        )
        flags_by_code: dict[str, dict[str, str]] = {}
        for _, row in matches:
            for flag in _risk_flags(
                row,
                adjustment_event_on_as_of=symbol in (adjustment_event_symbols or set()),
            ):
                flags_by_code[flag["code"]] = flag
        if invalid_score:
            flags_by_code["INVALID_STRATEGY_SCORE"] = {
                "code": "INVALID_STRATEGY_SCORE",
                "message": "策略分数缺失、非有限数或超出 0 到 100, 已隔离并等待重新生成",
            }
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


def _is_beginner_main_board(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if normalized.endswith(".SH"):
        return normalized[:6].startswith(("600", "601", "603", "605"))
    if normalized.endswith(".SZ"):
        return normalized[:6].startswith(("000", "001", "002", "003"))
    return False


def _is_st_name(name: Any) -> bool:
    text = str(name or "").strip()
    return bool(re.match(r"^(?:S\*ST|\*ST|ST)(?=[\u3400-\u9fff])", text, re.IGNORECASE))


def _beginner_eligibility_reason(candidate: dict, practice_capital: float) -> str | None:
    if not _is_beginner_main_board(str(candidate.get("symbol") or "")):
        return "not_main_board"
    if _is_st_name(candidate.get("name")):
        return "st_or_risk_warning"
    if candidate.get("risk_flags"):
        return "hard_risk"
    close = _optional_finite_float(candidate.get("close"))
    if close is None or close <= 0:
        return "hard_risk"
    if close * _BEGINNER_LOT_SIZE > practice_capital * _BEGINNER_POSITION_RATIO:
        return "over_practice_budget"
    if str(candidate.get("decision") or "") != "GO":
        return "not_go"
    return None


def build_beginner_candidate_progress(
    candidates: list[dict],
    *,
    as_of: str | None,
    history: list[dict] | None,
    trading_dates: list[str] | None,
    practice_capital: float = 10_000,
    limit: int = 3,
) -> dict:
    """Select beginner candidates before truncation and derive trading-day continuity.

    Only immutable, published snapshot history should be supplied by the API layer.
    A missing snapshot on the immediately preceding trading day resets the streak.
    """
    try:
        capital = float(practice_capital)
    except (TypeError, ValueError, OverflowError):
        capital = 10_000.0
    if not math.isfinite(capital) or capital <= 0:
        capital = 10_000.0

    current_as_of = str(as_of or "").strip()
    ordered_dates = sorted(
        {
            str(value).strip()
            for value in (trading_dates or [])
            if str(value).strip() and str(value).strip() <= current_as_of
        }
    )
    if current_as_of and current_as_of not in ordered_dates:
        ordered_dates.append(current_as_of)
        ordered_dates.sort()

    records_by_date: dict[str, list[dict]] = {}
    for record in history or []:
        if not isinstance(record, dict):
            continue
        record_as_of = str(record.get("as_of") or "").strip()
        record_candidates = record.get("candidates")
        if record_as_of and isinstance(record_candidates, list):
            records_by_date[record_as_of] = [
                value for value in record_candidates if isinstance(value, dict)
            ]
    records_by_date[current_as_of] = [
        value for value in candidates if isinstance(value, dict)
    ]

    def candidate_for(record_as_of: str, symbol: str) -> dict | None:
        for value in records_by_date.get(record_as_of, []):
            if str(value.get("symbol") or "") == symbol:
                return value
        return None

    excluded_counts = {
        "not_main_board": 0,
        "st_or_risk_warning": 0,
        "hard_risk": 0,
        "over_practice_budget": 0,
    }
    eligible: list[dict] = []
    previous_as_of = None
    if current_as_of in ordered_dates:
        current_index = ordered_dates.index(current_as_of)
        if current_index > 0:
            previous_as_of = ordered_dates[current_index - 1]

    for global_rank, candidate in enumerate(records_by_date[current_as_of], start=1):
        reason = _beginner_eligibility_reason(candidate, capital)
        if reason == "not_go":
            continue
        if reason is not None:
            excluded_counts[reason] += 1
            continue

        symbol = str(candidate.get("symbol") or "")
        streak = 0
        if current_as_of in ordered_dates:
            date_index = ordered_dates.index(current_as_of)
            while date_index >= 0:
                streak_candidate = candidate_for(ordered_dates[date_index], symbol)
                if (
                    streak_candidate is None
                    or _beginner_eligibility_reason(streak_candidate, capital) is not None
                ):
                    break
                streak += 1
                date_index -= 1
        else:
            streak = 1

        previous_candidate = (
            candidate_for(previous_as_of, symbol) if previous_as_of else None
        )
        close = float(candidate["close"])
        eligible.append(
            {
                **candidate,
                "candidate_state": "READY" if streak >= 2 else "GO1",
                "go_streak": streak,
                "global_rank": global_rank,
                "lot_size": _BEGINNER_LOT_SIZE,
                "lot_cost": round(close * _BEGINNER_LOT_SIZE, 2),
                "previous_as_of": previous_as_of,
                "previous_decision": (
                    str(previous_candidate.get("decision"))
                    if previous_candidate is not None
                    and previous_candidate.get("decision") is not None
                    else None
                ),
            }
        )

    eligible.sort(
        key=lambda value: (
            0 if value["candidate_state"] == "READY" else 1,
            int(value["global_rank"]),
        )
    )

    recent_dates = ordered_dates[-_MODEL_HEALTH_WINDOW:]
    published_sample_days = sum(
        record_as_of in records_by_date for record_as_of in recent_dates
    )
    complete_history = (
        len(recent_dates) == _MODEL_HEALTH_WINDOW
        and published_sample_days == _MODEL_HEALTH_WINDOW
    )
    has_ready = False
    if complete_history:
        eligible_symbols_by_date: dict[str, set[str]] = {}
        for record_as_of in recent_dates:
            eligible_symbols_by_date[record_as_of] = {
                str(value.get("symbol") or "")
                for value in records_by_date[record_as_of]
                if _beginner_eligibility_reason(value, capital) is None
            }
        has_ready = any(
            eligible_symbols_by_date[previous] & eligible_symbols_by_date[current]
            for previous, current in pairwise(recent_dates)
        )

    if not complete_history:
        model_health = {
            "status": "INSUFFICIENT_HISTORY",
            "sample_days": published_sample_days,
            "window_days": _MODEL_HEALTH_WINDOW,
            "message": (
                f"最近窗口内只有 {published_sample_days} 个已发布交易日快照; "
                f"满 {_MODEL_HEALTH_WINDOW} 日后才判断模型是否长期过严。"
            ),
        }
    elif has_ready:
        model_health = {
            "status": "OK",
            "sample_days": len(recent_dates),
            "window_days": _MODEL_HEALTH_WINDOW,
            "message": "最近10个完整交易日内已出现连续确认候选。",
        }
    else:
        model_health = {
            "status": "WARNING",
            "sample_days": len(recent_dates),
            "window_days": _MODEL_HEALTH_WINDOW,
            "message": "连续10个完整交易日没有任何候选进入可模拟状态, 模型需要校准。",
        }

    return {
        "candidates": eligible[: max(0, int(limit))],
        "excluded_counts": excluded_counts,
        "model_health": model_health,
    }


def monitor_published_plan(
    candidates: list[dict],
    live_cache: dict | None,
    data_phase: dict,
) -> list[dict]:
    """Monitor a sealed GO list with current strategy rows without selecting anew."""
    if data_phase.get("phase") not in {"LIVE_PROVISIONAL", "EOD_PENDING"}:
        return []
    source_as_of = str(data_phase.get("sealed_as_of") or "")
    monitor_as_of = str(data_phase.get("as_of") or "")
    if not source_as_of or not monitor_as_of or monitor_as_of <= source_as_of:
        return []

    cache = live_cache if isinstance(live_cache, dict) else {}
    results = cache.get("results") if isinstance(cache.get("results"), dict) else {}
    cache_as_of = str(cache.get("as_of") or "")
    monitored: list[dict] = []

    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or candidate.get("decision") != "GO"
            or candidate.get("risk_flags")
        ):
            continue
        symbol = str(candidate.get("symbol") or "")
        planned_strategies = [
            str(strategy_id)
            for strategy_id in candidate.get("strategies") or []
            if strategy_id
        ]
        current_results: dict[str, dict] = {}
        if cache_as_of == monitor_as_of:
            for strategy_id in planned_strategies:
                result = results.get(strategy_id)
                if (
                    isinstance(result, dict)
                    and str(result.get("as_of") or "") == monitor_as_of
                ):
                    current_results[strategy_id] = result

        matches: list[tuple[str, dict, float]] = []
        monitor_risks: list[str] = []
        for strategy_id, result in current_results.items():
            for row in result.get("rows") or []:
                if not isinstance(row, dict) or str(row.get("symbol") or "") != symbol:
                    continue
                score = _validated_strategy_score(row.get("score"))
                if score is None:
                    monitor_risks.append("盘中策略分数缺失或无效, 本次状态已隔离")
                else:
                    matches.append((strategy_id, row, score))
                monitor_risks.extend(
                    flag["message"]
                    for flag in _risk_flags(row, adjustment_event_on_as_of=False)
                )

        complete = bool(planned_strategies) and len(current_results) == len(
            planned_strategies
        )
        scores = [score for _, _, score in matches]
        highest = max(scores) if scores else 0.0
        average = sum(scores) / len(scores) if scores else 0.0
        score = round(
            min(
                100.0,
                highest * 0.7
                + average * 0.3
                + min(24.0, max(0, len(matches) - 1) * _CONSENSUS_BONUS),
            ),
            1,
        )
        if monitor_risks:
            status = "INVALIDATED"
            evidence = list(dict.fromkeys(monitor_risks))
        elif not complete:
            status = "PENDING"
            evidence = ["盘中策略结果尚未完整更新, 暂不能判断原计划状态"]
        elif len(matches) >= 2 and score >= _GO_SCORE:
            status = "TRIGGERED"
            count_label = "两条" if len(matches) == 2 else f"{len(matches)}条"
            evidence = [
                f"当前有{count_label}独立策略继续同向确认",
                f"盘中规则分数为 {score:.1f}, 达到研究复核门槛",
            ]
        elif data_phase.get("phase") == "EOD_PENDING":
            status = "INVALIDATED"
            evidence = ["截至收盘未达到两条独立策略同向确认"]
        else:
            status = "PENDING"
            evidence = [
                f"当前有 {len(matches)} 条独立策略同向确认, 尚未达到研究复核条件"
            ]

        representative = max(matches, key=lambda item: item[2])[1] if matches else {}
        monitored.append(
            {
                **candidate,
                "plan_monitor": {
                    "status": status,
                    "as_of": monitor_as_of,
                    "strategy_ids": planned_strategies,
                    "last_price": _optional_finite_float(representative.get("close")),
                    "change_pct": _optional_finite_float(
                        representative.get("change_pct")
                    ),
                    "evidence": evidence,
                },
            }
        )
    return monitored[:3]


def _market_gate(report_as_of: Any, market_overview: Any) -> dict:
    gate = {
        "decision": "NOT_APPLIED",
        "as_of": None,
        "breadth_total": None,
        "emotion_score": None,
        "emotion_label": None,
        "reasons": [],
    }
    if market_overview is None:
        return gate
    if not isinstance(market_overview, dict) or market_overview.get("available") is False:
        gate["decision"] = "BLOCK"
        gate["reasons"] = ["市场概览不可用, 无法确认当前市场风险"]
        return gate

    overview_as_of = (
        str(market_overview.get("as_of"))
        if market_overview.get("as_of")
        else None
    )
    gate["as_of"] = overview_as_of

    breadth = (
        market_overview.get("breadth")
        if isinstance(market_overview.get("breadth"), dict)
        else {}
    )
    try:
        breadth_total = int(breadth.get("total"))
    except (TypeError, ValueError, OverflowError):
        breadth_total = None
    gate["breadth_total"] = breadth_total

    emotion = (
        market_overview.get("emotion")
        if isinstance(market_overview.get("emotion"), dict)
        else {}
    )
    try:
        emotion_score = float(emotion.get("score"))
    except (TypeError, ValueError, OverflowError):
        emotion_score = None
    if (
        emotion_score is None
        or not math.isfinite(emotion_score)
        or not 0 <= emotion_score <= 100
    ):
        emotion_score = None
    elif emotion_score.is_integer():
        emotion_score = int(emotion_score)
    emotion_label = str(emotion.get("label") or "").strip() or None
    gate["emotion_score"] = emotion_score
    gate["emotion_label"] = emotion_label

    reasons: list[str] = []
    if report_as_of and overview_as_of != str(report_as_of):
        reasons.append(
            f"市场概览日期 {overview_as_of or '未知'} 与策略日期 {report_as_of} 不一致"
        )
    if breadth_total is None or breadth_total <= 0:
        reasons.append("市场广度覆盖为空, 无法确认全市场状态")
    if emotion_score is None:
        reasons.append("市场情绪分缺失或无效")
    elif emotion_score < _BEGINNER_MARKET_SCORE:
        reasons.append(
            f"市场情绪为{emotion_label or '偏弱'}({emotion_score}分), "
            f"低于新手研究门槛{int(_BEGINNER_MARKET_SCORE)}分"
        )

    gate["decision"] = "BLOCK" if reasons else "PASS"
    gate["reasons"] = reasons
    return gate


def build_beginner_daily_brief(recommendations: dict | None) -> dict:
    """Turn an existing deterministic recommendation result into a beginner brief."""
    report = recommendations if isinstance(recommendations, dict) else {}
    data_gate = report.get("data_gate") if isinstance(report.get("data_gate"), dict) else {}
    data_phase = report.get("data_phase") if isinstance(report.get("data_phase"), dict) else {}
    market_gate = _market_gate(report.get("as_of"), report.get("market_overview"))
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    plan_source_as_of = str(report.get("plan_source_as_of") or "") or None
    progress = build_beginner_candidate_progress(
        candidates,
        as_of=str(report.get("as_of") or "") or None,
        history=(
            report.get("candidate_history")
            if isinstance(report.get("candidate_history"), list)
            else []
        ),
        trading_dates=(
            report.get("trading_dates")
            if isinstance(report.get("trading_dates"), list)
            else []
        ),
        practice_capital=report.get("practice_capital", 10_000),
        limit=3,
    )
    research_candidates = [
        _beginner_candidate(candidate)
        for candidate in progress["candidates"]
        if isinstance(candidate, dict)
    ]
    if (
        data_phase.get("phase") in {"LIVE_PROVISIONAL", "EOD_PENDING"}
        and not plan_source_as_of
    ):
        research_candidates = []

    if data_phase.get("phase") == "LIVE_PROVISIONAL":
        action_state = "OBSERVE_ONLY"
        live_as_of = str(data_phase.get("as_of") or "当前交易日")
        if plan_source_as_of:
            today_message = (
                f"盘中数据已更新至 {live_as_of}, 正在监控 {plan_source_as_of} "
                "盘后计划。今天仍只观察, 不临时新增研究标的。"
            )
            next_step = "只核对原计划的盘中状态; 收盘后等待新快照替换旧计划。"
        else:
            today_message = (
                f"盘中数据已更新至 {live_as_of}, 但尚未盘后封存。"
                "今天只观察, 不进行模拟或研究筛选。"
            )
            next_step = "等待盘后刷新完成并发布同日研究快照后, 再重新生成日报。"
    elif data_phase.get("phase") == "EOD_PENDING":
        action_state = "OBSERVE_ONLY"
        pending_as_of = str(data_phase.get("as_of") or "当前交易日")
        if plan_source_as_of:
            today_message = (
                f"盘后行情已更新至 {pending_as_of}, 正在等待新快照替换 "
                f"{plan_source_as_of} 盘后计划。封存前仍只观察。"
            )
            next_step = "等待盘后流程发布同日研究快照; 发布成功后旧计划自动结束。"
        else:
            today_message = (
                f"盘后行情已更新至 {pending_as_of}, 但盘后数据尚未封存。"
                "今天只观察, 不进行模拟或研究筛选。"
            )
            next_step = "等待盘后刷新完成并发布同日研究快照后, 再重新生成日报。"
    elif data_gate.get("decision") != "PASS":
        action_state = "OBSERVE_ONLY"
        reasons = data_gate.get("reasons") if isinstance(data_gate.get("reasons"), list) else []
        actions = (
            data_gate.get("next_actions")
            if isinstance(data_gate.get("next_actions"), list)
            else []
        )
        primary_reason = str(reasons[0]) if reasons else "数据检查尚未通过"
        today_message = (
            f"数据检查未通过: {primary_reason}。"
            "今天只观察, 不进行模拟或研究筛选。"
        )
        next_step = (
            str(actions[0])
            if actions
            else "先处理数据检查中的问题, 再重新生成日报。"
        )
    elif market_gate["decision"] == "BLOCK":
        reason = (
            str(market_gate["reasons"][0])
            if market_gate["reasons"]
            else "市场风险条件未通过"
        )
        action_state = "OBSERVE_ONLY"
        today_message = (
            f"数据检查已通过, 但{reason}。"
            "新手模式今天只观察, 不记录模拟成交。"
        )
        next_step = "只记录盘后观察结果, 等市场情绪回到震荡或更强后再复核。"
    elif progress["model_health"]["status"] == "WARNING" and not any(
        candidate.get("candidate_state") == "READY"
        for candidate in research_candidates
    ):
        action_state = "MODEL_WARNING"
        today_message = (
            "数据和市场检查已通过, 但连续10个完整交易日没有候选完成连续确认。"
            "今天暂停新增模拟买入, 进入模型校准。"
        )
        next_step = "先回放最近10个交易日并检查筛选漏斗, 不直接降低评分门槛。"
    elif any(
        candidate.get("candidate_state") == "READY"
        for candidate in research_candidates
    ):
        action_state = "SIMULATE_ONLY"
        ready_count = sum(
            candidate.get("candidate_state") == "READY"
            for candidate in research_candidates
        )
        today_message = (
            f"数据和市场检查已通过, 有 {ready_count} 只候选连续2个交易日保持确认。"
            "仅允许按100股做模拟练习, 不代表可以买入实盘。"
        )
        next_step = "只选择页面标为“可模拟练习”的候选, 先写失效条件再记录模拟成交。"
    elif any(
        candidate.get("candidate_state") == "GO1"
        for candidate in research_candidates
    ):
        action_state = "RESEARCH_ONLY"
        today_message = (
            "数据和市场检查已通过, 候选仅完成第1天确认。"
            "今天只加入观察, 不记录模拟买入。"
        )
        next_step = "只等待下一可信交易日复核; 连续确认才进入模拟练习, 中断就重新计数。"
    else:
        action_state = "NO_CANDIDATE"
        today_message = (
            "数据和市场检查已通过, 但本批没有候选同时通过新手筛选条件。"
            "今天不新增模拟买入。"
        )
        next_step = "查看淘汰原因和下一交易日的新结果, 不把未通过候选强行加入自选。"

    return {
        "as_of": report.get("as_of"),
        "generated_at": report.get("generated_at"),
        "snapshot_id": report.get("snapshot_id"),
        "snapshot_published_at": report.get("snapshot_published_at"),
        "plan_source_as_of": plan_source_as_of,
        "data_phase": data_phase,
        "action_state": action_state,
        "today_message": today_message,
        "next_step": next_step,
        "data_gate": data_gate,
        "market_gate": market_gate,
        "model_health": progress["model_health"],
        "excluded_counts": progress["excluded_counts"],
        "method": report.get("method") if isinstance(report.get("method"), dict) else {},
        "candidates": research_candidates,
        "disclaimer": "仅供个人研究与模拟练习; 进入研究清单不构成任何交易指令或收益承诺, 历史结果不代表未来表现。",
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

    plan_monitor = candidate.get("plan_monitor")
    if not isinstance(plan_monitor, dict):
        plan_monitor = None

    candidate_state = (
        str(candidate.get("candidate_state"))
        if candidate.get("candidate_state") in {"GO1", "READY"}
        else "GO1"
    )
    go_streak = max(1, int(candidate.get("go_streak") or 1))
    lot_size = max(1, int(candidate.get("lot_size") or _BEGINNER_LOT_SIZE))
    if candidate_state == "READY":
        observation_conditions = [
            f"已连续 {go_streak} 个可信交易日通过相同筛选",
            f"仅按 {lot_size} 股记录模拟练习, 不连接券商或真实下单",
        ]
    else:
        observation_conditions = [
            "今天是第 1 个可信交易日确认",
            "下一可信交易日仍通过相同筛选才升级为可模拟练习",
        ]

    result = {
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name"),
        "research_decision": decision,
        "candidate_state": candidate_state,
        "go_streak": go_streak,
        "global_rank": candidate.get("global_rank"),
        "lot_size": lot_size,
        "lot_cost": candidate.get("lot_cost"),
        "previous_as_of": candidate.get("previous_as_of"),
        "previous_decision": candidate.get("previous_decision"),
        "deterministic_reasons": reasons,
        "observation_conditions": observation_conditions,
        "invalidation_conditions": [
            "任一必需数据回执异常或运行时校验失败",
            "下一可信交易日不再通过筛选时, 连续天数立即归零",
            "出现涨跌停、异常涨跌幅、ST或其他风险标记",
        ],
        "risk_flags": candidate.get("risk_flags")
        if isinstance(candidate.get("risk_flags"), list)
        else [],
    }
    if plan_monitor is not None:
        result["plan_monitor"] = plan_monitor
    return result
