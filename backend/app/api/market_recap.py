"""AI 大盘复盘 API — 流式复盘 + 报告持久化。

路由前缀: /api/market-recap

端点:
  POST /analyze                AI 流式大盘复盘(NDJSON)
  GET  /reports                历史复盘列表
  POST /reports                保存一条复盘报告
  DELETE /reports/{report_id}  删除一条复盘报告
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import market_recap_reports
from app.services.ai_attempts import get_registry
from app.services.ai_structured import CancellationToken, new_attempt_id
from app.services.market_recap import recap_market_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-recap", tags=["market-recap"])


def _canonical_date(request: Request) -> date | None:
    """返回当前 provider 已确认、业务层可见的 enriched 最晚日期。"""
    try:
        _, value = request.app.state.repo.get_enriched_latest()
        return value
    except Exception:  # noqa: BLE001
        return None


def _validated_report_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"as_of 格式应为 YYYY-MM-DD,收到: {raw}",
        ) from exc


class AnalyzeRequest(BaseModel):
    """AI 大盘复盘请求。"""
    as_of: str | None = None  # 可选:复盘日期(YYYY-MM-DD),缺省取最新有数据日
    focus: str = ""           # 可选:用户追加的复盘关注点
    document_text: str = ""
    profile_id: str | None = None


@router.post("/analyze")
async def analyze_market(request: Request, req: AnalyzeRequest):
    """AI 大盘复盘 — NDJSON 流式返回。

    装配市场总览(指数/涨跌/连板/封板/板块/情绪雷达)→ 复盘提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    协议:
      {"type":"meta","as_of","emotion_score","emotion_label","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    from datetime import date as date_cls

    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)

    as_of = None
    if req.as_of:
        try:
            as_of = date_cls.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD,收到: {req.as_of}")
    canonical_date = _canonical_date(request)
    if as_of is not None and canonical_date is not None and as_of > canonical_date:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "unconfirmed_as_of",
                "as_of": str(as_of),
                "canonical_as_of": str(canonical_date),
            },
        )

    attempt_id = new_attempt_id()
    token = CancellationToken()

    async def stream_gen():
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("market recap stream task unavailable")
        get_registry().register(attempt_id=attempt_id, task=task, token=token)
        try:
            async for chunk in recap_market_stream(
                repo, quote_service, depth_service, as_of, req.focus,
                document_text=req.document_text, profile_id=req.profile_id,
                cancel_token=token,
            ):
                yield chunk + "\n"
        finally:
            get_registry().unregister(attempt_id)

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-AI-Attempt-ID": attempt_id,
        },
    )


# ================================================================
# 报告 CRUD(历史复盘持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 大盘复盘报告。"""
    as_of: str
    focus: str = ""
    content: str
    summary: str = ""
    emotion_score: int | None = None
    emotion_label: str = ""


@router.get("/reports")
def list_reports(request: Request):
    """仅返回不晚于 canonical 水位的历史复盘；超前报告保留在磁盘。"""
    canonical_date = _canonical_date(request)
    visible: list[dict] = []
    discarded: list[dict[str, str | None]] = []
    for report in market_recap_reports.list_reports():
        raw_as_of = report.get("as_of")
        try:
            report_date = date.fromisoformat(str(raw_as_of))
        except ValueError:
            discarded.append(
                {"id": report.get("id"), "as_of": str(raw_as_of)}
            )
            continue
        if canonical_date is not None and report_date > canonical_date:
            discarded.append(
                {"id": report.get("id"), "as_of": str(raw_as_of)}
            )
            continue
        visible.append(report)
    return {
        "reports": visible,
        "canonical_as_of": str(canonical_date) if canonical_date else None,
        "discarded_reports": discarded,
    }


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条复盘报告。"""
    report_date = _validated_report_date(req.as_of)
    canonical_date = _canonical_date(request)
    if canonical_date is not None and report_date > canonical_date:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "unconfirmed_as_of",
                "as_of": req.as_of,
                "canonical_as_of": str(canonical_date),
            },
        )
    report = market_recap_reports.save_report({
        "as_of": req.as_of,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "emotion_score": req.emotion_score,
        "emotion_label": req.emotion_label,
    })
    # 推送到飞书(可选): 与定时复盘共用同一开关 review_push_enabled 与 _maybe_push_review。
    # 内部 try/except 静默降级, 不影响归档返回值。
    from app.jobs.daily_pipeline import _maybe_push_review
    _maybe_push_review(req.content, {
        "as_of": req.as_of,
        "emotion_label": req.emotion_label,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条复盘报告。"""
    ok = market_recap_reports.delete_report(report_id)
    return {"ok": ok}
