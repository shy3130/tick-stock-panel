"""Thin workbench routes for content-locked research DAGs."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from tivelo_contracts.base import Digest
from tivelo_contracts.errors import TiveloError

from app.services.engine_jobs.artifacts import read_published_report
from app.services.engine_jobs.models import JobRequest
from app.services.engine_jobs.price_variants import SavePriceVariant, save_price_variant
from app.services.engine_jobs.schedule_models import ScheduleControl, ScheduleRequest
from app.services.engine_jobs.storage import JobError

router = APIRouter(prefix="/api/engine-jobs", tags=["engine-jobs"])


def manager(request):
    value = getattr(request.app.state, "engine_job_manager", None)
    if value is None:
        raise HTTPException(503, detail={"code": "UNAVAILABLE", "message": "研究任务服务不可用"})
    return value


def invoke(operation):
    try:
        return operation()
    except JobError as exc:
        status = 404 if exc.code == "NOT_FOUND" else (503 if exc.code == "UNAVAILABLE" else 409)
        raise HTTPException(status, detail={"code": exc.code, "message": str(exc)}) from exc
    except TiveloError as exc:
        raise HTTPException(
            409, detail={"code": str(exc.code), "message": "引擎制品校验失败"}
        ) from exc
    except (ValidationError, ValueError, OSError) as exc:
        raise HTTPException(
            400, detail={"code": "INVALID_INPUT", "message": "研究输入不存在或格式不正确"}
        ) from exc


@router.get("/schema")
def schema():
    return JobRequest.model_json_schema()


@router.get("/inputs")
def inputs(request: Request):
    from app.services.engine_jobs.catalog import input_catalog

    return invoke(lambda: input_catalog(manager(request).store))


@router.get("/price-views")
def price_views(request: Request):
    from tivelo_extensions.store import EnginePackageStore

    from app.backtest.dynamic_daily import PRO_PROVIDER, supports_ordinary_replay

    def choices():
        views = EnginePackageStore(manager(request).store.data_dir).price_choices()
        for choice in views:
            if choice["provider_id"] == PRO_PROVIDER:
                choice["ordinary_backtest"] = supports_ordinary_replay(choice.get("version"))
        return views

    return invoke(choices)


@router.post("/inputs/price-variants")
def save_variant(request: Request, body: SavePriceVariant):
    return invoke(lambda: save_price_variant(manager(request).store, body))


@router.get("/schedules/schema")
def schedule_schema():
    return ScheduleRequest.model_json_schema()


@router.get("/schedules/calendar-presets")
def schedule_calendar_presets():
    from app.services.engine_jobs.schedule_calendar import calendar_choices

    return invoke(lambda: {"items": calendar_choices()})


@router.post("/schedules")
def create_schedule(request: Request, body: ScheduleRequest):
    return invoke(lambda: manager(request).create_schedule(body))


@router.get("/schedules")
def list_schedules(request: Request, limit: int = Query(default=50, ge=1, le=100)):
    return invoke(lambda: {"items": manager(request).schedule_store().list(limit=limit)})


@router.get("/schedules/{schedule_id}")
def get_schedule(request: Request, schedule_id: str):
    return invoke(lambda: manager(request).schedule_store().get(schedule_id))


@router.post("/schedules/{schedule_id}/enabled")
def enable_schedule(request: Request, schedule_id: str, body: ScheduleControl):
    return invoke(lambda: manager(request).set_schedule_enabled(schedule_id, body.enabled))


@router.get("/schedules/{schedule_id}/deliveries")
def schedule_deliveries(
    request: Request, schedule_id: str, limit: int = Query(default=50, ge=1, le=100)
):
    return invoke(
        lambda: {"items": manager(request).schedule_store().deliveries(schedule_id, limit=limit)}
    )


@router.get("/schedules/{schedule_id}/calendar")
def schedule_calendar(request: Request, schedule_id: str):
    return invoke(lambda: manager(request).schedule_store().calendar(schedule_id).model_dump(mode="json"))


@router.post("/preview")
def preview(request: Request, body: JobRequest):
    return invoke(lambda: manager(request).preview(body))


@router.post("")
def submit(request: Request, body: JobRequest, expected_plan_id: Digest | None = None):
    service = manager(request)
    return invoke(lambda: service.submit(body, expected_plan_id=expected_plan_id))


@router.get("")
def list_jobs(request: Request, limit: int = Query(default=50, ge=1, le=100)):
    return invoke(lambda: {"items": manager(request).store.list(limit=limit)})


@router.get("/{job_id}")
def get_job(request: Request, job_id: str):
    return invoke(lambda: manager(request).store.get(job_id))


@router.get("/{job_id}/plan")
def get_plan(request: Request, job_id: str):
    return invoke(lambda: manager(request).store.plan(job_id).model_dump(mode="json"))


@router.post("/{job_id}/cancel")
def cancel(request: Request, job_id: str):
    return invoke(lambda: manager(request).cancel(job_id))


@router.post("/{job_id}/retry")
def retry(request: Request, job_id: str):
    return invoke(lambda: manager(request).retry(job_id))


@router.get("/{job_id}/reports/{node_id}")
def report(request: Request, job_id: str, node_id: str):
    store = manager(request).store
    return invoke(lambda: read_published_report(store, job_id, node_id).model_dump(mode="json"))


class _ResultDownload(StreamingResponse):
    def __init__(self, stream, size, name):
        from app.services.engine_jobs.result_export import DOWNLOAD_CHUNK

        self._stream = stream
        super().__init__(
            iter(lambda: stream.read(DOWNLOAD_CHUNK), b""),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Length": str(size),
                "Cache-Control": "private, no-store",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stream.close()


@router.get("/{job_id}/results/{node_id}.zip", response_class=StreamingResponse)
def result_zip(request: Request, job_id: str, node_id: str):
    from app.services.engine_jobs.result_export import prepare_result_zip

    def download():
        store = manager(request).store
        published = read_published_report(store, job_id, node_id)
        stream, size = prepare_result_zip(store, published)
        return _ResultDownload(stream, size, f"tivelo-result-{published.execution.identity}.zip")

    return invoke(download)


@router.get("/{job_id}/snapshots/{node_id}")
def snapshot(request: Request, job_id: str, node_id: str):
    def read():
        from app.services.engine_jobs.daily_snapshot import (
            daily_snapshot_scope,
            read_published_snapshot,
        )

        store = manager(request).store
        artifact, bundle, locked = read_published_snapshot(store, job_id, node_id)
        scope = daily_snapshot_scope(locked, bundle)
        return {
            "snapshot_id": bundle.snapshot.snapshot_id,
            "bundle_id": artifact.identity,
            "path": artifact.path,
            "backtest": {
                "symbols": sorted(scope.symbols.values()),
                "start": scope.start,
                "end": scope.end,
            } if scope else None,
            "datasets": [
                {
                    "version_id": version.version_id,
                    **version.contents.model_dump(
                        mode="json",
                        include={
                            "dataset_id",
                            "record_type",
                            "knowledge_basis",
                            "source_ids",
                            "price_basis",
                            "scope",
                            "coverage",
                        },
                    ),
                }
                for version in bundle.datasets
            ],
        }

    return invoke(read)
