from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Market = Literal["cn", "hk", "us"]
Timeframe = Literal["5m", "15m", "30m"]


class MinuteBar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None


class StableTimeframeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeframe: Timeframe
    bar_completion: str
    provisional: bool = False
    price_to_line_pct: float | None = None
    line_role: str | None = None
    volume_ratio_20: float | None = None
    bars: tuple[MinuteBar, ...] = ()


class FormalSignalReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Literal["BUY", "SELL", "RISK"]
    stage: str
    label: str
    triggered_at: datetime
    event_key: str


class MinuteResultContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Market
    market_day: date
    symbol: str
    display_symbol: str
    decision_minute: datetime
    source_bar_time: datetime
    backfill: bool
    minute_bar: MinuteBar
    last_price: float | None = None
    prev_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    bid_volumes: tuple[float, ...] | None = None
    ask_volumes: tuple[float, ...] | None = None
    capital_total_in: float | None = None
    capital_total_out: float | None = None
    capital_quality: str | None = None
    vwap_distance_pct: float | None = None
    states: dict[Timeframe, StableTimeframeState] = Field(default_factory=dict)
    decision_direction: Literal["BULLISH", "BEARISH", "RANGE"] | None = None
    dominant_timeframe: str | None = None
    confirmation_timeframes: tuple[str, ...] = ()
    formal_signal: FormalSignalReference | None = None
    source_timestamps: dict[str, datetime] = Field(default_factory=dict)
    updated_at: datetime

    @model_validator(mode="after")
    def reject_future_sources(self) -> MinuteResultContext:
        future = [
            name
            for name, timestamp in self.source_timestamps.items()
            if timestamp > self.decision_minute
        ]
        if future:
            raise ValueError(f"future source timestamp: {', '.join(sorted(future))}")
        return self


class DowMonitorMinuteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Market
    symbol: str
    display_symbol: str
    decision_minute: datetime
    source_bar_time: datetime
    calculation_version: str = "v1"
    backfill: bool
    last_price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    minute_open: float
    minute_high: float
    minute_low: float
    minute_close: float
    minute_volume: float
    minute_turnover: float | None = None
    channel: str | None = None
    control_distance_pct: float | None = None
    vwap_distance_pct: float | None = None
    momentum_1m_pct: float | None = None
    momentum_5m_pct: float | None = None
    momentum_15m_pct: float | None = None
    volume_ratio: float | None = None
    volume_speed: float | None = None
    active_buy_ratio: float | None = None
    depth_imbalance_pct: float | None = None
    distance_to_day_high_pct: float | None = None
    distance_to_day_low_pct: float | None = None
    atr14_pct: float | None = None
    confirmation_count: int | None = Field(default=None, ge=0, le=2)
    formal_signal_side: str | None = None
    formal_signal_stage: str | None = None
    formal_signal_label: str | None = None
    formal_signal_time: datetime | None = None
    formal_signal_event_key: str | None = None
    data_quality: Literal["COMPLETE", "PARTIAL"]
    missing_fields: tuple[str, ...]
    source_timestamps: dict[str, datetime]
    result_payload: dict
    updated_at: datetime
