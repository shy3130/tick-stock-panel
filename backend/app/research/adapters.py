from __future__ import annotations

from .contracts import EventTableRow, NormalizedResearchResult


def _close(resource) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _event_rows(raw: dict) -> list[EventTableRow]:
    rows: list[EventTableRow] = []
    for collection in ("events", "censored"):
        values = raw.get(collection)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            rows.append(
                EventTableRow(
                    symbol=item.get("symbol"),
                    arm=item.get("arm") or item.get("arm_id"),
                    event_date=item.get("event_date")
                    or item.get("signal_date")
                    or item.get("date"),
                    qualified=item.get("qualified"),
                    reachable=item.get("reachable"),
                    censor_code=item.get("censor_code") or item.get("code") or item.get("reason"),
                    label=item.get("label"),
                    detail=item,
                )
            )
    return rows


def _norm(profile, raw):
    nested = raw.get("verdict")
    detail = dict(nested) if isinstance(nested, dict) else raw
    verdict = detail.get("value") or detail.get("verdict")
    if isinstance(verdict, dict):
        verdict = verdict.get("value") or verdict.get("verdict")

    explicit_status = detail.get("status") or raw.get("status")
    unavailable = explicit_status in {"unavailable", "missing"} or verdict == "unavailable"
    status = "unavailable" if unavailable else "ready"
    if verdict not in {"accepted", "rejected", "unavailable", "inconclusive"}:
        verdict = "unavailable" if unavailable else "inconclusive"

    reasons = detail.get("unavailable_reasons")
    if not isinstance(reasons, list):
        reasons = raw.get("unavailable_reasons")
    if not isinstance(reasons, list):
        reasons = []
    if unavailable and not reasons:
        reasons = [
            {
                "code": str(
                    detail.get("reason")
                    or detail.get("unavailable_reason")
                    or raw.get("reason")
                    or raw.get("unavailable_reason")
                    or "evaluator_unavailable"
                ),
                "detail": detail.get("detail") or raw.get("detail", ""),
            }
        ]

    payload = dict(raw)
    if detail is not raw:
        payload["verdict_detail"] = detail
    if profile == "arm_comparison":
        payload["arms"] = detail.get("arms") or detail.get("verdicts") or []
        payload["increments"] = detail.get("increments", [])
        payload["segments"] = detail.get("segments", {})
    elif profile == "event_signal":
        report = detail.get("report") if isinstance(detail.get("report"), dict) else {}
        payload["signals"] = report.get("signals", detail.get("signals", []))
        payload["report"] = report
    elif profile == "shape_distribution":
        payload["factors"] = detail.get("factors") or detail.get("patterns") or []
        payload["symbol_audit"] = detail.get("symbol_audit", [])
    elif profile == "retrieval":
        payload["routing"] = detail
    elif profile == "calendar_effect":
        payload["legs"] = detail.get("legs") or detail.get("windows") or []
        payload["sensitivity"] = detail.get("sensitivity", [])

    summary = dict(detail.get("summary") or {}) if isinstance(detail.get("summary"), dict) else {}
    summary.setdefault("status", explicit_status)
    summary.setdefault("schema", raw.get("schema") or detail.get("schema"))
    warnings = detail.get("warnings")
    if not isinstance(warnings, list):
        warnings = raw.get("warnings") if isinstance(raw.get("warnings"), list) else []
    series = detail.get("series") if isinstance(detail.get("series"), dict) else {}
    risk = detail.get("risk") if isinstance(detail.get("risk"), dict) else {}
    horizons = detail.get("horizons") if isinstance(detail.get("horizons"), list) else []
    provenance = {}
    for candidate in (raw.get("provenance"), detail.get("provenance")):
        if isinstance(candidate, dict):
            provenance.update(candidate)
    for key in ("generation", "manifest", "manifest_sha256", "cohort", "data_availability"):
        if key in detail:
            provenance.setdefault(key, detail[key])
    return NormalizedResearchResult(
        profile=profile,
        status=status,
        verdict=verdict,
        summary=summary,
        payload=payload,
        events=_event_rows(detail),
        series=series,
        horizons=horizons,
        risk=risk,
        provenance=provenance,
        unavailable_reasons=reasons,
        warnings=[str(item) for item in warnings],
    )


_DATA_STATUS_PRIORITY = {
    "missing": 0,
    "stale": 1,
    "censored": 2,
    "partial": 3,
    "ready": 4,
}


def result_data_status(result: NormalizedResearchResult, fallback: str = "ready") -> str:
    """Aggregate explicit data availability independently from the verdict."""
    availability = result.provenance.get("data_availability")
    statuses: list[str] = []
    if isinstance(availability, str):
        statuses.append("missing" if availability == "unavailable" else availability)
    elif isinstance(availability, dict):
        for value in availability.values():
            if isinstance(value, str):
                statuses.append("missing" if value == "unavailable" else value)
            elif isinstance(value, dict) and isinstance(value.get("status"), str):
                status = value["status"]
                statuses.append("missing" if status == "unavailable" else status)
    known = [status for status in statuses if status in _DATA_STATUS_PRIORITY]
    if not known:
        return fallback
    return min(known, key=_DATA_STATUS_PRIORITY.__getitem__)


def execute_factor(factor_id, repo, scope, params):
    p = params.model_dump() if hasattr(params, "model_dump") else dict(params)
    syms = scope.symbols
    if factor_id == "macd-arms":
        from app.services.macd_stages import evaluate_macd_arms, resolve_pinned_reader

        reader = resolve_pinned_reader(repo)
        try:
            return _norm(
                "arm_comparison",
                evaluate_macd_arms(
                    reader, start=p["start"], end=p["end"], symbols=syms, oos_start=p["oos_start"]
                ),
            )
        finally:
            if reader is not None and callable(getattr(reader, "close", None)):
                reader.close()
    if factor_id == "escape-risk":
        from datetime import timedelta

        from app.services.daily_event_research.production import evaluate_escape_risk_production
        from app.services.research_sealed_data import PublishedCanonicalDailyReader

        canonical = PublishedCanonicalDailyReader.from_repository(repo)
        if canonical is None:
            return _norm(
                "event_signal", {"status": "unavailable", "reason": "unavailable_canonical_reader"}
            )
        intraday_reader = None
        provider = None
        try:
            try:
                from app.data_providers.registry import get_active_provider_name, get_provider

                provider = get_provider(get_active_provider_name(capability="minute"))
                opener = getattr(provider, "open_escape_risk_intraday_reader", None)
                if callable(opener):
                    days = canonical.market_days(p["start"] - timedelta(days=30), p["end"])
                    intraday_reader = opener(canonical.manifest(), tuple(days))
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                intraday_reader = None
            return _norm(
                "event_signal",
                evaluate_escape_risk_production(
                    symbols=syms,
                    start=p["start"],
                    end=p["end"],
                    oos_start=p["oos_start"],
                    canonical_reader=canonical,
                    intraday_reader=intraday_reader,
                    cost_bps=p["cost_bps"],
                ),
            )
        finally:
            if callable(getattr(intraday_reader, "close", None)):
                intraday_reader.close()
            if callable(getattr(provider, "close", None)):
                provider.close()
            if callable(getattr(canonical, "close", None)):
                canonical.close()
    if factor_id == "chip-peak-patterns":
        from app.services.chip_peak_patterns import (
            ChipPeakRequest,
            evaluate,
            production_reader_scope,
        )

        req = ChipPeakRequest(
            symbols=syms,
            start=p["start"],
            oos_start=p["oos_start"],
            end=p["end"],
            cost_bps=p["cost_bps"],
        )
        with production_reader_scope(repo, req) as readers:
            return _norm("shape_distribution", evaluate(req, readers=readers))
    if factor_id == "n-shape":
        from app.services.n_shape_golden_phoenix import evaluate_n_shape, resolve_n_shape_reader

        reader = resolve_n_shape_reader(repo)
        try:
            return _norm(
                "event_signal",
                evaluate_n_shape(start=p["start"], end=p["end"], symbols=syms, reader=reader),
            )
        finally:
            if reader is not None and callable(getattr(reader, "close", None)):
                reader.close()
    return _extended(factor_id, repo, scope, p)


def _extended(factor_id, repo, scope, p):
    s = scope.symbols
    try:
        if factor_id == "weak-to-strong":
            from app.services.weak_to_strong import (
                WeakToStrongEvaluateRequest,
                evaluate_weak_to_strong_v1,
            )
            from app.services.weak_to_strong_research_data import production_reader_scope

            req = WeakToStrongEvaluateRequest(symbols=s, **p)
            with production_reader_scope(repo, req.signal_date.year) as reader:
                return _norm("event_signal", evaluate_weak_to_strong_v1(req, reader=reader))
        if factor_id == "volume-breakout":
            from app.services.volume_breakout import (
                evaluate_volume_breakout,
                resolve_pinned_reader,
                resolve_pit_universe,
                resolve_versioned_calendar,
            )

            r = resolve_pinned_reader(repo)
            universe = resolve_pit_universe(repo)
            calendar = resolve_versioned_calendar(repo)
            try:
                return _norm(
                    "arm_comparison",
                    evaluate_volume_breakout(
                        start=p["start"],
                        end=p["end"],
                        symbols=s,
                        pinned_reader=r,
                        pit_universe=universe,
                        calendar=calendar,
                        oos_start=p["oos_start"],
                        cost_bps=p["cost_bps"],
                    ),
                )
            finally:
                _close(r)
                _close(universe)
                _close(calendar)
        if factor_id == "n-depth":
            from app.services.n_shape_pullback_depth import (
                evaluate_n_shape_pullback_depth,
                resolve_n_shape_reader,
            )

            r = resolve_n_shape_reader(repo)
            try:
                return _norm(
                    "arm_comparison",
                    evaluate_n_shape_pullback_depth(
                        start=p["start"],
                        end=p["end"],
                        symbols=s,
                        reader=r,
                        reversal_mode=p["reversal_mode"],
                        reversal_value=p["reversal_value"],
                        cost_bps=p["cost_bps"],
                    ),
                )
            finally:
                if callable(getattr(r, "close", None)):
                    r.close()
        if factor_id == "daily-open-anchor":
            from app.services.daily_open_anchor_filter import (
                evaluate_daily_open_anchor,
                resolve_daily_open_anchor_canonical,
                unavailable_payload,
            )

            r = resolve_daily_open_anchor_canonical(repo)
            try:
                return _norm(
                    "arm_comparison",
                    unavailable_payload(["canonical_reader_missing"])
                    if r is None
                    else evaluate_daily_open_anchor(
                        canonical=r,
                        start=p["start"],
                        end=p["end"],
                        oos_start=p["oos_start"],
                        symbols=s,
                    ),
                )
            finally:
                _close(r)
        if factor_id == "single-yang-no-break":
            from app.data_providers.fquant.daily_market_research import (
                PublishedDailyMarketFactsReader,
            )
            from app.services.single_yang_no_break import (
                SingleYangCompositeReader,
                evaluate_single_yang,
                evaluate_single_yang_increment,
            )

            r = getattr(repo, "generation_pinned_daily_reader", None)
            inc = r
            m = None
            try:
                out = evaluate_single_yang(
                    reader=r,
                    start=p["start"],
                    end=p["end"],
                    symbols=s,
                    oos_start=p["oos_start"],
                    cost_bps=p["cost_bps"],
                )
                try:
                    m = PublishedDailyMarketFactsReader.from_canonical_manifest(r.manifest())
                    inc = SingleYangCompositeReader(r, m)
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                    pass
                out["increment_research"] = evaluate_single_yang_increment(
                    reader=inc,
                    start=p["start"],
                    end=p["end"],
                    symbols=s,
                    oos_start=p["oos_start"],
                    cost_bps=p["cost_bps"],
                )
                return _norm("arm_comparison", out)
            finally:
                if callable(getattr(m, "close", None)):
                    m.close()
                _close(r)
        if factor_id == "zuoyi-defense":
            from app.services.zuoyi_defense import evaluate_zuoyi_defense

            canonical = getattr(repo, "generation_pinned_daily_reader", None)
            market_facts = getattr(repo, "generation_pinned_market_facts_reader", None)
            try:
                return _norm(
                    "arm_comparison",
                    evaluate_zuoyi_defense(
                        canonical,
                        start=p["start"],
                        end=p["end"],
                        symbols=s,
                        oos_start=p["oos_start"],
                        cost_bps=p["cost_bps"],
                        market_facts_reader=market_facts,
                    ),
                )
            finally:
                _close(canonical)
                _close(market_facts)
        if factor_id in {"hold-firm", "pre-surge", "negative-exclusion"}:
            from app.services.hold_firm_patterns import (
                HoldFirmPatternsRequest,
                evaluate_hold_firm_patterns,
                production_reader_scope,
            )

            with production_reader_scope(repo) as q:
                if factor_id == "hold-firm":
                    request = HoldFirmPatternsRequest(symbols=s, **p)
                    return _norm(
                        "arm_comparison",
                        evaluate_hold_firm_patterns(
                            request, q.canonical, q.market_facts, q.universe_reader
                        ),
                    )
                if factor_id == "pre-surge":
                    from app.services.daily_event_research.production import (
                        evaluate_pre_surge_production,
                    )

                    return _norm(
                        "arm_comparison",
                        evaluate_pre_surge_production(
                            symbols=s,
                            canonical_reader=q.canonical,
                            market_facts_reader=q.market_facts,
                            universe_reader=q.universe_reader,
                            **p,
                        ),
                    )
                from app.services.negative_exclusion_production import (
                    evaluate_negative_exclusion_production,
                )

                return _norm(
                    "arm_comparison",
                    evaluate_negative_exclusion_production(
                        symbols=s,
                        canonical_reader=q.canonical,
                        market_facts_reader=q.market_facts,
                        universe_reader=q.universe_reader,
                        **p,
                    ),
                )
        if factor_id == "doji-patterns":
            from app.services.doji_patterns import (
                DojiPatternsRequest,
                evaluate_doji_patterns,
                production_reader_scope,
            )

            request = DojiPatternsRequest(symbols=s, **p)
            with production_reader_scope(repo) as q:
                return _norm(
                    "event_signal",
                    evaluate_doji_patterns(request, q.canonical, q.market_facts, q.universe_reader),
                )
        if factor_id == "weekly-flagpole":
            from app.services.weekly_flagpole import WeeklyFlagpoleRequest, evaluate, resolve_reader

            r = resolve_reader(repo)
            index_reader = getattr(repo, "index_daily_research_reader", None)
            try:
                return _norm(
                    "arm_comparison",
                    evaluate(
                        WeeklyFlagpoleRequest(symbols=s, **p),
                        r,
                        index_reader,
                    ),
                )
            finally:
                _close(r)
                _close(index_reader)
        if factor_id == "escape-windows":
            from app.services.escape_windows import EscapeWindowsRequest, evaluate_escape_windows

            canonical = getattr(repo, "generation_pinned_daily_reader", None)
            calendar = getattr(repo, "versioned_exchange_calendar", None)
            presence = getattr(repo, "pit_presence_universe", None)
            index_reader = getattr(repo, "index_daily_research_reader", None)
            try:
                return _norm(
                    "calendar_effect",
                    evaluate_escape_windows(
                        EscapeWindowsRequest(**p),
                        canonical_reader=canonical,
                        calendar=calendar,
                        presence_universe=presence,
                        index_reader=index_reader,
                    ),
                )
            finally:
                _close(canonical)
                _close(calendar)
                _close(presence)
                _close(index_reader)
        if factor_id == "mera":
            from app.services.research_sealed_data import PublishedCanonicalDailyReader
            from app.services.retrieval_routing_research import (
                DEFAULT_FEATURE_IDS,
                RetrievalRoutingRequest,
                RoutingUnavailableReason,
                build_pinned_factor_panel,
                evaluate_retrieval_routing,
                unavailable_routing_response,
            )

            request = RetrievalRoutingRequest(
                label_horizon=p["label_horizon"],
                cost_bps=p["cost_bps"],
                placebo_rounds=p["placebo_rounds"],
                feature_names=p.get("feature_names"),
            )
            canonical = PublishedCanonicalDailyReader.from_repository(repo)
            if canonical is None:
                return _norm(
                    "retrieval",
                    unavailable_routing_response(
                        request,
                        RoutingUnavailableReason.PANEL_COVERAGE,
                        "canonical history is not published",
                    ),
                )
            try:
                panel = build_pinned_factor_panel(
                    canonical,
                    s,
                    p["start"],
                    p["end"],
                    feature_ids=p.get("feature_names") or DEFAULT_FEATURE_IDS,
                    label_horizon=p["label_horizon"],
                )
                return _norm("retrieval", evaluate_retrieval_routing(panel, request))
            finally:
                _close(canonical)
        if factor_id == "dugu-trend":
            from app.data_providers.fquant.daily_market_research import (
                PublishedDailyMarketFactsReader,
            )
            from app.services.daily_event_research import DailyEventRequest, evaluate_daily_events
            from app.services.research_sealed_data import PublishedCanonicalDailyReader

            c = PublishedCanonicalDailyReader.from_repository(repo)
            if c is None:
                return _norm(
                    "arm_comparison",
                    {"status": "unavailable", "reason": "unavailable_canonical_reader"},
                )
            f = None
            try:
                f = PublishedDailyMarketFactsReader.from_canonical_manifest(c.manifest())
                return _norm(
                    "arm_comparison", evaluate_daily_events(DailyEventRequest(symbols=s, **p), c, f)
                )
            finally:
                if callable(getattr(f, "close", None)):
                    f.close()
                _close(c)
        if factor_id == "mtf-direction":
            from app.data_providers.registry import get_active_provider_name, get_provider
            from app.services.mtf_direction_15m5m import (
                MTFDirectionEvaluateIn,
                evaluate_mtf_direction,
                resolve_minute_reader,
            )

            reader = resolve_minute_reader()
            provider = None
            try:
                if reader is None:
                    provider = get_provider(
                        get_active_provider_name(capability="ordered_trans_research")
                    )
                    if not getattr(
                        getattr(provider, "capabilities", None), "ordered_trans_research", False
                    ):
                        reader = None
                    else:
                        reader = provider.open_ordered_trans_reader()
                return _norm(
                    "event_signal",
                    evaluate_mtf_direction(MTFDirectionEvaluateIn(symbols=s, **p), reader=reader),
                )
            finally:
                if callable(getattr(reader, "close", None)):
                    reader.close()
                if callable(getattr(provider, "close", None)):
                    provider.close()
    finally:
        pass
    raise ValueError(f"unknown factor adapter: {factor_id}")
