"""涨幅轮动矩阵 API。

供「概念分析 → 涨幅RPS轮动」对话框调用。返回最近 N 个交易日的概念涨幅
排名矩阵:每列(日期)各自把所有概念按当天涨幅从高到低排序。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import rps_rotation
from app.services.concept_rotation_analyzer import analyze_rotation_stream

router = APIRouter(prefix="/api/rps", tags=["rps"])


@router.get("/rotation")
def get_rotation(
    request: Request,
    days: int = Query(12, ge=7, le=30, description="最近 N 个交易日(7-30)"),
    kind: str = Query("concept", pattern="concept|industry", description="维度: concept 概念 / industry 行业"),
    level: int | None = Query(None, ge=1, le=3, description="行业层级(仅 kind=industry): 1/2/3 级"),
) -> dict:
    """维度涨幅轮动矩阵(概念或行业)。

    Returns:
        dates: 日期字符串列表(最新在最前)
        columns: {日期: [[成员名, 涨幅小数], ...]} 每列各自降序
        concept_count: 去重维度成员总数
    """
    return rps_rotation.build_rps_rotation(request.app.state.repo, days, kind, level)


class AnalyzeRequest(BaseModel):
    """AI 维度轮动分析请求(概念或行业)。"""
    days: int = 12   # 分析最近 N 个交易日
    focus: str = ""  # 用户追加的关注点
    kind: str = "concept"  # "concept" 概念 / "industry" 行业
    level: int | None = None  # 行业层级(1/2/3), 仅 kind=industry 有效


@router.post("/rotation-analyze")
async def analyze_rotation(request: Request, req: AnalyzeRequest):
    """AI 维度轮动分析 — NDJSON 流式返回。

    装配轮动矩阵信号 + 大盘背景 → 分析提示词 → 流式调用 LLM →
    逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    协议:
      {"type":"meta","days","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)
    days = max(7, min(30, req.days))
    kind = "industry" if req.kind == "industry" else "concept"
    level = req.level if (kind == "industry" and req.level in (1, 2, 3)) else None

    async def stream_gen():
        async for chunk in analyze_rotation_stream(
            repo, days, req.focus, quote_service, depth_service, kind, level,
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
