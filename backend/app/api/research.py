from __future__ import annotations

from datetime import date
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator

from app.services.macd_stages import (
    MacdStagesRequest,
    evaluate_macd_stages,
    macd_stages_availability,
)
from app.services.macd_stages import (
    resolve_pinned_reader as resolve_macd_reader,
)
from app.services.mtf_direction_15m5m import (
    MTFDirectionEvaluateIn,
    evaluate_mtf_direction,
    resolve_minute_reader,
)
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
from app.services.single_yang_no_break import (
    SINGLE_YANG_DEFINITION,
    evaluate_single_yang,
    run_single_yang_research,
)
from app.services.single_yang_no_break import (
    assess_capability as assess_single_yang_capability,
)
from app.services.zuoyi_defense import (
    ARMS,
    UNAVAILABLE_CODES,
    ZUOYI_DEFINITION,
    assess_capability as assess_zuoyi_capability,
    evaluate_zuoyi_defense,
)
from app.services.volume_breakout import (
    DEFAULT_COST_BPS,
    DEFAULT_OOS_START,
    VolumeBreakoutResponse,
)
from app.services.weak_to_strong import (
    WeakToStrongEvaluateRequest,
    WeakToStrongEvaluateResponse,
    evaluate_weak_to_strong_v1,
)

router = APIRouter(prefix="/api/research", tags=["research"])


class VolumeBreakoutEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[str] | None = Field(default=None, max_length=1000)
    oos_start: date = DEFAULT_OOS_START
    cost_bps: float = Field(default=DEFAULT_COST_BPS, ge=0)


@router.post("/factors/volume-breakout/evaluate", response_model=VolumeBreakoutResponse)
def evaluate_volume_breakout_factor(body: VolumeBreakoutEvaluateIn, request: Request):
    """量价序列突破研究契约；能力缺失/未实现时显式 unavailable。"""
    from app.services.volume_breakout import (
        evaluate_volume_breakout,
        resolve_pinned_reader,
        resolve_pit_universe,
        resolve_versioned_calendar,
    )

    try:
        pinned_reader = resolve_pinned_reader(request.app.state.repo)
        return evaluate_volume_breakout(
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            pinned_reader=pinned_reader,
            pit_universe=resolve_pit_universe(request.app.state.repo),
            calendar=(
                pinned_reader
                if pinned_reader is not None
                and callable(getattr(pinned_reader, "version", None))
                and callable(getattr(pinned_reader, "market_days", None))
                else resolve_versioned_calendar(request.app.state.repo)
            ),
            oos_start=body.oos_start,
            cost_bps=body.cost_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/factors/mtf-direction/evaluate")
def evaluate_mtf_direction_factor(body: MTFDirectionEvaluateIn):
    """运行 ordered-trans 多周期研究；每请求 owned provider/reader，注册 reader caller-owned。"""
    registered = resolve_minute_reader()
    if registered is not None:
        return evaluate_mtf_direction(body, reader=registered)
    provider = None
    reader = None
    try:
        try:
            from app.data_providers.registry import get_active_provider_name, get_provider
            provider = get_provider(get_active_provider_name(capability="ordered_trans_research"))
        except Exception:
            return evaluate_mtf_direction(body, reader=None)
        capabilities = getattr(provider, "capabilities", None)
        if not getattr(capabilities, "ordered_trans_research", False):
            return evaluate_mtf_direction(body, reader=None)
        opener = getattr(provider, "open_ordered_trans_reader", None)
        if not callable(opener):
            return evaluate_mtf_direction(body, reader=None)
        reader = opener()
        if reader is None:
            return evaluate_mtf_direction(body, reader=None)
        return evaluate_mtf_direction(body, reader=reader)
    finally:
        try:
            close_reader = getattr(reader, "close", None)
            if callable(close_reader):
                close_reader()
        finally:
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                close_provider()


class NShapeEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[str] | None = Field(default=None, max_length=1000)


@router.post("/factors/n-shape/evaluate")
def evaluate_n_shape_factor(body: NShapeEvaluateIn, request: Request):
    """运行只读 N 字形态研究；缺少 immutable markets 复合 reader 时显式 unavailable。"""
    from app.services.n_shape_golden_phoenix import evaluate_n_shape, resolve_n_shape_reader

    reader = resolve_n_shape_reader(getattr(request.app.state, "repo", None))
    try:
        return evaluate_n_shape(
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            reader=reader,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


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


class SingleYangEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[str] = Field(min_length=1, max_length=1000)
    oos_start: date
    cost_bps: float = Field(default=10.0, ge=0)


class ZuoyiDefenseEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[str] = Field(min_length=1, max_length=500)
    oos_start: date
    cost_bps: float = Field(default=10.0, ge=0, le=1000)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if not all(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value) for value in normalized):
            raise ValueError("symbols must use canonical 6-digit exchange symbols")
        return list(dict.fromkeys(normalized))


class ZuoyiDefenseOkResponse(BaseModel):
    status: Literal["ok"]
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_closed_contract(self):
        arms = self.model_extra.get("arms") if self.model_extra else None
        verdict = self.model_extra.get("verdict") if self.model_extra else None
        names = [item.get("arm") for item in arms] if isinstance(arms, list) and all(isinstance(item, dict) for item in arms) else []
        if names != list(ARMS):
            raise ValueError("response arms must contain the six approved arms in order")
        if not isinstance(verdict, dict) or verdict.get("value") not in {"accepted", "rejected"}:
            raise ValueError("ok response verdict must be accepted or rejected")
        return self


class ZuoyiDefenseUnavailableResponse(BaseModel):
    status: Literal["unavailable"]
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_closed_code(self):
        code = self.model_extra.get("code") if self.model_extra else None
        if code not in UNAVAILABLE_CODES:
            raise ValueError("unapproved unavailable code")
        return self
ZuoyiDefenseResponse = ZuoyiDefenseOkResponse | ZuoyiDefenseUnavailableResponse


@router.get("/zuoyi-defense")
def get_zuoyi_defense(request: Request):
    repo = getattr(request.app.state, "repo", None)
    reader = getattr(repo, "generation_pinned_daily_reader", None)
    markets = getattr(repo, "generation_pinned_market_facts_reader", None)
    try:
        capability = assess_zuoyi_capability(reader)
        generation = getattr(markets, "generation", lambda: "")() if markets else ""
        digest = getattr(markets, "pin_manifest_sha256", getattr(markets, "manifest_sha256", lambda: ""))() if markets else ""
        identity_fn = getattr(markets, "pin_identity_verified", None) if markets else None
        verified = identity_fn() if identity_fn is not None else bool(generation and digest)
        market_available = bool(generation and digest and verified)
        if not market_available:
            capability["available"] = False
            capability["status"] = "unavailable"
            capability.setdefault("reasons", []).append("markets_pin_identity_unverified")
        capability["markets"] = {"available": market_available, "generation": generation, "manifest_sha256": digest, "pin_verification_mode": getattr(markets, "pin_verification_mode", lambda: "legacy")() if markets else "missing"}
        return {**capability, "definition": ZUOYI_DEFINITION}
    finally:
        close = getattr(markets, "close", None)
        if close is not None:
            close()


@router.post("/factors/zuoyi-defense/evaluate", response_model=ZuoyiDefenseResponse)
async def evaluate_zuoyi_defense_factor(request: Request):
    try:
        body = ZuoyiDefenseEvaluateIn.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    repo = getattr(request.app.state, "repo", None)
    reader = getattr(repo, "generation_pinned_daily_reader", None)
    markets = getattr(repo, "generation_pinned_market_facts_reader", None)
    try:
        return evaluate_zuoyi_defense(
            reader, start=body.start, end=body.end, symbols=body.symbols,
            oos_start=body.oos_start, cost_bps=body.cost_bps, market_facts_reader=markets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        close = getattr(markets, "close", None)
        if close is not None:
            close()

@router.get("/single-yang-no-break")
def get_single_yang_no_break(request: Request):
    """返回单阳不破真实 sealed/raw 能力，不运行研究。"""
    repo = getattr(request.app.state, "repo", None)
    reader = getattr(repo, "generation_pinned_daily_reader", None)
    capability = assess_single_yang_capability(reader)
    if not capability["available"]:
        return run_single_yang_research()
    return {"status": "available", "reasons": [], "definition": SINGLE_YANG_DEFINITION}


@router.post("/factors/single-yang-no-break/evaluate")
def evaluate_single_yang_factor(body: SingleYangEvaluateIn, request: Request):
    try:
        return evaluate_single_yang(
            reader=getattr(
                getattr(request.app.state, "repo", None), "generation_pinned_daily_reader", None
            ),
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
            cost_bps=body.cost_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/macd-stages")
def get_macd_stages(request: Request):
    """返回 MACD 阶段研究能力。"""
    return macd_stages_availability(
        resolve_macd_reader(getattr(request.app.state, "repo", None))
    ).as_dict()


@router.post("/factors/macd-stages/evaluate")
def evaluate_macd_stages_factor(body: MacdStagesRequest, request: Request):
    try:
        return evaluate_macd_stages(
            resolve_macd_reader(getattr(request.app.state, "repo", None)),
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/factors/weak-to-strong/evaluate",
    response_model=WeakToStrongEvaluateResponse,
)
def evaluate_weak_to_strong(body: WeakToStrongEvaluateRequest, request: Request):
    """弱转强研究因子评估；production reader 由请求拥有并在 finally 关闭。"""
    from app.services.weak_to_strong_research_data import production_reader_scope

    with production_reader_scope(getattr(request.app.state, "repo", None), body.signal_date.year) as reader:
        return evaluate_weak_to_strong_v1(body, reader=reader)
