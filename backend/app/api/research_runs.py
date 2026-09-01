from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.research.catalog import factor_detail, get_factor, list_factors
from app.research.contracts import (
    PreflightRequest,
    PreflightResult,
    RunAccepted,
    RunCreateRequest,
    RunEvidenceLinkRequest,
    RunLinks,
    RunPatch,
)
from app.research.control import (
    PreflightBlocked,
    create_durable_run,
    plan_factor_run,
    spawn_full_market_worker,
    watch_full_market_process,
)
from app.research.job_store import (
    ACTIVE_JOB_STATUSES,
    FactorJobStore,
    InvalidRunIdError,
)
from app.research.preflight import preflight
from app.research.run_store import FactorRunStore
from app.research.runner import InteractiveWorker
from app.services.research_registry import ResearchStore


def install_error_handlers(app) -> None:
    """Install structured validation and HTTP errors for research routes."""

    async def validation_handler(request, exc):
        if not request.url.path.startswith("/api/research"):
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "retryable": False,
                    "field": None,
                    "details": {"errors": exc.errors()},
                }
            },
        )

    async def http_handler(request, exc):
        if not request.url.path.startswith("/api/research"):
            return await http_exception_handler(request, exc)
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            return JSONResponse(status_code=exc.status_code, content=detail)
        return await http_exception_handler(request, exc)

    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(HTTPException, http_handler)


router = APIRouter(prefix="/api/research", tags=["research-v2"])


def _services(request: Request):
    data_dir = (
        getattr(getattr(request.app.state, "datastore", None), "data_dir", None)
        or getattr(getattr(request.app.state, "repo", None), "data_dir", None)
        or "data"
    )
    if not hasattr(request.app.state, "research_jobs"):
        request.app.state.research_jobs = FactorJobStore(data_dir)
    if not hasattr(request.app.state, "research_runs"):
        request.app.state.research_runs = FactorRunStore(data_dir)
    if not hasattr(request.app.state, "research_worker"):
        request.app.state.research_worker = InteractiveWorker(
            request.app.state.research_jobs, request.app.state.research_runs
        )
    return (
        request.app.state.research_jobs,
        request.app.state.research_runs,
        request.app.state.research_worker,
    )


def _research_store(request: Request) -> ResearchStore:
    data_dir = (
        getattr(getattr(request.app.state, "datastore", None), "data_dir", None)
        or getattr(getattr(request.app.state, "repo", None), "data_dir", None)
        or "data"
    )
    return ResearchStore(data_dir)


def _error(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    retryable: bool = False,
    field: str | None = None,
):
    raise HTTPException(
        status_code=status,
        detail={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "field": field,
                "details": details or {},
            }
        },
    )


def _job_or_error(jobs: FactorJobStore, run_id: str) -> dict[str, Any]:
    try:
        record = jobs.get(run_id)
    except InvalidRunIdError:
        _error(400, "invalid_run_id", "invalid run_id", field="run_id")
    if record is None:
        _error(404, "run_not_found", "run not found")
    return record


@router.get("/factors")
def factors(
    request: Request,
    category: str | None = None,
    engineering_status: str | None = None,
    scope: str | None = None,
    query: str | None = None,
    data_status: str | None = None,
    verdict: str | None = None,
):
    jobs, _, _ = _services(request)
    latest: dict[str, dict[str, Any]] = {}
    for item in sorted(
        jobs.list_runs(),
        key=lambda value: (value.get("created_at", ""), value.get("run_id", "")),
        reverse=True,
    ):
        factor_id = item.get("factor_id")
        if factor_id and factor_id not in latest:
            latest[factor_id] = item
    entries = list_factors(
        category=category,
        engineering_status=engineering_status,
        scope=scope,
        query=query,
    )
    result: list[dict[str, Any]] = []
    for entry in entries:
        item = entry.model_dump(mode="json")
        run = latest.get(entry.id)
        item["latest_run_id"] = run.get("run_id") if run else None
        if run is not None:
            item["latest_data_status"] = run.get("data_status")
            item["latest_verdict"] = run.get("verdict")
        if data_status and item.get("latest_data_status") != data_status:
            continue
        if verdict and item.get("latest_verdict") != verdict:
            continue
        result.append(item)
    result.sort(
        key=lambda item: (
            next(
                (
                    run.get("created_at", "")
                    for run in latest.values()
                    if run.get("factor_id") == item["id"]
                ),
                "",
            ),
            item["id"],
        ),
        reverse=True,
    )
    return {"items": result}


@router.get("/factors/{factor_id}")
def factor(factor_id: str):
    definition = get_factor(factor_id)
    if definition is None:
        _error(404, "factor_not_found", "factor not found")
    return factor_detail(definition)


@router.post("/preflights", response_model=PreflightResult)
def preflights(body: PreflightRequest, request: Request):
    try:
        return preflight(getattr(request.app.state, "repo", None), body)
    except KeyError:
        _error(404, "factor_not_found", "factor not found")
    except ValidationError as exc:
        _error(422, "validation_error", "invalid factor parameters", {"errors": exc.errors()})


@router.post("/runs", response_model=RunAccepted, status_code=202)
async def create_run(body: RunCreateRequest, request: Request):
    definition = get_factor(body.factor_id)
    if definition is None:
        _error(404, "factor_not_found", "factor not found")
    try:
        plan = plan_factor_run(
            getattr(request.app.state, "repo", None),
            body.factor_id,
            body.scope,
            body.parameters,
        )
    except PreflightBlocked as exc:
        _error(
            409,
            "preflight_blocked",
            "preflight blocked run creation",
            {"blocking_reasons": exc.blocking_reasons},
        )
    except ValidationError as exc:
        _error(422, "validation_error", "invalid factor parameters", {"errors": exc.errors()})
    if body.scope.type == "full_market":
        active = any(
            item.get("scope", {}).get("type") == "full_market"
            and item.get("job_status") in ACTIVE_JOB_STATUSES
            for item in _services(request)[0].list_runs()
        )
        process = getattr(request.app.state, "full_market_process", None)
        if process is not None and process.poll() is None:
            active = True
        if active:
            _error(429, "full_market_busy", "another full-market run is active", retryable=True)
    created = datetime.now(UTC).isoformat()
    jobs, _, worker = _services(request)
    run_id = create_durable_run(
        jobs,
        plan,
        source_run_id=body.source_run_id,
        origin={"kind": "api"},
    )
    definition, scope, _checked, parameters = plan
    if scope.type == "full_market":
        data_dir = str(
            getattr(getattr(request.app.state, "datastore", None), "data_dir", None)
            or getattr(getattr(request.app.state, "repo", None), "data_dir", None)
            or "data"
        )
        try:
            process = spawn_full_market_worker(data_dir, run_id)
            request.app.state.full_market_process = process
            watch_full_market_process(process, jobs, run_id)
        except (OSError, subprocess.SubprocessError) as exc:
            jobs.transition(
                run_id,
                "failed",
                error={"code": "runner_spawn_failed", "message": str(exc)},
            )
            jobs.append_event(
                run_id, "failed", {"code": "runner_spawn_failed", "message": str(exc)}
            )
            _error(503, "runner_spawn_failed", "full-market worker failed to start", retryable=True)
    else:
        worker.submit(
            run_id, definition.id, getattr(request.app.state, "repo", None), scope, parameters
        )
    links = RunLinks(
        self=f"/api/research/runs/{run_id}",
        stream=f"/api/research/runs/{run_id}/stream",
        events=f"/api/research/runs/{run_id}/events",
    )
    return RunAccepted(
        run_id=run_id,
        job_status="pending",
        factor_id=definition.id,
        scope=scope,
        created_at=created,
        links=links,
    )


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    jobs, runs, _ = _services(request)
    record = _job_or_error(jobs, run_id)
    summary = runs.read_summary(run_id)
    manifest = runs.read_manifest(run_id)
    record["summary_available"] = summary is not None
    record["summary"] = summary
    record["result"] = summary
    record["result_profile"] = record.get("profile")
    record["artifact"] = manifest
    record["links"] = {
        "self": f"/api/research/runs/{run_id}",
        "stream": f"/api/research/runs/{run_id}/stream",
        "events": f"/api/research/runs/{run_id}/events",
    }
    record["hypotheses"] = [
        hypothesis.__dict__ for hypothesis in _research_store(request).hypotheses_for_run(run_id)
    ]
    return record


@router.post("/runs/{run_id}/links", status_code=201)
def link_run(run_id: str, body: RunEvidenceLinkRequest, request: Request):
    jobs, _, _ = _services(request)
    _job_or_error(jobs, run_id)
    try:
        hypothesis = _research_store(request).link_factor_run(
            body.hypothesis_id, run_id, body.summary
        )
    except KeyError:
        _error(404, "hypothesis_not_found", "hypothesis not found")
    except ValueError as exc:
        _error(400, "invalid_evidence", str(exc))
    return {"run_id": run_id, "hypothesis": hypothesis.__dict__}


@router.get("/runs")
def list_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    factor_id: str | None = None,
    job_status: str | None = None,
    verdict: str | None = None,
    scope_type: str | None = Query(default=None, alias="scope.type"),
    favorite: bool | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
):
    jobs, _, _ = _services(request)
    items = sorted(
        jobs.list_runs(),
        key=lambda item: (item.get("created_at", ""), item.get("run_id", "")),
        reverse=True,
    )
    if cursor:
        items = [
            item
            for item in items
            if f"{item.get('created_at', '')}|{item.get('run_id', '')}" < cursor
        ]
    if factor_id:
        items = [item for item in items if item.get("factor_id") == factor_id]
    if job_status:
        items = [item for item in items if item.get("job_status") == job_status]
    if verdict:
        items = [item for item in items if item.get("verdict") == verdict]
    if scope_type:
        items = [item for item in items if item.get("scope", {}).get("type") == scope_type]
    if favorite is not None:
        items = [item for item in items if item.get("favorite", False) == favorite]
    if created_after:
        items = [item for item in items if item.get("created_at", "") >= created_after]
    if created_before:
        items = [item for item in items if item.get("created_at", "") <= created_before]
    page = items[:limit]
    next_cursor = (
        f"{page[-1].get('created_at', '')}|{page[-1].get('run_id', '')}"
        if len(page) == limit
        else None
    )
    return {"items": page, "next_cursor": next_cursor}


@router.patch("/runs/{run_id}")
def patch_run(run_id: str, body: RunPatch, request: Request):
    jobs, _, _ = _services(request)
    _job_or_error(jobs, run_id)
    try:
        jobs.patch(
            run_id, {key: value for key, value in body.model_dump().items() if value is not None}
        )
    except ValueError as exc:
        _error(400, "invalid_run_id", str(exc))
    return get_run(run_id, request)


@router.post("/runs/{run_id}/cancellation")
async def cancel(run_id: str, request: Request):
    jobs, _, worker = _services(request)
    record = _job_or_error(jobs, run_id)
    if record.get("job_status") in {"completed", "failed", "cancelled", "interrupted"}:
        _error(409, "invalid_run_state", "run is terminal")
    if record.get("scope", {}).get("type") == "full_market":
        process = getattr(request.app.state, "full_market_process", None)
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()
        jobs.transition(run_id, "cancelled")
    else:
        worker.cancel(run_id)
    jobs.append_event(run_id, "cancelled")
    return jobs.get(run_id)


@router.get("/runs/{run_id}/events")
def events(
    run_id: str,
    request: Request,
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    symbol: str | None = None,
    arm: str | None = None,
    qualified: bool | None = None,
    reachable: bool | None = None,
    censor_code: str | None = None,
    date: str | None = None,
):
    jobs, runs, _ = _services(request)
    _job_or_error(jobs, run_id)
    items = runs.read_events(
        run_id,
        cursor,
        limit,
        {
            "symbol": symbol,
            "arm": arm,
            "qualified": qualified,
            "reachable": reachable,
            "censor_code": censor_code,
            "event_date": date,
        },
    )
    for item in items:
        if "date" in item and "event_date" not in item:
            item["event_date"] = item.pop("date")
    return {"items": items, "next_cursor": cursor + len(items) if len(items) == limit else None}


@router.get("/runs/{run_id}/series")
def series(
    run_id: str,
    request: Request,
    kind: str | None = None,
    kinds: list[str] | None = None,
    max_points: int = Query(500, ge=1, le=2000),
):
    jobs, runs, _ = _services(request)
    _job_or_error(jobs, run_id)
    selected = [part for value in (kinds or []) for part in value.split(",")]
    if kind:
        selected.append(kind)
    return {"series": runs.read_series(run_id, selected or None, max_points)}


@router.get("/runs/{run_id}/stream")
def stream(run_id: str, request: Request, last_event_id: int | None = None):
    jobs, _, _ = _services(request)
    _job_or_error(jobs, run_id)
    header_id = request.headers.get("Last-Event-ID")
    if header_id is not None and (not header_id.isdigit() or int(header_id) < 0):
        _error(400, "invalid_last_event_id", "Last-Event-ID must be a non-negative integer")
    if last_event_id is not None and last_event_id < 0:
        _error(400, "invalid_last_event_id", "last_event_id must be non-negative")
    sent = int(header_id if header_id is not None else (last_event_id or 0))

    async def generate():
        nonlocal sent
        current = jobs.get(run_id)
        yield f"event: snapshot\ndata: {json.dumps(current or {}, ensure_ascii=False)}\n\n"
        while True:
            for event in jobs.events(run_id, sent, 200):
                sent = event["seq"]
                payload = dict(event.get("payload") or {})
                if "date" in payload and "event_date" not in payload:
                    payload["event_date"] = payload.pop("date")
                yield (
                    f"id: {sent}\nevent: {event['event_type']}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            current = jobs.get(run_id)
            if current and current.get("job_status") in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                break
            await asyncio.sleep(0.5)
        yield "event: heartbeat\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
