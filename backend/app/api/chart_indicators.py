"""Custom chart definitions and read-only evaluations."""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.indicators.chart import ChartIndicator, compile_indicator
from app.services import chart_indicators as service

router = APIRouter(prefix="/api/chart-indicators", tags=["chart-indicators"])


@router.get("/library")
def get_library():
    try:
        return service.get_library()
    except ValueError as exc:
        raise HTTPException(400, f"图表指标配置无效：{exc}") from exc


@router.put("/library")
def save_library(body: service.LibrarySave):
    try:
        return service.save_library(body)
    except service.LibraryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/evaluate")
def evaluate(request: Request, body: service.EvaluateRequest):
    return service.evaluate(body, getattr(request.app.state, "repo", None))


@router.post("/compile")
def compile_definition(body: ChartIndicator):
    """Check source without market data; saving uses the same compiler."""
    try:
        _, warmup, dependencies = compile_indicator(body)
        return {"valid": True, "warmup": warmup, "dependencies": sorted(dependencies)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/export")
def export_definitions(body: service.DefinitionsExport):
    try:
        package = service.export_definitions(body.ids, body.revision)
    except service.LibraryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        json.dumps(package, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="chart-indicators.json"'},
    )


@router.post("/import")
def import_definitions(body: service.DefinitionsImport):
    try:
        return service.import_definitions(body)
    except service.LibraryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
