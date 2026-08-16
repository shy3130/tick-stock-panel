"""Authenticated API for evidence-first, asynchronous stock research."""
from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.research_agent.harness import get_harness
from app.research_agent.skill import sanitize_question
from app.research_agent.store import ResearchRunCapacityError, run_store

router = APIRouter(prefix="/api/research-agent", tags=["research-agent"])
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


class CreateResearchRunRequest(BaseModel):
    symbol: str = Field(min_length=9, max_length=9)
    name: str = Field(default="", max_length=80)
    question: str = Field(default="", max_length=600)
    include_web_news: bool = True

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("标的代码格式应为 000001.SZ")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip())

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        return sanitize_question(value)


@router.post("/runs", status_code=202)
async def create_run(
    body: CreateResearchRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    try:
        run = run_store.create(
            symbol=body.symbol,
            name=body.name,
            question=body.question,
            include_web_news=body.include_web_news,
        )
    except ResearchRunCapacityError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    background_tasks.add_task(get_harness(request.app).run, run["id"])
    return {"run": run}


@router.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=60)) -> dict:
    return {"runs": run_store.list_recent(limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="研究任务不存在")
    return {"run": run}
