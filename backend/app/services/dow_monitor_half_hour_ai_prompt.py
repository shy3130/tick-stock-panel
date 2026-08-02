# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

from app.services.dow_monitor_half_hour_ai_models import (
    AnalysisScenario,
    HourlyStageReport,
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
    report: HourlyStageReport | None = None


class ParsedAiAnalysis(BaseModel):
    title: str
    summary: str
    conclusion: str
    evidence: list[ValidatedEvidence]
    risks: list[str]
    scenarios: list[AnalysisScenario]
    data_quality: list[str]
    report: HourlyStageReport | None = None


class HalfHourAiPromptService:
    def __init__(
        self,
        generate_text: Callable[..., Awaitable[str]] | None,
    ) -> None:
        self._generate_text = generate_text

    async def analyze(self, snapshot: HalfHourAiSnapshot) -> ParsedAiAnalysis:
        if self._generate_text is None:
            raise RuntimeError("AI provider is unavailable")
        messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一位高级盘中证券分析师。你的任务是解释本阶段发生了什么、"
                        "与上一阶段相比发生了什么变化、这些变化对持仓者和未参与者分别意味着什么。"
                        "不得只罗列VWAP、动量、量比、盘口等指标；必须解释分钟路径、日内累计结构、"
                        "通道、形态、量价资金推动或背离，以及下一阶段的确认、风险和失效条件。"
                        "只依据输入事实，不给保证性结论、仓位比例、下单指令或正式买卖信号。"
                        "所有数字必须由metric_key引用输入evidence_values，正文不得自行编造数值。"
                        "只返回JSON，并保留title、summary、conclusion、evidence、risks、scenarios、"
                        "data_quality字段，同时必须提供report字段。report严格匹配此JSON Schema："
                        + json.dumps(
                            HourlyStageReport.model_json_schema(),
                            ensure_ascii=False,
                        )
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        snapshot.model_dump(mode="json"), ensure_ascii=False
                    ),
                },
            ]
        raw = await self._generate_text(
            messages,
            temperature=0.2,
            max_tokens=3200,
        )
        try:
            return self.parse_and_validate(raw, snapshot, require_report=True)
        except InvalidAiAnalysis as initial_error:
            repaired = await self._generate_text(
                self._repair_messages(raw, snapshot, initial_error),
                temperature=0,
                max_tokens=3200,
            )
            try:
                return self.parse_and_validate(
                    repaired,
                    snapshot,
                    require_report=True,
                )
            except InvalidAiAnalysis as repair_error:
                raise InvalidAiAnalysis(
                    "AI output remained invalid after one repair attempt"
                ) from repair_error

    @staticmethod
    def _repair_messages(
        raw: str,
        snapshot: HalfHourAiSnapshot,
        error: InvalidAiAnalysis,
    ) -> list[dict[str, str]]:
        cause = error.__cause__
        validation_error = str(cause or error)[:2000]
        repair_input = {
            "validation_error": validation_error,
            "allowed_metric_keys": sorted(snapshot.evidence_values),
            "required_schema": _ModelOutput.model_json_schema(),
            "original_output": raw,
        }
        return [
            {
                "role": "system",
                "content": (
                    "你是结构化 JSON 修复器。只修复输入的格式、缺失字段、字段类型、"
                    "长度和 metric_key，使其严格符合 required_schema；不得添加输入中"
                    "不存在的行情事实或数字。只返回一个 JSON 对象，不要 Markdown、"
                    "解释、前后缀或代码围栏。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(repair_input, ensure_ascii=False),
            },
        ]

    def parse_and_validate(
        self,
        raw: str,
        snapshot: HalfHourAiSnapshot,
        *,
        require_report: bool = False,
    ) -> ParsedAiAnalysis:
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            parsed = _ModelOutput.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise InvalidAiAnalysis("AI output is not valid structured JSON") from exc
        if require_report and parsed.report is None:
            raise InvalidAiAnalysis("hourly stage report is required")
        if parsed.report is not None:
            self._validate_report(parsed.report, snapshot)
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
            report=parsed.report,
        )

    def _validate_report(
        self,
        report: HourlyStageReport,
        snapshot: HalfHourAiSnapshot,
    ) -> None:
        keys: list[str] = []
        for segment in report.stage_path:
            keys.extend(segment.metric_keys)
        keys.extend(report.channel.evidence_metric_keys)
        for pattern in report.patterns:
            keys.extend(pattern.evidence_metric_keys)
            keys.extend(pattern.invalidation_metric_keys)
        unknown = sorted(set(keys) - set(snapshot.evidence_values))
        if unknown:
            raise InvalidAiAnalysis(f"unknown evidence key: {unknown[0]}")

        narrative = " ".join(
            (
                report.headline.summary,
                report.comparison_with_previous,
                report.day_overview,
                report.channel.explanation,
                report.volume_capital_interpretation,
                report.holding_advice.advice,
                report.watching_advice.advice,
            )
        )
        meaning_tokens = (
            "推动",
            "背离",
            "修复",
            "承接",
            "抛压",
            "确认",
            "失效",
            "风险",
            "机会",
            "通道",
            "突破",
            "回踩",
            "转弱",
            "增强",
        )
        if not any(token in narrative for token in meaning_tokens):
            raise InvalidAiAnalysis("indicator narration lacks business interpretation")
