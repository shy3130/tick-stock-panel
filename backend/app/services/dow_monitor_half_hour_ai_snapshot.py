# ruff: noqa: RUF001

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.services.dow_monitor_hourly_ai_structure import (
    HourlyMarketStructure,
    PreviousStageContext,
    build_hourly_market_structure,
)


class HalfHourAiSnapshot(BaseModel):
    market: str
    symbol: str
    session_open: datetime
    stage_start: datetime
    window_end: datetime
    data_cutoff: datetime
    stage_trading_minutes: int
    observation_count: int
    range_start: datetime | None
    range_end: datetime | None
    first_price: float | None
    latest_price: float | None
    session_high: float | None
    session_low: float | None
    sufficient: bool
    data_quality: list[str] = Field(default_factory=list)
    evidence_values: dict[str, float] = Field(default_factory=dict)
    market_structure: HourlyMarketStructure
    previous_stage: PreviousStageContext | None = None


class HalfHourAiSnapshotBuilder:
    def __init__(self, *, minimum_observations: int = 5) -> None:
        self._minimum_observations = minimum_observations

    def build(
        self,
        *,
        market: str,
        symbol: str,
        session_open: datetime,
        window_end: datetime,
        data_cutoff: datetime,
        rows: list[dict[str, Any]],
        stage_start: datetime | None = None,
        previous_stage: PreviousStageContext | None = None,
    ) -> HalfHourAiSnapshot:
        effective_stage_start = stage_start or session_open
        filtered: list[tuple[datetime, dict[str, Any], float]] = []
        for row in rows:
            observed_at = _time(row.get("decision_minute") or row.get("observed_at"))
            price = _number(row.get("last_price") or row.get("minute_close"))
            if (
                observed_at is None
                or price is None
                or observed_at < session_open
                or observed_at > data_cutoff
            ):
                continue
            filtered.append((observed_at, row, price))
        filtered.sort(key=lambda item: item[0])
        market_structure = build_hourly_market_structure(
            minute_rows=rows,
            stage_start=effective_stage_start,
            data_cutoff=data_cutoff,
            previous_stage=previous_stage,
        )
        stage_times = {
            observed_at
            for observed_at, _row, _price in filtered
            if effective_stage_start <= observed_at <= data_cutoff
        }
        prices = [item[2] for item in filtered]
        evidence: dict[str, float] = {}
        if prices:
            evidence.update(
                {
                    "latest_price": prices[-1],
                    "session_high": max(prices),
                    "session_low": min(prices),
                    "session_change_pct": (prices[-1] / prices[0] - 1) * 100,
                }
            )
            latest = filtered[-1][1]
            for key in (
                "vwap_distance_pct",
                "momentum_1m_pct",
                "momentum_5m_pct",
                "momentum_15m_pct",
                "volume_ratio",
                "volume_speed",
                "active_buy_ratio",
                "depth_imbalance_pct",
                "atr14_pct",
            ):
                value = _number(latest.get(key))
                if value is not None:
                    evidence[key] = value
        evidence.update(market_structure.evidence_values)
        sufficient = len(stage_times) >= self._minimum_observations
        quality = [] if sufficient else [
            f"有效阶段分钟观察仅 {len(stage_times)} 条，至少需要 {self._minimum_observations} 条"
        ]
        if filtered and str(filtered[-1][1].get("data_quality") or "").upper() == "PARTIAL":
            quality.append("最新实时评估数据不完整")
        for reason in market_structure.data_quality:
            if reason not in quality and len(stage_times) < self._minimum_observations:
                quality.append(reason)
        return HalfHourAiSnapshot(
            market=market,
            symbol=symbol,
            session_open=session_open,
            stage_start=effective_stage_start,
            window_end=window_end,
            data_cutoff=data_cutoff,
            stage_trading_minutes=len(stage_times),
            observation_count=len(filtered),
            range_start=filtered[0][0] if filtered else None,
            range_end=filtered[-1][0] if filtered else None,
            first_price=prices[0] if prices else None,
            latest_price=prices[-1] if prices else None,
            session_high=max(prices) if prices else None,
            session_low=min(prices) if prices else None,
            sufficient=sufficient,
            data_quality=quality,
            evidence_values=evidence,
            market_structure=market_structure,
            previous_stage=previous_stage,
        )


def _time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
