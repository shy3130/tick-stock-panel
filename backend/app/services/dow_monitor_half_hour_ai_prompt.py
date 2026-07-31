# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

from app.services.dow_monitor_half_hour_ai_models import (
    AnalysisScenario,
    ValidatedEvidence,
)
from app.services.dow_monitor_half_hour_ai_snapshot import HalfHourAiSnapshot

LABELS = {
    "latest_price": ("现价", ""),
    "session_high": ("日高", ""),
    "session_low": ("日低", ""),
    "session_change_pct": ("盘中累计涨跌", "%"),
    "vwap_distance_pct": ("VWAP偏离", "%"),
    "momentum_1m_pct": ("1分钟动量", "%"),
    "momentum_5m_pct": ("5分钟动量", "%"),
    "momentum_15m_pct": ("15分钟动量", "%"),
    "volume_ratio": ("同时段量比", "×"),
    "volume_speed": ("量能加速度", "%"),
    "active_buy_ratio": ("主动买入占比", "%"),
    "depth_imbalance_pct": ("五档不平衡", "%"),
    "atr14_pct": ("ATR", "%"),
}


class InvalidAiAnalysis(ValueError):  # noqa: N818
    pass


class _EvidenceClaim(BaseModel):
    metric_key: str
    meaning: str


class _ModelOutput(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=120)
    conclusion: str = Field(min_length=1, max_length=2000)
    evidence: list[_EvidenceClaim] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(min_length=1, max_length=8)
    scenarios: list[AnalysisScenario] = Field(default_factory=list, max_length=6)
    data_quality: list[str] = Field(min_length=1, max_length=8)


class ParsedAiAnalysis(BaseModel):
    title: str
    summary: str
    conclusion: str
    evidence: list[ValidatedEvidence]
    risks: list[str]
    scenarios: list[AnalysisScenario]
    data_quality: list[str]


class HalfHourAiPromptService:
    def __init__(
        self,
        generate_text: Callable[..., Awaitable[str]] | None,
    ) -> None:
        self._generate_text = generate_text

    async def analyze(self, snapshot: HalfHourAiSnapshot) -> ParsedAiAnalysis:
        if self._generate_text is None:
            raise RuntimeError("AI provider is unavailable")
        raw = await self._generate_text(
            [
                {
                    "role": "system",
                    "content": (
                        "你是盘中结构分析助手。只依据输入证据，不给保证性结论或直接交易指令；"
                        "区分观察、推断、场景和失效条件；必须说明风险与数据质量；只返回JSON。"
                        "输出必须严格使用："
                        '{"title":"短标题","summary":"不超过80字","conclusion":"综合分析",'
                        '"evidence":[{"metric_key":"输入中存在的evidence_values键",'
                        '"meaning":"为什么重要"}],"risks":["风险"],'
                        '"scenarios":[{"condition":"条件","implication":"可能含义",'
                        '"invalidates_when":"失效条件"}],"data_quality":["质量说明"]}。'
                        "不得自行填写证据数值，数值由后端根据metric_key渲染。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        snapshot.model_dump(mode="json"), ensure_ascii=False
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=1800,
        )
        return self.parse_and_validate(raw, snapshot)

    def parse_and_validate(
        self,
        raw: str,
        snapshot: HalfHourAiSnapshot,
    ) -> ParsedAiAnalysis:
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            parsed = _ModelOutput.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise InvalidAiAnalysis("AI output is not valid structured JSON") from exc
        evidence = []
        for claim in parsed.evidence:
            if claim.metric_key not in snapshot.evidence_values:
                raise InvalidAiAnalysis(
                    f"unknown evidence key: {claim.metric_key}"
                )
            label, unit = LABELS.get(
                claim.metric_key,
                (claim.metric_key, ""),
            )
            value = snapshot.evidence_values[claim.metric_key]
            evidence.append(
                ValidatedEvidence(
                    metric_key=claim.metric_key,
                    label=label,
                    value=f"{value:.2f}{unit}",
                    meaning=claim.meaning,
                )
            )
        return ParsedAiAnalysis(
            title=parsed.title,
            summary=parsed.summary,
            conclusion=parsed.conclusion,
            evidence=evidence,
            risks=parsed.risks,
            scenarios=parsed.scenarios,
            data_quality=parsed.data_quality,
        )
