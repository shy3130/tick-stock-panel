from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from json import JSONDecodeError
from typing import Annotated, Any, Literal

import httpx
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
)

from app.services.dow_monitor_bar_safety import sanitize_engine_bars


def _validate_iso_date_or_datetime(value: str) -> str:
    for parser in (datetime.fromisoformat, date.fromisoformat):
        try:
            parser(value)
            return value
        except ValueError:
            continue
    raise ValueError("must be an ISO date or datetime")


IsoDateOrDateTime = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_iso_date_or_datetime),
]


class DowEngineUnavailable(RuntimeError):  # noqa: N818 - public engine contract name
    """The authoritative Longbridge Dow engine could not provide a usable result."""


class _EngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DowBar(_EngineModel):
    index: int
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class DowLine(_EngineModel):
    id: str
    side: str
    role: str
    generation: int
    anchor_indexes: tuple[int, int] = Field(alias="anchorIndexes")
    anchor_times: tuple[str, str] = Field(alias="anchorTimes")
    anchor_prices: tuple[float, float] = Field(alias="anchorPrices")
    created_index: int = Field(alias="createdIndex")
    invalidated_index: int | None = Field(alias="invalidatedIndex")
    controls_signals: bool = Field(alias="controlsSignals")


class DowSignalDetail(_EngineModel):
    name: str
    value: Any


class DowSignalEvidence(_EngineModel):
    code: str
    detector: str
    side: str
    bar_index: int = Field(alias="barIndex")
    strength: str
    structure_id: str | None = Field(alias="structureId")
    details: tuple[DowSignalDetail, ...]


class DowSignal(_EngineModel):
    side: str
    bar_index: int = Field(alias="barIndex")
    bar_time: str = Field(alias="barTime")
    price: float
    reason: str
    confidence: str
    line_id: str | None = Field(alias="lineId")
    first_cross_index: int | None = Field(alias="firstCrossIndex")
    first_cross_time: str | None = Field(alias="firstCrossTime")
    volume_ratio: float | None = Field(alias="volumeRatio")
    pattern: str | None
    evidence: tuple[DowSignalEvidence, ...]


class DowSnapshot(_EngineModel):
    symbol: str
    timeframe: str
    bar_time: str
    bar_completion: str
    provisional: bool
    phase: str
    phase_code: str
    candle_pattern: str | None
    line_id: str | None
    line_role: str | None
    line_side: str | None
    line_anchor_times: tuple[str, ...]
    line_value: float | None
    price_to_line_pct: float | None
    sequence_count: int
    volume_ratio_20: float | None
    volume_confirmation: str
    action: str
    action_code: str
    reason_codes: tuple[str, ...]


class DowLongTermSnapshot(_EngineModel):
    symbol: str
    timeframe: str
    bar_time: IsoDateOrDateTime
    bar_completion: Literal["FINAL", "FORMING"]
    provisional: StrictBool
    trend_direction: Literal["UP", "DOWN", "RANGE", "UNKNOWN"]
    trend_name: str
    pattern_name: str
    operation: Literal["观察", "买入触发", "卖出触发", "持有", "无操作"]
    signal_stage: Literal["NONE", "WARNING", "TRIGGER", "CONFIRMED"]
    breakout_type: Literal["NONE", "TREND_LINE", "KEY_LEVEL", "DOUBLE_BREAKOUT", "RETEST"]
    line_id: str | None
    line_side: str | None
    line_status: str | None
    first_anchor_time: IsoDateOrDateTime | None
    first_anchor_price: float | None
    second_anchor_time: IsoDateOrDateTime | None
    second_anchor_price: float | None
    line_value: float | None
    key_level_type: str | None
    key_level_time: IsoDateOrDateTime | None
    key_level_price: float | None
    first_break_time: IsoDateOrDateTime | None
    recent_low_scale: Literal["PRIMARY"] | None
    recent_low_label: str | None
    recent_low_time: IsoDateOrDateTime | None
    recent_low_price: float | None
    recent_low_confirmed_time: IsoDateOrDateTime | None
    evidence_codes: tuple[str, ...]
    failure_reason: str | None


HeadShouldersStage = Literal[
    "FORMING",
    "WAIT_NECKLINE_BREAK",
    "BREAK_WATCH",
    "WICK_CROSS",
    "NECKLINE_BREAK_WEAK",
    "CONFIRMED",
    "RETEST_CONFIRMED",
    "FALSE_BREAKOUT",
    "FAILED",
]


class DowHeadShouldersPoint(_EngineModel):
    role: Literal[
        "LEFT_SHOULDER",
        "NECKLINE_1",
        "HEAD",
        "NECKLINE_2",
        "RIGHT_SHOULDER",
        "BREAKOUT",
    ]
    bar_index: int = Field(alias="barIndex")
    bar_time: IsoDateOrDateTime = Field(alias="barTime")
    confirmed_index: int = Field(alias="confirmedIndex")
    confirmed_time: IsoDateOrDateTime = Field(alias="confirmedTime")
    price: float


class DowHeadShouldersSignal(_EngineModel):
    family: Literal["HEAD_SHOULDERS"]
    pattern_id: str = Field(alias="patternId")
    side: Literal["BUY", "SELL"]
    stage: Literal["CONFIRMED", "RETEST_CONFIRMED"]
    bar_index: int = Field(alias="barIndex")
    bar_time: IsoDateOrDateTime = Field(alias="barTime")
    price: float


class DowHeadShouldersPoints(_EngineModel):
    left_shoulder: DowHeadShouldersPoint | None = Field(alias="leftShoulder")
    neckline_1: DowHeadShouldersPoint | None = Field(alias="neckline1")
    head: DowHeadShouldersPoint | None
    neckline_2: DowHeadShouldersPoint | None = Field(alias="neckline2")
    right_shoulder: DowHeadShouldersPoint | None = Field(alias="rightShoulder")
    breakout: DowHeadShouldersPoint | None


class DowHeadShouldersNeckline(_EngineModel):
    anchors: tuple[DowHeadShouldersPoint, DowHeadShouldersPoint]
    anchor_indexes: tuple[int, int] = Field(alias="anchorIndexes")
    anchor_times: tuple[IsoDateOrDateTime, IsoDateOrDateTime] = Field(alias="anchorTimes")
    anchor_prices: tuple[float, float] = Field(alias="anchorPrices")
    value: float
    trigger_index: int | None = Field(alias="triggerIndex")
    trigger_time: IsoDateOrDateTime | None = Field(alias="triggerTime")
    trigger_value: float | None = Field(alias="triggerValue")


class DowHeadShouldersVolume(_EngineModel):
    ratio: float | None
    required_ratio: float = Field(alias="requiredRatio")
    baseline: float | None
    trigger_index: int | None = Field(alias="triggerIndex")
    trigger_time: IsoDateOrDateTime | None = Field(alias="triggerTime")


class DowHeadShouldersInvalidation(_EngineModel):
    price: float | None


class DowHeadShouldersLifecycle(_EngineModel):
    created_index: int = Field(alias="createdIndex")
    last_updated_index: int = Field(alias="lastUpdatedIndex")
    evidence: tuple[str, ...]


class DowHeadShouldersPattern(_EngineModel):
    id: str
    type: Literal["BOTTOM", "TOP"]
    stage: HeadShouldersStage
    side: Literal["BUY", "SELL"] | None
    signal: DowHeadShouldersSignal | None
    points: DowHeadShouldersPoints
    neckline: DowHeadShouldersNeckline | None
    volume: DowHeadShouldersVolume | None
    invalidation: DowHeadShouldersInvalidation
    geometry_score: float = Field(alias="geometryScore")
    volume_score: float = Field(alias="volumeScore")
    context_score: float = Field(alias="contextScore")
    quality_score: float = Field(alias="qualityScore")
    evidence: tuple[str, ...]
    lifecycle: DowHeadShouldersLifecycle


class DowHeadShouldersPayload(_EngineModel):
    patterns: tuple[DowHeadShouldersPattern, ...]
    signals: tuple[DowHeadShouldersSignal, ...]


class DowEngineResult(_EngineModel):
    symbol: str
    timeframe: str
    snapshot: DowSnapshot
    bars: tuple[DowBar, ...]
    lines: tuple[DowLine, ...]
    signals: tuple[DowSignal, ...]
    long_term: DowLongTermSnapshot = Field(alias="longTerm")
    turning: dict[str, Any] | None = None
    head_shoulders: DowHeadShouldersPayload | None = Field(
        default=None,
        alias="headShoulders",
    )
    evaluated_at: datetime = Field(alias="evaluatedAt")


class LongbridgeDowClient:
    def __init__(
        self,
        endpoint: str,
        timeout_s: float = 20.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=endpoint.rstrip("/") + "/",
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LongbridgeDowClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        bars: Sequence[Mapping[str, object]],
        completion: str,
        as_of: datetime,
        *,
        timeout_s: float | None = None,
    ) -> DowEngineResult:
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "completion": completion,
            "asOf": as_of.isoformat(),
            "bars": sanitize_engine_bars(timeframe, bars),
        }
        try:
            if timeout_s is None:
                response = self._client.post("/api/dow-state/evaluate", json=payload)
            else:
                response = self._client.post(
                    "/api/dow-state/evaluate",
                    json=payload,
                    timeout=timeout_s,
                )
            response.raise_for_status()
            return DowEngineResult.model_validate(response.json())
        except (httpx.HTTPError, JSONDecodeError, ValidationError) as exc:
            raise DowEngineUnavailable(str(exc)) from exc
