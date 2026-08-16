"""Orchestrate one isolated, evidence-first stock research run."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.ai_provider import current_ai_model, current_ai_provider, generate_ai_text

from .models import china_now_iso, json_safe
from .skill import parse_plan, planner_prompt, sanitize_question, synthesis_prompt
from .store import ResearchRunStore, run_store
from .tools import collect_evidence

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[S(\d{1,3})\]")

Collector = Callable[..., list[dict]]
TextGenerator = Callable[..., Awaitable[str]]


class ResearchAgentHarness:
    """Run planner -> whitelisted evidence collectors -> cited synthesis.

    Provider credentials never leave the process boundary. The AI provider receives
    only the bounded evidence records constructed by ``collect_evidence``.
    """

    def __init__(
        self,
        app: Any,
        *,
        store: ResearchRunStore = run_store,
        collector: Collector = collect_evidence,
        text_generator: TextGenerator = generate_ai_text,
    ) -> None:
        self._app = app
        self._store = store
        self._collector = collector
        self._text_generator = text_generator

    async def run(self, run_id: str) -> dict | None:
        """Execute a queued run. Collection errors become evidence, not task errors."""
        run = self._store.claim(run_id)
        if run is None:
            return self._store.get(run_id)

        symbol = str(run.get("symbol") or "").strip().upper()
        name = str(run.get("name") or "").strip()
        question = sanitize_question(str(run.get("question") or ""))
        include_web_news = bool(run.get("include_web_news", True))
        planner_state = "fallback"
        try:
            planner_reply = await self._text_generator(
                [{
                    "role": "user",
                    "content": planner_prompt(
                        symbol=symbol,
                        name=name,
                        question=question,
                        include_web_news=include_web_news,
                    ),
                }],
                temperature=0.0,
                max_tokens=300,
                timeout=90.0,
            )
            selected_tools = parse_plan(
                planner_reply,
                include_web_news=include_web_news,
                full_scope=True,
            )
            planner_state = "completed"
        except Exception as error:  # The deterministic full-scope fallback is intentional.
            logger.info("research planner unavailable for %s: %s", symbol, type(error).__name__)
            selected_tools = parse_plan("", include_web_news=include_web_news, full_scope=True)

        plan = [{"tool": tool, "status": "scheduled"} for tool in selected_tools]
        self._store.update(
            run_id,
            status="collecting",
            stage="采集证据",
            progress=15,
            plan=plan,
            runtime={
                "provider": current_ai_provider(),
                "model": current_ai_model(),
                "planner": planner_state,
                "mode": "isolated_evidence_first",
            },
        )

        def on_progress(stage: str, index: int) -> None:
            total = max(1, len(selected_tools))
            progress = 15 + round(index / total * 60)
            self._store.update(
                run_id,
                status="collecting",
                stage=f"采集: {stage}",
                progress=min(progress, 75),
            )

        evidence_records: list[dict] = []
        try:
            evidence_records = await asyncio.to_thread(
                self._collector,
                self._app,
                symbol=symbol,
                name=name,
                tools=selected_tools,
                on_progress=on_progress,
            )
            collected = json_safe(evidence_records, max_depth=12)
            if not isinstance(collected, list):
                raise RuntimeError("invalid collector response")
            evidence_records = [record for record in collected if isinstance(record, dict)]
            for index, record in enumerate(evidence_records, start=1):
                record["citation"] = f"[S{index:02d}]"
            resolved_name = _resolved_name(name, evidence_records)
            self._store.update(
                run_id,
                status="analyzing",
                stage="生成带引用研究报告",
                progress=80,
                name=resolved_name,
                evidence=evidence_records,
            )

            answer = await self._text_generator(
                [{
                    "role": "user",
                    "content": synthesis_prompt(
                        symbol=symbol,
                        name=resolved_name,
                        question=question,
                        records=evidence_records,
                    ),
                }],
                temperature=0.2,
                max_tokens=5_000,
                timeout=300.0,
            )
            answer = (answer or "").strip()
            if not answer:
                raise RuntimeError("empty AI response")
            answer, invalid_citations = _normalize_citations(answer, evidence_records)
            return self._store.update(
                run_id,
                status="succeeded",
                stage="已完成",
                progress=100,
                answer=answer[:60_000],
                completed_at=china_now_iso(),
                runtime={
                    "provider": current_ai_provider(),
                    "model": current_ai_model(),
                    "planner": planner_state,
                    "mode": "isolated_evidence_first",
                    "invalid_citations_rewritten": invalid_citations,
                },
            )
        except Exception as error:
            logger.warning("research run %s stopped: %s", run_id, type(error).__name__)
            return self._store.update(
                run_id,
                status="failed",
                stage="研究未完成",
                progress=100,
                evidence=evidence_records,
                error=(
                    f"研究生成未完成({type(error).__name__})。"
                    "已保留已采集的证据,供人工复核或重新运行。"
                ),
                completed_at=china_now_iso(),
            )


def get_harness(app: Any) -> ResearchAgentHarness:
    """Reuse the harness per FastAPI application without shared run state."""
    harness = getattr(app.state, "research_agent_harness", None)
    if harness is None:
        harness = ResearchAgentHarness(app)
        app.state.research_agent_harness = harness
    return harness


def _resolved_name(fallback: str, records: list[dict]) -> str:
    for record in records:
        data = record.get("data") if isinstance(record, dict) else None
        if not isinstance(data, dict):
            continue
        candidate = str(data.get("name") or "").strip()
        if candidate:
            return candidate[:80]
    return fallback[:80]


def _normalize_citations(answer: str, records: list[dict]) -> tuple[str, list[str]]:
    """Prevent a model-only citation id from looking like a valid source card."""
    valid = {str(record.get("citation") or "") for record in records}
    invalid: list[str] = []

    def replace(match: re.Match[str]) -> str:
        citation = f"[S{int(match.group(1)):02d}]"
        if citation in valid:
            return citation
        invalid.append(citation)
        return "[引用无效]"

    return _CITATION_RE.sub(replace, answer), list(dict.fromkeys(invalid))
