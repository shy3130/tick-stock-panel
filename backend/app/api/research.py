from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.research_registry import ResearchStore

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


def _store(request: Request) -> ResearchStore:
    data_dir = request.app.state.repo.store.data_dir
    return ResearchStore(data_dir)


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
