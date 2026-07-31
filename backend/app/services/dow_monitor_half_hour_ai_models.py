from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

HalfHourAiStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "insufficient_data",
    "unavailable",
]


class ValidatedEvidence(BaseModel):
    metric_key: str
    label: str
    value: str
    meaning: str


class AnalysisScenario(BaseModel):
    condition: str
    implication: str
    invalidates_when: str


class HalfHourAiSummary(BaseModel):
    analysis_id: str | None = None
    market: Literal["cn", "hk", "us"]
    symbol: str
    trade_date: date
    window_end: datetime | None = None
    status: HalfHourAiStatus
    title: str | None = None
    summary: str | None = None
    updated_at: datetime


class HalfHourAiAnalysis(HalfHourAiSummary):
    data_cutoff: datetime
    model_name: str | None = None
    conclusion: str | None = None
    evidence: list[ValidatedEvidence] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scenarios: list[AnalysisScenario] = Field(default_factory=list)
    data_quality: list[str] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1, le=65535)
    error_code: str | None = None
    error_message: str | None = None


def analysis_id_for(
    market: str,
    symbol: str,
    trade_date: date,
    window_end: datetime,
) -> str:
    if window_end.tzinfo is None or window_end.utcoffset() is None:
        raise ValueError("window_end must be timezone-aware")
    logical_key = "|".join(
        (
            market.lower(),
            symbol.strip().upper(),
            trade_date.isoformat(),
            window_end.astimezone(UTC).isoformat(),
        )
    )
    return hashlib.sha256(logical_key.encode("utf-8")).hexdigest()
