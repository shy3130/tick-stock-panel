"""AI 大盘复盘 API — 流式复盘 + 报告持久化 + 龙虎榜 + 盘前风向标。

路由前缀: /api/market-recap

端点:
  POST /analyze                AI 流式大盘复盘(NDJSON)
  GET  /reports                历史复盘列表
  POST /reports                保存一条复盘报告
  DELETE /reports/{report_id}  删除一条复盘报告
  GET  /dragon-tiger           龙虎榜三榜 (fuyao 专有, 历史按日缓存)
  GET  /auction-benchmark      盘前风向标 (fuyao 专有, 含当日/次日真实收益)
"""
from __future__ import annotations

import logging
from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import auction_benchmark, dragon_tiger, market_recap_reports, preferences
from app.services.market_recap import recap_market_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-recap", tags=["market-recap"])


class AnalyzeRequest(BaseModel):
    """AI 大盘复盘请求。"""
    as_of: str | None = None  # 可选:复盘日期(YYYY-MM-DD),缺省取最新有数据日
    focus: str = ""           # 可选:用户追加的复盘关注点


@router.get("/dragon-tiger")
def get_dragon_tiger(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近已发布交易日"),
):
    """龙虎榜三榜 (全部/机构/游资)。fuyao 专有, 未配置时 state=source_unavailable。

    非交易日/当日未发布由服务层自动回退到上一交易日 (state=fallback_prev)。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return dragon_tiger.get_dragon_tiger(
        request.app.state.repo.store.data_dir, target
    )


@router.get("/auction-benchmark")
def get_auction_benchmark(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近交易日"),
):
    """盘前风向标 (同花顺竞价筛选名单 + 当日/次日真实收益)。

    fuyao 专有, 未配置时 state=source_unavailable; 非交易日由服务层自动回退。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return auction_benchmark.get_auction_benchmark(
        request.app.state.repo.store.data_dir, target
    )


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

    async def stream_gen():
        async for chunk in recap_market_stream(repo, quote_service, depth_service, as_of, req.focus):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    """获取全部历史复盘(按时间降序,后端已裁剪到上限)。"""
    return {"reports": market_recap_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest, push: bool = False):
    """保存一条复盘报告。push=True 或 review_push_mode=auto 时才推送到外部渠道。"""
    report = market_recap_reports.save_report({
        "as_of": req.as_of,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "emotion_score": req.emotion_score,
        "emotion_label": req.emotion_label,
    })
    # 推送门控: manual 模式需显式 push=True; auto 模式保持归档即推。
    # 内部 try/except 静默降级, 不影响归档返回值。
    if push or preferences.get_review_push_mode() == "auto":
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
