"""Read-only deterministic recommendation API."""
from __future__ import annotations

from datetime import date

import polars as pl
from fastapi import APIRouter, Query, Request

from app.data_providers.trust import load_latest_audits
from app.services import paper_account, strategy_cache
from app.services.advisor import (
    build_advisor_recommendations,
    build_beginner_daily_brief,
    monitor_published_plan,
)
from app.services.market_overview_builder import build_market_overview
from app.services.research_snapshot import (
    ResearchSnapshotCorruptError,
    load_latest_research_snapshot,
    load_research_snapshot_history,
    research_snapshot_source_problem,
)

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_INTRADAY_MARKET_PHASES = {
    "preopen",
    "morning",
    "morning_final",
    "pre_afternoon",
    "afternoon",
}

_ADJ_FACTOR_RUNTIME_PROBLEM = {
    "code": "ADJ_FACTOR_RUNTIME_UNAVAILABLE",
    "reason": "除权因子文件缺失、无法读取或结构不完整, 无法核对策略日期的除权除息事件",
    "next_action": (
        "请重新同步除权因子, 并确认 all.parquet 包含 symbol、trade_date 列后"
        "再重新生成研究清单。"
    ),
}


def _load_adjustment_event_symbols(
    data_dir,
    as_of: str | None,
) -> tuple[set[str], dict[str, str] | None]:
    if not as_of:
        return set(), None
    try:
        frame = pl.read_parquet(
            data_dir / "adj_factor" / "all.parquet",
            columns=["symbol", "trade_date"],
        )
        if frame.schema["symbol"] != pl.String or frame.schema["trade_date"] != pl.Date:
            raise ValueError("unexpected adjustment-factor schema")
        if frame.filter(
            pl.col("symbol").is_null()
            | (pl.col("symbol").str.strip_chars() == "")
            | pl.col("trade_date").is_null()
        ).height:
            raise ValueError("invalid adjustment-factor values")
    except Exception:
        return set(), dict(_ADJ_FACTOR_RUNTIME_PROBLEM)
    symbols = set(
        frame.filter(pl.col("trade_date").cast(pl.Utf8) == as_of)
        .get_column("symbol")
        .drop_nulls()
        .cast(pl.Utf8)
        .to_list()
    )
    return symbols, None


def _iso_date(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    text = str(value).strip()
    return text or None


def _latest_repo_date(repo, method_name: str, *args) -> str | None:
    method = getattr(repo, method_name, None)
    if not callable(method):
        return None
    try:
        return _iso_date(method(*args))
    except Exception:
        return None


def _quote_status(request: Request) -> dict:
    service = getattr(request.app.state, "quote_service", None)
    status_method = getattr(service, "status", None)
    if not callable(status_method):
        return {}
    try:
        status = status_method()
    except Exception:
        return {}
    return status if isinstance(status, dict) else {}


def _strategy_cache_with_realtime(request: Request, data_dir) -> dict | None:
    """Overlay current in-memory monitor results on the persisted strategy cache."""
    cached = strategy_cache.read_cache(data_dir)
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    latest_method = getattr(monitor_engine, "latest_strategy_results", None)
    if not callable(latest_method):
        return cached
    try:
        realtime_results = latest_method()
    except Exception:
        return cached
    if not isinstance(realtime_results, dict) or not realtime_results:
        return cached

    merged = dict(cached) if isinstance(cached, dict) else {}
    merged["results"] = {
        **(merged.get("results") if isinstance(merged.get("results"), dict) else {}),
        **realtime_results,
    }
    result_dates = {
        str(result.get("as_of"))
        for result in realtime_results.values()
        if isinstance(result, dict) and result.get("as_of")
    }
    if len(result_dates) == 1:
        merged["as_of"] = result_dates.pop()
    return merged


def _data_phase(
    request: Request,
    *,
    snapshot: dict | None,
    live_cache: dict | None,
) -> dict:
    """Describe persisted live rows separately from a published research snapshot."""
    repo = request.app.state.repo
    daily_as_of = _latest_repo_date(repo, "latest_daily_date")
    enriched_as_of = _latest_repo_date(repo, "latest_enriched_date", "stock")
    strategy_as_of = (
        str(live_cache.get("as_of"))
        if isinstance(live_cache, dict) and live_cache.get("as_of")
        else None
    )
    sealed_as_of = (
        str(snapshot.get("as_of"))
        if isinstance(snapshot, dict) and snapshot.get("as_of")
        else None
    )
    quote_status = _quote_status(request)
    market_phase = str(quote_status.get("market_phase") or "") or None
    current_dates = [value for value in (daily_as_of, enriched_as_of) if value]
    current_as_of = max(current_dates) if current_dates else strategy_as_of or sealed_as_of
    persisted_live_pair = bool(
        daily_as_of
        and enriched_as_of
        and daily_as_of == enriched_as_of
        and (sealed_as_of is None or daily_as_of > sealed_as_of)
    )

    if persisted_live_pair and market_phase in _INTRADAY_MARKET_PHASES:
        phase = "LIVE_PROVISIONAL"
    elif persisted_live_pair:
        phase = "EOD_PENDING"
    elif sealed_as_of:
        phase = "EOD_SEALED"
        current_as_of = sealed_as_of
    else:
        phase = "UNAVAILABLE"

    last_quote_ms = quote_status.get("last_fetch_ms")
    if isinstance(last_quote_ms, bool) or not isinstance(last_quote_ms, (int, float)):
        last_quote_ms = None
    return {
        "phase": phase,
        "as_of": current_as_of,
        "sealed_as_of": sealed_as_of,
        "daily_as_of": daily_as_of,
        "enriched_as_of": enriched_as_of,
        "strategy_as_of": strategy_as_of,
        "market_phase": market_phase,
        "last_quote_ms": last_quote_ms,
    }


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _is_date_mismatch_reason(reason: str, label: str) -> bool:
    return (
        (f"{label}截止日" in reason and "策略日期" in reason)
        or ("策略结果仍为" in reason and f"{label}已更新至" in reason)
    )


def _explain_unsealed_data(recommendations: dict, data_phase: dict) -> None:
    """Replace stale-sync advice when current rows exist but are not EOD sealed."""
    phase = data_phase.get("phase")
    if phase not in {"LIVE_PROVISIONAL", "EOD_PENDING"}:
        return
    gate = recommendations.get("data_gate")
    if not isinstance(gate, dict):
        return
    datasets = gate.get("datasets")
    if not isinstance(datasets, dict):
        return

    phase_word = "盘中临时数据" if phase == "LIVE_PROVISIONAL" else "盘后待封存数据"
    wait_action = "等待盘后刷新完成并生成同日可信回执, 不需要重复手动同步。"
    for key, label, actual_field in (
        ("daily", "日K", "daily_as_of"),
        ("daily_enriched", "派生日K", "enriched_as_of"),
    ):
        detail = datasets.get(key)
        actual_as_of = data_phase.get(actual_field)
        if not isinstance(detail, dict) or not actual_as_of:
            continue
        previous_reasons = detail.get("reasons")
        reason_values = previous_reasons if isinstance(previous_reasons, list) else []
        kept_reasons = [
            str(reason)
            for reason in reason_values
            if not _is_date_mismatch_reason(str(reason), label)
        ]
        receipt_as_of = str(detail.get("observed_end") or "未知")
        _append_unique(
            kept_reasons,
            [
                f"{label}盘后可信回执截止 {receipt_as_of}; "
                f"{phase_word}已更新至 {actual_as_of}, 尚未封存"
            ],
        )
        detail["reasons"] = kept_reasons
        detail["next_actions"] = [wait_action]

    reasons: list[str] = []
    next_actions: list[str] = []
    for detail in datasets.values():
        if not isinstance(detail, dict):
            continue
        detail_reasons = detail.get("reasons")
        detail_actions = detail.get("next_actions")
        if isinstance(detail_reasons, list):
            _append_unique(reasons, [str(value) for value in detail_reasons])
        if isinstance(detail_actions, list):
            _append_unique(next_actions, [str(value) for value in detail_actions])
    runtime_problems = gate.get("runtime_problems")
    if isinstance(runtime_problems, list):
        for problem in runtime_problems:
            if not isinstance(problem, dict):
                continue
            if problem.get("reason"):
                _append_unique(reasons, [str(problem["reason"])])
            if problem.get("next_action"):
                _append_unique(next_actions, [str(problem["next_action"])])
    gate["reasons"] = reasons
    gate["next_actions"] = next_actions


def _persisted_recommendations(request: Request, *, limit: int) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    live_cache = _strategy_cache_with_realtime(request, data_dir)
    snapshot_problem = None
    try:
        snapshot = load_latest_research_snapshot(data_dir)
    except ResearchSnapshotCorruptError:
        snapshot = None
        snapshot_problem = {
            "code": "RESEARCH_SNAPSHOT_CORRUPT",
            "reason": "已发布研究快照损坏或校验失败",
            "next_action": "请重新运行一次盘后刷新, 生成新的可信研究快照。",
        }
    if snapshot is not None:
        audits = snapshot["audits"]
        cache = snapshot["strategy_cache"]
        snapshot_problem = research_snapshot_source_problem(data_dir, snapshot)
    else:
        audits = load_latest_audits(data_dir)
        cache = live_cache
        if snapshot_problem is None:
            snapshot_problem = {
                "code": "RESEARCH_SNAPSHOT_MISSING",
                "reason": "尚无通过完整盘后流程发布的研究快照",
                "next_action": "请运行一次盘后刷新, 等研究快照发布成功后再查看候选。",
            }
    as_of = str(cache.get("as_of")) if isinstance(cache, dict) and cache.get("as_of") else None
    adjustment_event_symbols, adjustment_factor_problem = _load_adjustment_event_symbols(
        data_dir,
        as_of,
    )
    recommendations = build_advisor_recommendations(
        audits,
        cache,
        limit=limit,
        adjustment_event_symbols=adjustment_event_symbols,
        adjustment_factor_problem=adjustment_factor_problem,
        research_snapshot_problem=snapshot_problem,
    )
    recommendations["snapshot_id"] = snapshot.get("snapshot_id") if snapshot else None
    recommendations["snapshot_published_at"] = (
        snapshot.get("published_at") if snapshot else None
    )
    data_phase = _data_phase(
        request,
        snapshot=snapshot,
        live_cache=live_cache,
    )
    recommendations["data_phase"] = data_phase
    plan_source_as_of = None
    if snapshot is not None and data_phase.get("phase") in {
        "LIVE_PROVISIONAL",
        "EOD_PENDING",
    }:
        monitored_candidates = monitor_published_plan(
            recommendations.get("candidates") or [],
            live_cache,
            data_phase,
        )
        recommendations["candidates"] = monitored_candidates
        if monitored_candidates:
            plan_source_as_of = str(snapshot.get("as_of") or "") or None
    recommendations["plan_source_as_of"] = plan_source_as_of
    _explain_unsealed_data(recommendations, data_phase)
    return recommendations


def _market_overview_for_brief(request: Request, as_of: str | None) -> dict:
    if not as_of:
        return {"available": False, "as_of": None}
    try:
        parsed_as_of = date.fromisoformat(as_of)
        overview = build_market_overview(
            repo=request.app.state.repo,
            quote_service=getattr(request.app.state, "quote_service", None),
            depth_service=getattr(request.app.state, "depth_service", None),
            as_of=parsed_as_of,
        )
    except Exception:
        return {"available": False, "as_of": as_of}
    if not isinstance(overview, dict):
        return {"available": False, "as_of": as_of}
    return {**overview, "available": True}


def _published_candidate_history(data_dir, as_of: str | None) -> list[dict]:
    history: list[dict] = []
    for snapshot in load_research_snapshot_history(
        data_dir,
        before_as_of=as_of,
        limit=10,
    ):
        # 历史快照是“当日实际发布过什么”的不可变记录。加载器已经校验其内容哈希。
        # 后续复权修订或全量重算可以改变旧分区,但不能抹掉当时的研究证据。
        # 只有最新快照用于今天决策时才必须继续比对当前源文件。
        snapshot_as_of = str(snapshot.get("as_of") or "")
        adjustment_symbols, adjustment_problem = _load_adjustment_event_symbols(
            data_dir,
            snapshot_as_of,
        )
        recommendations = build_advisor_recommendations(
            snapshot.get("audits") if isinstance(snapshot.get("audits"), list) else [],
            (
                snapshot.get("strategy_cache")
                if isinstance(snapshot.get("strategy_cache"), dict)
                else None
            ),
            limit=10_000,
            adjustment_event_symbols=adjustment_symbols,
            adjustment_factor_problem=adjustment_problem,
        )
        history.append(
            {
                "as_of": snapshot_as_of,
                "snapshot_id": snapshot.get("snapshot_id"),
                "candidates": recommendations.get("candidates") or [],
            }
        )
    return history


def _trading_dates_through(data_dir, as_of: str | None) -> list[str]:
    if not as_of:
        return []
    root = data_dir / "kline_daily"
    if not root.exists():
        return []
    return sorted(
        {
            path.name.removeprefix("date=")
            for path in root.glob("date=????-??-??")
            if path.is_dir() and path.name.removeprefix("date=") <= as_of
        }
    )


def _practice_capital(data_dir) -> float:
    account_path = data_dir / "user_data" / "paper_account.json"
    if not account_path.exists():
        return float(paper_account.DEFAULT_INITIAL_CASH)
    try:
        account = paper_account.get_account(data_dir)
        value = float(account.get("initial_cash"))
    except Exception:
        return float(paper_account.DEFAULT_INITIAL_CASH)
    return value if value > 0 else float(paper_account.DEFAULT_INITIAL_CASH)


def _persisted_daily_brief(request: Request) -> dict:
    recommendations = _persisted_recommendations(request, limit=10_000)
    data_dir = request.app.state.repo.store.data_dir
    as_of = recommendations.get("as_of")
    return build_beginner_daily_brief(
        {
            **recommendations,
            "candidate_history": _published_candidate_history(data_dir, as_of),
            "trading_dates": _trading_dates_through(data_dir, as_of),
            "practice_capital": _practice_capital(data_dir),
            "market_overview": _market_overview_for_brief(
                request,
                recommendations.get("as_of"),
            ),
        }
    )


@router.get("/recommendations")
def recommendations(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    return _persisted_recommendations(request, limit=limit)


@router.get("/daily-brief")
def daily_brief(request: Request) -> dict:
    return _persisted_daily_brief(request)
