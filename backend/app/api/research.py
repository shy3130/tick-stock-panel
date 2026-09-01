from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.data_providers.fquant.daily_market_research import (
    PublishedDailyMarketFactsReader,
)

from app.services.macd_stages import (
    MacdStagesRequest,
    evaluate_macd_arms,
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
    SingleYangCompositeReader,
    evaluate_single_yang,
    evaluate_single_yang_increment,
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

from app.services.hold_firm_patterns import (
    CapabilityResult,
    HoldFirmPatternsRequest,
    HoldFirmResponse,
    HoldFirmStatus,
    ProductionReaderScopeUnavailable,
    assess_capability as assess_hold_firm_capability,
    evaluate_hold_firm_patterns as evaluate_hold_firm_patterns_v1,
    production_reader_scope as hold_firm_reader_scope,
)
from app.services.chip_peak_patterns import ChipPeakRequest, ChipPeakResponse
from app.services.doji_patterns import DojiPatternsRequest, DojiResponse
from app.services.escape_windows import (
    EscapeWindowsRequest,
    EscapeWindowsResponse,
)
from app.services.weekly_flagpole import (
    WeeklyFlagpoleRequest,
    WeeklyFlagpoleResponse,
)


from app.services.daily_open_anchor_filter import (
    assess_daily_open_anchor_capability,
    evaluate_daily_open_anchor,
    resolve_daily_open_anchor_canonical,
    unavailable_payload,
)
from app.services.daily_event_research import (
    DailyEventRequest,
    DailyEventResponse,
    UnavailabilityReason as DailyEventUnavailabilityReason,
    evaluate_daily_events,
)
from app.services.daily_event_research.escape_risk import SIGNAL_CAPABILITIES
from app.services.daily_event_research.models import (
    unavailable_response as unavailable_daily_event_response,
)
from app.services.daily_event_research.production import (
    evaluate_escape_risk_production,
    evaluate_pre_surge_production,
)
from app.services.research_sealed_data import PublishedCanonicalDailyReader
from app.services.retrieval_routing_research import (
    MAX_PLACEBO_ROUNDS,
    DEFAULT_FEATURE_IDS,
    MIN_PANEL_SYMBOLS,
    MIN_PLACEBO_ROUNDS,
    RetrievalRoutingRequest,
    RetrievalRoutingResponse,
    RoutingUnavailableReason,
    build_pinned_factor_panel,
    evaluate_retrieval_routing,
    unavailable_routing_response,
)
from app.services.negative_exclusion import (
    capability_report as negative_exclusion_capability_report,
)
from app.services.negative_exclusion_production import (
    evaluate_negative_exclusion_production,
)

router = APIRouter(prefix="/api/research", tags=["research"])


def _normalize_research_symbols(symbols: list[str]) -> list[str]:
    normalized = [symbol.strip().upper() for symbol in symbols]
    if any(re.fullmatch(r"^\d{6}\.(SH|SZ|BJ)$", symbol) is None for symbol in normalized):
        raise ValueError("symbols must be canonical A-share identifiers")
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be unique")
    return normalized


class PreSurgeEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    oos_start: date
    end: date
    benchmark_symbol: str = Field(
        default="000300.SH",
        pattern=r"^\d{6}\.(SH|SZ|BJ)$",
    )
    cost_bps: float = Field(default=10.0, ge=0.0, le=1000.0)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        return _normalize_research_symbols(symbols)

    @model_validator(mode="after")
    def validate_dates(self):
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class EscapeRiskEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    end: date
    oos_start: date
    cost_bps: float = Field(default=10.0, ge=0.0, le=1000.0)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        return _normalize_research_symbols(symbols)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if not self.start <= self.oos_start <= self.end:
            raise ValueError("oos_start must be within [start, end]")
        return self


class RetrievalRoutingEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=MIN_PANEL_SYMBOLS, max_length=200)
    start: date
    end: date
    label_horizon: int = Field(default=1, ge=1, le=20)
    cost_bps: float = Field(default=10.0, ge=0.0, le=1000.0)
    placebo_rounds: int = Field(
        default=200,
        ge=MIN_PLACEBO_ROUNDS,
        le=MAX_PLACEBO_ROUNDS,
    )
    feature_names: list[str] | None = None

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        return _normalize_research_symbols(symbols)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start >= self.end:
            raise ValueError("start must be < end")
        return self


class DailyOpenAnchorEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    oos_start: date
    symbols: list[str] = Field(min_length=1, max_length=200)


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


class NShapePullbackDepthEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    symbols: list[str] | None = Field(default=None, max_length=1000)
    reversal_mode: Literal["fixed_pct", "atr_multiple"] = "fixed_pct"
    reversal_value: float = Field(default=0.08, gt=0.0, le=10.0)
    cost_bps: float = Field(default=20.0, ge=0.0, le=1000.0)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str] | None) -> list[str] | None:
        return None if symbols is None else _normalize_research_symbols(symbols)

    @model_validator(mode="after")
    def validate_request(self):
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if self.reversal_mode == "fixed_pct" and self.reversal_value >= 1:
            raise ValueError("fixed_pct reversal_value must be < 1")
        return self


class NegativeExclusionEvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=200)
    start: date
    oos_start: date
    end: date
    enabled_classes: list[Literal["v2", "v4", "v5"]] | None = None
    horizon_days: int = Field(default=10, ge=1, le=60)
    cost_bps: float = Field(default=20.0, ge=0.0, le=1000.0)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        return _normalize_research_symbols(symbols)

    @field_validator("enabled_classes")
    @classmethod
    def unique_classes(
        cls, classes: list[Literal["v2", "v4", "v5"]] | None
    ) -> list[Literal["v2", "v4", "v5"]] | None:
        if classes is not None and len(classes) != len(set(classes)):
            raise ValueError("enabled_classes must be unique")
        return classes

    @model_validator(mode="after")
    def validate_dates(self):
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


@router.post("/factors/n-shape-pullback-depth/evaluate")
def evaluate_n_shape_pullback_depth_factor(
    body: NShapePullbackDepthEvaluateIn,
    request: Request,
):
    """Evaluate causal zigzag pullback buckets without terminal-pivot leakage."""
    from app.services.n_shape_pullback_depth import (
        evaluate_n_shape_pullback_depth,
        resolve_n_shape_reader,
    )

    reader = resolve_n_shape_reader(getattr(request.app.state, "repo", None))
    try:
        return evaluate_n_shape_pullback_depth(
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            reader=reader,
            reversal_mode=body.reversal_mode,
            reversal_value=body.reversal_value,
            cost_bps=body.cost_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


@router.get("/negative-exclusion")
def get_negative_exclusion_capability():
    """Expose V1/V3 gaps and the available V2/V4/V5 research classes."""
    return {
        "status": "partially_available",
        "classes": negative_exclusion_capability_report(),
        "promoted": False,
    }


@router.post("/factors/negative-exclusion/evaluate")
def evaluate_negative_exclusion_factor(
    body: NegativeExclusionEvaluateIn,
    request: Request,
):
    """Compare the pinned OOS pool before/after each available exclusion."""
    repo = getattr(request.app.state, "repo", None)
    try:
        with hold_firm_reader_scope(repo) as scope:
            return evaluate_negative_exclusion_production(
                symbols=body.symbols,
                start=body.start,
                oos_start=body.oos_start,
                end=body.end,
                canonical_reader=scope.canonical,
                market_facts_reader=scope.market_facts,
                universe_reader=scope.universe_reader,
                enabled_classes=body.enabled_classes,
                horizon_days=body.horizon_days,
                cost_bps=body.cost_bps,
            )
    except ProductionReaderScopeUnavailable as exc:
        return {
            "schema": "negative_exclusion_research/production/v1",
            "status": "unavailable",
            "reason": exc.reason.value,
            "detail": exc.detail,
            "capabilities": negative_exclusion_capability_report(),
            "promoted": False,
        }


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


@router.get("/daily-open-anchor")
def get_daily_open_anchor(request: Request):
    repo = getattr(request.app.state, "repo", None)
    canonical = resolve_daily_open_anchor_canonical(repo)
    return assess_daily_open_anchor_capability(canonical)


@router.post("/factors/daily-open-anchor/evaluate")
def evaluate_daily_open_anchor_factor(body: DailyOpenAnchorEvaluateIn, request: Request):
    repo = getattr(request.app.state, "repo", None)
    canonical = resolve_daily_open_anchor_canonical(repo)
    if canonical is None:
        return unavailable_payload(["canonical_reader_missing"])
    try:
        return evaluate_daily_open_anchor(
            canonical=canonical,
            start=body.start,
            end=body.end,
            oos_start=body.oos_start,
            symbols=body.symbols,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        from app.services.daily_open_anchor_filter import UnavailableError

        if isinstance(exc, UnavailableError):
            return unavailable_payload([exc.reason], exc.detail)
        raise HTTPException(status_code=503, detail="daily_open_anchor_reader_unavailable") from exc


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
        names = (
            [item.get("arm") for item in arms]
            if isinstance(arms, list) and all(isinstance(item, dict) for item in arms)
            else []
        )
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
        digest = (
            getattr(
                markets, "pin_manifest_sha256", getattr(markets, "manifest_sha256", lambda: "")
            )()
            if markets
            else ""
        )
        identity_fn = getattr(markets, "pin_identity_verified", None) if markets else None
        verified = identity_fn() if identity_fn is not None else bool(generation and digest)
        market_available = bool(generation and digest and verified)
        if not market_available:
            capability["available"] = False
            capability["status"] = "unavailable"
            capability.setdefault("reasons", []).append("markets_pin_identity_unverified")
        capability["markets"] = {
            "available": market_available,
            "generation": generation,
            "manifest_sha256": digest,
            "pin_verification_mode": getattr(markets, "pin_verification_mode", lambda: "legacy")()
            if markets
            else "missing",
        }
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
            reader,
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
            cost_bps=body.cost_bps,
            market_facts_reader=markets,
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
    markets = None
    try:
        reader = getattr(
            getattr(request.app.state, "repo", None),
            "generation_pinned_daily_reader",
            None,
        )
        response = evaluate_single_yang(
            reader=reader,
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
            cost_bps=body.cost_bps,
        )
        increment_reader = reader
        manifest = getattr(reader, "manifest", None)
        if callable(manifest):
            try:
                markets = PublishedDailyMarketFactsReader.from_canonical_manifest(manifest())
                increment_reader = SingleYangCompositeReader(reader, markets)
            except (OSError, RuntimeError, TypeError, ValueError):
                markets = None
        response["increment_research"] = evaluate_single_yang_increment(
            reader=increment_reader,
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
            cost_bps=body.cost_bps,
        )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        close = getattr(markets, "close", None)
        if callable(close):
            close()


@router.get("/macd-stages")
def get_macd_stages(request: Request):
    """返回 MACD 阶段研究能力。"""
    return macd_stages_availability(
        resolve_macd_reader(getattr(request.app.state, "repo", None))
    ).as_dict()


@router.post("/factors/macd-stages/evaluate")
def evaluate_macd_stages_factor(body: MacdStagesRequest, request: Request):
    try:
        reader = resolve_macd_reader(getattr(request.app.state, "repo", None))
        response = evaluate_macd_stages(
            reader,
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
        )
        response["arms_research"] = evaluate_macd_arms(
            reader,
            start=body.start,
            end=body.end,
            symbols=body.symbols,
            oos_start=body.oos_start,
        )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/factors/weak-to-strong/evaluate",
    response_model=WeakToStrongEvaluateResponse,
)
def evaluate_weak_to_strong(body: WeakToStrongEvaluateRequest, request: Request):
    """弱转强研究因子评估；production reader 由请求拥有并在 finally 关闭。"""
    from app.services.weak_to_strong_research_data import production_reader_scope

    with production_reader_scope(
        getattr(request.app.state, "repo", None), body.signal_date.year
    ) as reader:
        return evaluate_weak_to_strong_v1(body, reader=reader)


@router.get("/hold-firm-patterns", response_model=CapabilityResult)
def get_hold_firm_patterns_capability(request: Request):
    """Return pinned capability for the four hold-firm detectors."""
    repo = getattr(request.app.state, "repo", None)
    try:
        with hold_firm_reader_scope(repo) as scope:
            return assess_hold_firm_capability(
                scope.canonical, scope.market_facts, scope.universe_reader
            )
    except ProductionReaderScopeUnavailable as exc:
        detail = f"{exc.reason.value}: {exc.detail}" if exc.detail else exc.reason.value
        return CapabilityResult(status=HoldFirmStatus.UNAVAILABLE, problems=(detail,))


@router.post("/factors/hold-firm-patterns/evaluate", response_model=HoldFirmResponse)
def evaluate_hold_firm_patterns_factor(body: HoldFirmPatternsRequest, request: Request):
    """Validate and delegate; research I/O remains in the evaluator service."""
    repo = getattr(request.app.state, "repo", None)
    try:
        with hold_firm_reader_scope(repo) as scope:
            return evaluate_hold_firm_patterns_v1(
                body, scope.canonical, scope.market_facts, scope.universe_reader
            )
    except ProductionReaderScopeUnavailable as exc:
        return HoldFirmResponse(
            status=HoldFirmStatus.UNAVAILABLE,
            unavailable_reason=exc.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/doji-patterns", response_model=CapabilityResult)
def get_doji_patterns_capability(request: Request):
    """Return immutable-source capability for D1-D4 doji research."""
    from app.services.doji_patterns import (
        assess_doji_capability,
        production_reader_scope,
    )

    repo = getattr(request.app.state, "repo", None)
    try:
        with production_reader_scope(repo) as scope:
            return assess_doji_capability(
                scope.canonical, scope.market_facts, scope.universe_reader
            )
    except ProductionReaderScopeUnavailable as exc:
        detail = f"{exc.reason.value}: {exc.detail}" if exc.detail else exc.reason.value
        return CapabilityResult(status=HoldFirmStatus.UNAVAILABLE, problems=(detail,))
    except (AttributeError, TypeError, ValueError) as exc:
        return CapabilityResult(
            status=HoldFirmStatus.UNAVAILABLE,
            problems=(f"unavailable_canonical_reader: {exc}",),
        )


@router.post("/factors/doji-patterns/evaluate", response_model=DojiResponse)
def evaluate_doji_patterns_factor(body: DojiPatternsRequest, request: Request):
    """Evaluate four doji hypotheses over one pinned three-source scope."""
    from app.services.doji_patterns import (
        DojiStatus,
        evaluate_doji_patterns,
        production_reader_scope,
        UnavailabilityReason,
    )

    repo = getattr(request.app.state, "repo", None)
    try:
        with production_reader_scope(repo) as scope:
            return evaluate_doji_patterns(
                body, scope.canonical, scope.market_facts, scope.universe_reader
            )
    except ProductionReaderScopeUnavailable as exc:
        return DojiResponse(
            status=DojiStatus.UNAVAILABLE,
            unavailable_reason=exc.reason,
        )
    except AttributeError:
        return DojiResponse(
            status=DojiStatus.UNAVAILABLE,
            unavailable_reason=UnavailabilityReason.CANONICAL_READER,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post(
    "/factors/chip-peak-patterns/evaluate",
    response_model=ChipPeakResponse,
)
def evaluate_chip_peak_patterns_factor(
    body: ChipPeakRequest,
    request: Request,
):
    """Evaluate C1-C5 using the frozen local turnover-decay model."""
    from app.services.chip_peak_patterns import (
        ChipPeakResponse,
        ChipProductionScopeUnavailableError,
        ChipStatus,
        evaluate,
        UnavailabilityReason,
        production_reader_scope,
    )

    repo = getattr(request.app.state, "repo", None)
    try:
        with production_reader_scope(repo, body) as readers:
            return evaluate(body, readers=readers)
    except ChipProductionScopeUnavailableError as exc:
        return ChipPeakResponse(
            status=ChipStatus.UNAVAILABLE,
            unavailable_reason=exc.reason,
        )
    except AttributeError:
        return ChipPeakResponse(
            status=ChipStatus.UNAVAILABLE,
            unavailable_reason=UnavailabilityReason.CANONICAL_READER,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/weekly-flagpole")
def get_weekly_flagpole_capability(request: Request):
    """Expose sealed composite-reader capability for weekly flag research."""
    from app.services.weekly_flagpole import assess_capability, resolve_reader

    reader = resolve_reader(getattr(request.app.state, "repo", None))
    try:
        return assess_capability(reader)
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


@router.post(
    "/factors/weekly-flagpole/evaluate",
    response_model=WeeklyFlagpoleResponse,
)
def evaluate_weekly_flagpole_factor(
    body: WeeklyFlagpoleRequest,
    request: Request,
):
    """Evaluate F0-F5 without falling back from the pinned composite reader."""
    from app.services.weekly_flagpole import evaluate, resolve_reader

    repo = getattr(request.app.state, "repo", None)
    reader = resolve_reader(repo)
    index_reader = getattr(repo, "index_daily_research_reader", None)
    try:
        return evaluate(body, reader, index_reader)
    finally:
        for active_reader in (reader, index_reader):
            close = getattr(active_reader, "close", None)
            if callable(close):
                close()


@router.get("/escape-windows")
def get_escape_windows_capability(request: Request):
    """Report the four sealed legs needed by the calendar-effect study."""
    from app.services.escape_windows import assess_escape_windows_capability

    return assess_escape_windows_capability(
        getattr(request.app.state, "repo", None)
    )


@router.post(
    "/escape-windows/evaluate",
    response_model=EscapeWindowsResponse,
)
def evaluate_escape_windows_factor(
    body: EscapeWindowsRequest,
    request: Request,
):
    """Evaluate six calendar anchors with explicit per-leg coverage censors."""
    from app.services.escape_windows import evaluate_escape_windows

    repo = getattr(request.app.state, "repo", None)
    canonical = getattr(repo, "generation_pinned_daily_reader", None)
    calendar = getattr(repo, "versioned_exchange_calendar", None)
    presence = getattr(repo, "pit_presence_universe", None)
    index_reader = getattr(repo, "index_daily_research_reader", None)
    try:
        return evaluate_escape_windows(
            body,
            canonical_reader=canonical,
            calendar=calendar,
            presence_universe=presence,
            index_reader=index_reader,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        close = getattr(index_reader, "close", None)
        if callable(close):
            close()


@router.post(
    "/factors/dugu-trend/evaluate",
    response_model=DailyEventResponse,
)
def evaluate_dugu_trend_factor(body: DailyEventRequest, request: Request):
    """Evaluate the frozen multi-stage trend detector on pinned daily inputs."""
    repo = getattr(request.app.state, "repo", None)
    canonical = PublishedCanonicalDailyReader.from_repository(repo)
    if canonical is None:
        return unavailable_daily_event_response(
            body,
            DailyEventUnavailabilityReason.CANONICAL_READER,
        )
    try:
        facts = PublishedDailyMarketFactsReader.from_canonical_manifest(canonical.manifest())
    except (OSError, RuntimeError, TypeError, ValueError):
        return unavailable_daily_event_response(
            body,
            DailyEventUnavailabilityReason.MARKET_FACTS,
        )
    try:
        return evaluate_daily_events(body, canonical, facts)
    finally:
        facts.close()


@router.post("/factors/pre-surge-features/evaluate")
def evaluate_pre_surge_features_factor(body: PreSurgeEvaluateIn, request: Request):
    """Evaluate F1-F4 and their intersection with independent OOS verdicts."""
    repo = getattr(request.app.state, "repo", None)
    try:
        with hold_firm_reader_scope(repo) as scope:
            return evaluate_pre_surge_production(
                symbols=body.symbols,
                start=body.start,
                oos_start=body.oos_start,
                end=body.end,
                canonical_reader=scope.canonical,
                market_facts_reader=scope.market_facts,
                universe_reader=scope.universe_reader,
                benchmark_symbol=body.benchmark_symbol,
                cost_bps=body.cost_bps,
            )
    except ProductionReaderScopeUnavailable as exc:
        return {
            "schema": "daily_event_research/pre_surge/v1",
            "status": "unavailable",
            "reason": exc.reason.value,
            "detail": exc.detail,
            "promoted": False,
        }


@router.get("/escape-risk")
def get_escape_risk_capability():
    """Expose implemented detectors separately from request-time data gates."""
    return {
        "status": "available",
        "signals": dict(SIGNAL_CAPABILITIES),
        "runtime_requirements": {
            "s2_s7": "catalog_pinned_minutes_trans",
            "s10": "catalog_pinned_minutes_trans_and_pit_float_shares",
        },
        "promoted": False,
    }


@router.post("/factors/escape-risk/evaluate")
def evaluate_escape_risk_factor(body: EscapeRiskEvaluateIn, request: Request):
    """Evaluate S1-S10; request-time route/PIT gaps remain explicit censors."""
    repo = getattr(request.app.state, "repo", None)
    canonical = PublishedCanonicalDailyReader.from_repository(repo)
    if canonical is None:
        return {
            "schema": "daily_event_research/escape_risk/v1",
            "status": "unavailable",
            "reason": "unavailable_canonical_reader",
            "capabilities": dict(SIGNAL_CAPABILITIES),
            "promoted": False,
        }
    intraday_reader = None
    try:
        try:
            from app.data_providers.registry import get_active_provider_name, get_provider

            provider = get_provider(get_active_provider_name(capability="minute"))
            opener = getattr(provider, "open_escape_risk_intraday_reader", None)
            if callable(opener):
                market_days = canonical.market_days(
                    body.start - timedelta(days=30), body.end
                )
                intraday_reader = opener(canonical.manifest(), tuple(market_days))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            intraday_reader = None
        return evaluate_escape_risk_production(
            symbols=body.symbols,
            start=body.start,
            end=body.end,
            oos_start=body.oos_start,
            canonical_reader=canonical,
            intraday_reader=intraday_reader,
            cost_bps=body.cost_bps,
        )
    finally:
        close = getattr(intraday_reader, "close", None)
        if callable(close):
            close()


@router.post(
    "/factors/mera-routing/evaluate",
    response_model=RetrievalRoutingResponse,
)
def evaluate_mera_routing_factor(
    body: RetrievalRoutingEvaluateIn,
    request: Request,
):
    """Evaluate the leak-safe daily retrieval-routing proxy, not minute MERA."""
    routing_request = RetrievalRoutingRequest(
        label_horizon=body.label_horizon,
        cost_bps=body.cost_bps,
        placebo_rounds=body.placebo_rounds,
        feature_names=body.feature_names,
    )
    repo = getattr(request.app.state, "repo", None)
    canonical = PublishedCanonicalDailyReader.from_repository(repo)
    if canonical is None:
        return unavailable_routing_response(
            routing_request,
            RoutingUnavailableReason.PANEL_COVERAGE,
            "canonical history is not published",
        )
    try:
        panel = build_pinned_factor_panel(
            canonical,
            body.symbols,
            body.start,
            body.end,
            feature_ids=body.feature_names or DEFAULT_FEATURE_IDS,
            label_horizon=body.label_horizon,
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return unavailable_routing_response(
            routing_request,
            RoutingUnavailableReason.PANEL_COVERAGE,
            str(exc),
        )
    return evaluate_retrieval_routing(panel, routing_request)
