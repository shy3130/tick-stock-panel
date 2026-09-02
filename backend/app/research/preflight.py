from __future__ import annotations

from contextlib import suppress
from datetime import date, timedelta
from typing import Any

from app.research.catalog import get_factor
from app.research.contracts import (
    BlockingReason,
    CohortEstimate,
    PreflightRequest,
    PreflightResult,
    PreflightSource,
    ResourceEstimate,
)
from app.research.pinning import PinnedResearchRepository, reader_identity


def _requirement_reader(repo: Any, factor_id: str, kind: str) -> tuple[str, Any]:
    if kind == "canonical":
        from app.services.research_sealed_data import PublishedCanonicalDailyReader

        return kind, PublishedCanonicalDailyReader.from_repository(repo)
    if kind == "markets":
        return kind, getattr(repo, "generation_pinned_market_facts_reader", None)
    if kind == "index_daily":
        return kind, getattr(repo, "index_daily_research_reader", None)
    if kind == "universe":
        if factor_id == "volume-breakout":
            return "eligible_universe", getattr(repo, "pit_eligible_universe", None)
        return kind, getattr(repo, "pit_presence_universe", None) or getattr(
            repo, "pit_universe", None
        )
    if kind == "calendar":
        return kind, getattr(repo, "versioned_exchange_calendar", None)
    return kind, None


def _source(
    kind: str, reader: Any, start: date | None = None, end: date | None = None
) -> PreflightSource:
    generation, digest = reader_identity(reader)
    if not generation or not digest:
        raise ValueError(f"{kind} pinned generation or manifest is unavailable")
    available_from = available_to = None
    days = getattr(reader, "market_days", None)
    if callable(days) and start is not None and end is not None:
        values = list(days(start, end))
        if values:
            available_from, available_to = min(values), max(values)
    return PreflightSource(
        kind=kind,
        status="ready",
        generation=generation,
        manifest_sha256=digest,
        available_from=available_from,
        available_to=available_to,
    )

def _collect_requirement_source(
    repo: Any,
    factor_id: str,
    kind: str,
    start: date | None,
    end: date | None,
    sources: list[PreflightSource],
    blockers: list[BlockingReason],
    warnings: list[str],
) -> None:
    if kind in {"minutes", "trans"}:
        if factor_id == "mtf-direction" and kind == "trans":
            _collect_ordered_trans_source(repo, sources, blockers)
        elif factor_id == "escape-risk" and kind == "minutes":
            _collect_escape_intraday_source(repo, start, end, sources, warnings)
        else:
            warnings.append(
                f"{kind} reader is optional; unavailable signals remain explicitly censored"
            )
        return
    source_kind = "eligible_universe" if kind == "universe" and factor_id == "volume-breakout" else kind
    try:
        source_kind, reader = _requirement_reader(repo, factor_id, kind)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        reader = None
    if reader is None:
        sources.append(PreflightSource(kind=source_kind, status="missing"))
        blockers.append(
            BlockingReason(
                code=f"{source_kind}_reader_missing",
                message=f"{source_kind} reader unavailable",
                source=source_kind,
            )
        )
        return
    try:
        sources.append(_source(source_kind, reader, start, end))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        sources.append(PreflightSource(kind=source_kind, status="missing"))
        blockers.append(
            BlockingReason(
                code=f"{source_kind}_provenance_unavailable",
                message=f"{source_kind} generation or manifest unavailable",
                source=source_kind,
            )
        )
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


def _collect_ordered_trans_source(
    repo: Any,
    sources: list[PreflightSource],
    blockers: list[BlockingReason],
) -> None:
    from app.data_providers.registry import get_active_provider_name, get_provider

    reader = None
    provider = None
    try:
        provider = get_provider(get_active_provider_name(capability="ordered_trans_research"))
        opener = getattr(provider, "open_ordered_trans_reader", None)
        reader = opener() if callable(opener) else None
        if reader is None:
            sources.append(PreflightSource(kind="ordered_trans", status="missing"))
            blockers.append(
                BlockingReason(
                    code="ordered_trans_reader_missing",
                    message="ordered-trans research reader unavailable",
                    source="ordered_trans",
                )
            )
            return
        sources.append(_source("ordered_trans", reader))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        sources.append(PreflightSource(kind="ordered_trans", status="missing"))
        blockers.append(
            BlockingReason(
                code="ordered_trans_provenance_unavailable",
                message="ordered-trans generation or manifest unavailable",
                source="ordered_trans",
            )
        )
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        close = getattr(provider, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


def _collect_escape_intraday_source(
    repo: Any,
    start: date | None,
    end: date | None,
    sources: list[PreflightSource],
    warnings: list[str],
    *,
    lookback_calendar_days: int = 30,
) -> None:
    if any(source.kind == "escape_intraday" for source in sources):
        return
    if start is None or end is None:
        warnings.append("escape-risk intraday route pin skipped because the date window is missing")
        return
    from app.data_providers.registry import get_active_provider_name, get_provider
    from app.services.research_sealed_data import PublishedCanonicalDailyReader

    canonical = None
    reader = None
    provider = None
    try:
        canonical = PublishedCanonicalDailyReader.from_repository(repo)
        if canonical is None:
            warnings.append("escape-risk intraday route pin unavailable")
            return
        days = canonical.market_days(start - timedelta(days=lookback_calendar_days), end)
        provider = get_provider(get_active_provider_name(capability="minute"))
        opener = getattr(provider, "open_escape_risk_intraday_reader", None)
        reader = opener(canonical.manifest(), tuple(days)) if callable(opener) else None
        if reader is None:
            warnings.append("escape-risk intraday route pin unavailable")
            return
        generation, manifest = reader_identity(reader)
        route_pins = getattr(reader, "route_pins", None)
        routes = route_pins() if callable(route_pins) else None
        if not isinstance(routes, dict):
            raise ValueError("escape-risk route pins unavailable")
        if not generation or not manifest:
            raise ValueError("escape-risk route identity unavailable")
        sources.append(
            PreflightSource(
                kind="escape_intraday",
                status="ready",
                generation=generation,
                manifest_sha256=manifest,
                pin={"routes": routes},
                available_from=days[0] if days else start,
                available_to=days[-1] if days else end,
            )
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        warnings.append(f"escape-risk intraday route pin unavailable: {exc}")
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        close = getattr(canonical, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        close = getattr(provider, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

def preflight(repo: Any, request: PreflightRequest) -> PreflightResult:
    factor = get_factor(request.factor_id)
    if factor is None:
        raise KeyError("factor_not_found")
    parameters = factor.request_model.model_validate(request.parameters)
    normalized = parameters.model_dump(mode="json")
    if request.scope.type not in factor.supported_scopes:
        return PreflightResult(
            ready=False,
            factor_id=factor.id,
            normalized_request=normalized,
            blocking_reasons=[
                BlockingReason(
                    code="scope_unsupported", message="factor does not support requested scope"
                )
            ],
        )
    symbols = request.scope.symbols or []
    if factor.max_symbols and len(symbols) > factor.max_symbols:
        return PreflightResult(
            ready=False,
            factor_id=factor.id,
            normalized_request=normalized,
            blocking_reasons=[
                BlockingReason(
                    code="symbol_limit_exceeded", message=f"maximum symbols is {factor.max_symbols}"
                )
            ],
        )
    if factor.min_symbols and symbols and len(symbols) < factor.min_symbols:
        return PreflightResult(
            ready=False,
            factor_id=factor.id,
            normalized_request=normalized,
            blocking_reasons=[
                BlockingReason(
                    code="minimum_symbols_not_met",
                    message=f"minimum symbols is {factor.min_symbols}",
                )
            ],
        )
    if request.scope.type == "full_market":
        from app.research.catalog import resolve_full_market_executor
        from app.services.full_market_research import (
            collect_cohort,
            reader_provenance,
            resolve_pinned_reader,
        )

        blockers: list[BlockingReason] = []
        sources: list[PreflightSource] = []
        warnings: list[str] = []
        try:
            executor_available = (
                factor.full_market_executor is not None
                and resolve_full_market_executor(factor.id) is not None
            )
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
            executor_available = False
        if not executor_available:
            blockers.append(
                BlockingReason(
                    code="full_market_executor_unavailable",
                    message="factor has no controlled full-market executor",
                )
            )
        start = normalized.get("start")
        end = normalized.get("end")
        start_date = date.fromisoformat(start) if isinstance(start, str) else None
        end_date = date.fromisoformat(end) if isinstance(end, str) else None
        reader = resolve_pinned_reader(repo)
        cohort_count = 0
        try:
            if reader is None:
                blockers.append(
                    BlockingReason(
                        code="pinned_reader_missing", message="pinned reader unavailable"
                    )
                )
            elif start_date is None or end_date is None:
                blockers.append(
                    BlockingReason(
                        code="date_window_missing", message="full-market date window required"
                    )
                )
            else:
                try:
                    provenance = reader_provenance(reader)
                    cohort = collect_cohort(reader, start_date, end_date)
                    cohort_count = len(cohort)
                    sources.append(
                        PreflightSource(
                            kind="full_market",
                            status="ready",
                            generation=provenance["generation"],
                            manifest_sha256=provenance["manifest_sha256"],
                            available_from=start_date,
                            available_to=end_date,
                        )
                    )
                    component_provenance = reader.source_provenance()
                    for source_kind in ("canonical", "markets"):
                        component = component_provenance.get(source_kind)
                        if not isinstance(component, dict):
                            raise ValueError(f"{source_kind} component pin is unavailable")
                        component_generation = component.get("generation")
                        component_manifest = component.get("manifest_sha256")
                        if not isinstance(component_generation, str) or not component_generation:
                            raise ValueError(f"{source_kind} component generation is invalid")
                        if (
                            not isinstance(component_manifest, str)
                            or len(component_manifest) != 64
                            or any(
                                value not in "0123456789abcdef"
                                for value in component_manifest.lower()
                            )
                        ):
                            raise ValueError(f"{source_kind} component manifest is invalid")
                        sources.append(
                            PreflightSource(
                                kind=source_kind,
                                status="ready",
                                generation=component_generation,
                                manifest_sha256=component_manifest.lower(),
                                available_from=start_date if source_kind == "canonical" else None,
                                available_to=end_date if source_kind == "canonical" else None,
                            )
                        )
                    pinned_repo = PinnedResearchRepository.from_sources(
                        repo, [source.model_dump(mode="json") for source in sources]
                    )
                    for kind in factor.data_requirements:
                        if kind in {"canonical", "markets"}:
                            continue
                        requirement_repo = (
                            pinned_repo if kind in {"calendar", "index_daily"} else repo
                        )
                        _collect_requirement_source(
                            requirement_repo,
                            factor.id,
                            kind,
                            start_date,
                            end_date,
                            sources,
                            blockers,
                            warnings,
                        )
                    if factor.id == "doji-patterns":
                        _collect_escape_intraday_source(
                            repo,
                            start_date,
                            end_date,
                            sources,
                            warnings,
                            lookback_calendar_days=0,
                        )
                except Exception as exc:
                    blockers.append(
                        BlockingReason(
                            code="pinned_generation_or_cohort_invalid",
                            message="pinned generation, manifest, or PIT cohort unavailable",
                            details={"reason": str(exc)},
                        )
                    )
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        return PreflightResult(
            ready=not blockers,
            factor_id=factor.id,
            normalized_request=normalized,
            sources=sources,
            cohort=CohortEstimate(
                requested_symbols=0, eligible_symbols=cohort_count, censored_symbols=0
            ),
            warnings=warnings,
            blocking_reasons=blockers,
            resource_estimate=ResourceEstimate(
                resource_class="full_market", full_market_supported=True
            ),
        )
    sources: list[PreflightSource] = []
    blockers: list[BlockingReason] = []
    warnings: list[str] = []
    start = normalized.get("start")
    end = normalized.get("end")
    start_date = date.fromisoformat(start) if isinstance(start, str) else None
    end_date = date.fromisoformat(end) if isinstance(end, str) else None
    for kind in factor.data_requirements:
        _collect_requirement_source(
            repo,
            factor.id,
            kind,
            start_date,
            end_date,
            sources,
            blockers,
            warnings,
        )
    cohort = CohortEstimate(
        requested_symbols=len(symbols), eligible_symbols=len(symbols), censored_symbols=0
    )
    return PreflightResult(
        ready=not blockers,
        factor_id=factor.id,
        normalized_request=normalized,
        sources=sources,
        cohort=cohort,
        warnings=warnings,
        blocking_reasons=blockers,
        resource_estimate=ResourceEstimate(),
    )
