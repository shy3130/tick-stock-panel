"""板块切换(盘中轮动) API — 基于全量分钟数据 + 扩展资金流。薄层, 计算在 services。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request

from app.services import sector_rotation

router = APIRouter(prefix="/api/sector-rotation", tags=["sector-rotation"])


@router.get("")
def get_sector_rotation(
    request: Request,
    kind: Literal["concept", "industry"] = Query("concept", description="板块维度: 概念/行业 二选一"),
    flow: str | None = Query(None, max_length=200, description="资金流扩展列 (表id.列名), 缺省纯涨幅"),
    top: int = Query(30, ge=5, le=100, description="返回板块数上限"),
    bucket: int = Query(5, description="分钟桶粒度 (1/5/15)"),
):
    """盘中板块切换走势: 全市场分钟K聚合到板块, 涨幅 + 扩展资金流综合评分。

    数据不可用时返回明确的 no_data/empty 状态与原因, 不静默。
    """
    return sector_rotation.build_sector_rotation(
        request.app.state.repo,
        kind=kind,
        flow_field=flow,
        top=top,
        bucket_minutes=bucket,
    )
