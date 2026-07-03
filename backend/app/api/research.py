from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.research_registry import ResearchStore
from app.services.scheduled_research import ScheduledResearchStore, register_jobs, run_schedule

router = APIRouter(prefix="/api/research", tags=["research"])


class HypothesisIn(BaseModel):
    title: str
    thesis: str
    status: str = "exploring"
    tags: list[str] = []


class HypothesisPatch(BaseModel):
    title: str | None = None
    thesis: str | None = None
    status: str | None = None
    tags: list[str] | None = None


class EvidenceIn(BaseModel):
    kind: str
    ref: str = ""
    summary: str


class ScheduleIn(BaseModel):
    name: str
    template: str
    cron: str
    enabled: bool = True
    params: dict = {}


class SchedulePatch(BaseModel):
    name: str | None = None
    template: str | None = None
    cron: str | None = None
    enabled: bool | None = None
    params: dict | None = None


def _store(request: Request) -> ResearchStore:
    data_dir = request.app.state.repo.store.data_dir
    return ResearchStore(data_dir)


def _schedule_store(request: Request) -> ScheduledResearchStore:
    return ScheduledResearchStore(request.app.state.repo.store.data_dir)


def _refresh_scheduler(request: Request) -> None:
    register_jobs(getattr(request.app.state, "scheduler", None), _schedule_store(request), request.app.state)


@router.get("/hypotheses")
def list_hypotheses(request: Request, status: str | None = None, query: str | None = None):
    try:
        return {"items": [h.__dict__ for h in _store(request).search(status=status, query=query)]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/hypotheses")
def create_hypothesis(body: HypothesisIn, request: Request):
    try:
        return _store(request).create_hypothesis(body.title, body.thesis, body.status, body.tags).__dict__
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/hypotheses/{hyp_id}")
def get_hypothesis(hyp_id: str, request: Request):
    try:
        return _store(request).get_hypothesis(hyp_id).__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="hypothesis not found") from e


@router.patch("/hypotheses/{hyp_id}")
def update_hypothesis(hyp_id: str, body: HypothesisPatch, request: Request):
    try:
        return _store(request).update_hypothesis(hyp_id, **body.model_dump(exclude_unset=True)).__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="hypothesis not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/hypotheses/{hyp_id}/evidence")
def add_evidence(hyp_id: str, body: EvidenceIn, request: Request):
    try:
        return _store(request).add_evidence(hyp_id, body.kind, body.ref, body.summary).__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="hypothesis not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/run-cards/{run_id}")
def get_run_card(run_id: str, request: Request):
    try:
        return _store(request).get_run_card(run_id).__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="run card not found") from e


@router.get("/schedules")
def list_schedules(request: Request):
    return {"items": [x.__dict__ for x in _schedule_store(request).list()]}


@router.post("/schedules")
def create_schedule(body: ScheduleIn, request: Request):
    try:
        item = _schedule_store(request).create(body.name, body.template, body.cron, body.enabled, body.params)
        _refresh_scheduler(request)
        return item.__dict__
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: SchedulePatch, request: Request):
    try:
        item = _schedule_store(request).patch(schedule_id, **body.model_dump(exclude_unset=True))
        _refresh_scheduler(request)
        return item.__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="schedule not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, request: Request):
    ok = _schedule_store(request).delete(schedule_id)
    _refresh_scheduler(request)
    return {"ok": ok}


@router.post("/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: str, request: Request):
    try:
        store = _schedule_store(request)
        item = store.get(schedule_id)
        result = run_schedule(item, request.app.state)
        store.save(item)
        return {"schedule": item.__dict__, "result": result}
    except KeyError as e:
        raise HTTPException(status_code=404, detail="schedule not found") from e
