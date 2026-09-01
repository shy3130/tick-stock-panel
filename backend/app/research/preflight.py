from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import date
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


def _identity(reader: Any) -> tuple[str | None, str | None]:
    identity = getattr(reader, "identity", None)
    if callable(identity):
        value = identity()
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, dict):
            return value.get("generation"), value.get("manifest_sha256")
    pin = getattr(reader, "pin", None)
    if callable(pin):
        value = pin()
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, dict):
            generations = {
                key: item
                for key, item in value.items()
                if key.endswith("_generation") and isinstance(item, str) and item
            }
            manifests = {
                key: item
                for key, item in value.items()
                if key.endswith("_manifest_sha256") and isinstance(item, str) and item
            }
            if generations and len(generations) == len(manifests):
                generation = "|".join(
                    f"{key.removesuffix('_generation')}={generations[key]}"
                    for key in sorted(generations)
                )
                digest = hashlib.sha256(
                    json.dumps(manifests, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return generation, digest
    generation = getattr(reader, "generation", None)
    digest = getattr(reader, "manifest_sha256", None)
    return (
        generation() if callable(generation) else generation,
        digest() if callable(digest) else digest,
    )


def _source(
    kind: str, reader: Any, start: date | None = None, end: date | None = None
) -> PreflightSource:
    generation, digest = _identity(reader)
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
        reader = None
        if kind == "canonical":
            try:
                from app.services.research_sealed_data import PublishedCanonicalDailyReader

                reader = PublishedCanonicalDailyReader.from_repository(repo)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                reader = None
        elif kind == "markets":
            reader = getattr(repo, "generation_pinned_market_facts_reader", None)
        elif kind == "index_daily":
            reader = getattr(repo, "index_daily_research_reader", None)
        elif kind == "universe":
            reader = getattr(repo, "pit_presence_universe", None) or getattr(
                repo, "pit_universe", None
            )
        elif kind == "calendar":
            reader = getattr(repo, "versioned_exchange_calendar", None)
        elif kind in {"minutes", "trans"}:
            warnings.append(
                f"{kind} reader is optional; unavailable signals remain explicitly censored"
            )
            continue
        if reader is None:
            sources.append(PreflightSource(kind=kind, status="missing"))
            if kind in {"canonical", "markets", "universe", "calendar", "index_daily"}:
                blockers.append(
                    BlockingReason(
                        code=f"{kind}_reader_missing",
                        message=f"{kind} reader unavailable",
                        source=kind,
                    )
                )
        else:
            try:
                sources.append(_source(kind, reader, start_date, end_date))
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                sources.append(PreflightSource(kind=kind, status="missing"))
                blockers.append(
                    BlockingReason(
                        code=f"{kind}_provenance_unavailable",
                        message=f"{kind} generation or manifest unavailable",
                        source=kind,
                    )
                )
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()
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
