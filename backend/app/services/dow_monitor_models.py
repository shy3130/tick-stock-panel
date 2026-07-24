from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MonitoredSymbol(BaseModel):
    symbol: str
    market: Literal["cn", "hk", "us"]
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


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
