from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.services.research_registry import ResearchStore
from app.services.scheduled_research import (
    ScheduledResearchStore,
    register_jobs,
    run_schedule,
)
from app.services.short_pool import (
    MAX_LIMIT,
    MIN_LIMIT,
    T_RESEARCH_RESERVED_TAGS,
    build_t_research_hypothesis,
    run_short_pool,
)

router = APIRouter(prefix="/api/research", tags=["research"])


class HypothesisIn(BaseModel):
    title: str
    thesis: str
    status: str = "exploring"
    tags: list[str] = []


class TResearchConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    as_of: date
    limit: StrictInt = Field(ge=MIN_LIMIT, le=MAX_LIMIT)


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


def _has_reserved_t_research_tag(tags: list[str]) -> bool:
    return any(tag in T_RESEARCH_RESERVED_TAGS or tag.startswith("short_pool:") for tag in tags)


def _schedule_store(request: Request) -> ScheduledResearchStore:
    return ScheduledResearchStore(request.app.state.repo.store.data_dir)


def _refresh_scheduler(request: Request) -> None:
    register_jobs(
        getattr(request.app.state, "scheduler", None), _schedule_store(request), request.app.state
    )


@router.get("/hypotheses")
def list_hypotheses(request: Request, status: str | None = None, query: str | None = None):
    try:
        return {"items": [h.__dict__ for h in _store(request).search(status=status, query=query)]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/hypotheses")
def create_hypothesis(body: HypothesisIn, request: Request):
    if _has_reserved_t_research_tag(body.tags):
        raise HTTPException(
            status_code=400,
            detail="做T研究系统标签只能通过显式确认入口创建",
        )
    try:
        return (
            _store(request)
            .create_hypothesis(body.title, body.thesis, body.status, body.tags)
            .__dict__
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/t-suitability/hypotheses")
def confirm_t_research_hypothesis(body: TResearchConfirmIn, request: Request):
    """重算观察池与 T-1 市场门禁后，唯一写入一条 exploring 研究假设。"""
    try:
        pool = run_short_pool(request.app.state, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="做T研究确认所需数据暂不可用",
        ) from exc
    if pool["pool_id"] != body.pool_id or pool["as_of"] != body.as_of.isoformat():
        raise HTTPException(
            status_code=409,
            detail="短线观察池已变化，请刷新后重新确认",
        )
    try:
        hypothesis = build_t_research_hypothesis(pool)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return (
        _store(request)
        .create_or_get_hypothesis_by_tag(f"short_pool:{pool['pool_id']}", **hypothesis)
        .__dict__
    )


@router.get("/hypotheses/{hyp_id}")
def get_hypothesis(hyp_id: str, request: Request):
    try:
        return _store(request).get_hypothesis(hyp_id).__dict__
    except KeyError as e:
        raise HTTPException(status_code=404, detail="hypothesis not found") from e


@router.patch("/hypotheses/{hyp_id}")
def update_hypothesis(hyp_id: str, body: HypothesisPatch, request: Request):
    if body.tags is not None and _has_reserved_t_research_tag(body.tags):
        raise HTTPException(
            status_code=400,
            detail="做T研究系统标签只能通过显式确认入口创建",
        )
    store = _store(request)
    try:
        existing = store.get_hypothesis(hyp_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="hypothesis not found") from e
    patch = body.model_dump(exclude_unset=True)
    if _has_reserved_t_research_tag(existing.tags) and set(patch) - {"status"}:
        raise HTTPException(
            status_code=400,
            detail="做T研究系统假设的标题/论点/标签为固定协议，仅允许更新状态",
        )
    try:
        return store.update_hypothesis(hyp_id, **patch).__dict__
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
        item = _schedule_store(request).create(
            body.name, body.template, body.cron, body.enabled, body.params
        )
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
