from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MonitoredSymbol(BaseModel):
    symbol: str
    market: Literal["cn", "hk", "us"]
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


class HistoryBackfillStatus(BaseModel):
    status: Literal[
        "pending",
        "queued",
        "running",
        "rebuilding",
        "completed",
        "partial",
        "failed",
        "unknown",
    ]
    progress: int = Field(default=0, ge=0, le=100)
    missing_timeframes: tuple[str, ...] = ()
    last_error: str | None = None
    updated_at: datetime | None = None


class DowTimeframeState(BaseModel):
    symbol: str
    market: Literal["cn", "hk", "us"]
    timeframe: Literal["5m", "15m", "30m", "60m", "day"]
    freshness_state: Literal["LIVE", "STALE_DATA", "ANALYSIS_PAUSED"]
    source_timestamp: datetime | None
    snapshot: dict
    chart: dict
    updated_at: datetime


class DowNotification(BaseModel):
    notification_id: str
    event_key: str
    symbol: str
    market: Literal["cn", "hk", "us"]
    timeframe: str
    side: Literal["BUY", "SELL", "RISK"]
    action_name: str
    shape_name: str
    triggered_at: datetime
    trigger_price: float
    snapshot_payload: dict
    read_at: datetime | None = None


class DowMinuteDecision(BaseModel):
    symbol: str
    market: Literal["cn", "hk", "us"]
    decision_minute: datetime
    direction: Literal["BULLISH", "BEARISH", "RANGE"]
    direction_label: Literal["偏涨", "偏跌", "震荡"]
    action: Literal["WATCH_BUY", "HOLD", "REDUCE_SELL", "OBSERVE"]
    action_label: Literal["买入观察", "持有", "减仓/卖出", "继续观察"]
    confidence: int = Field(ge=0, le=100)
    dominant_timeframe: Literal["5m", "15m", "30m", "60m", "day"] | None
    confirmation_timeframes: tuple[Literal["5m", "15m", "30m", "60m", "day"], ...] = ()
    supporting_reasons: tuple[str, ...] = ()
    contrary_risks: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    data_status: Literal[
        "COMPLETE",
        "WAITING_NEW_MINUTE",
        "DELAYED",
        "CAPITAL_UNCONFIRMED",
        "CAPITAL_UNAVAILABLE",
        "CAPITAL_DELAYED",
        "CAPITAL_INSUFFICIENT",
        "MARKET_CLOSED",
        "INSUFFICIENT_STRUCTURE",
    ]
    status_label: str
    source_timestamp: datetime | None = None

    @field_validator("decision_minute")
    @classmethod
    def require_timezone_aware_decision_minute(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_minute must be timezone-aware")
        return value
