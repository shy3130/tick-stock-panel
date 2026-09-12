"""板块切换(盘中轮动) API — 基于全量分钟数据 + 扩展资金流。薄层, 计算在 services。"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import sector_rotation

router = APIRouter(prefix="/api/sector-rotation", tags=["sector-rotation"])


@router.get("")
def get_sector_rotation(
    request: Request,
    kind: Literal["concept", "industry"] = Query("concept", description="板块维度: 概念/行业 二选一"),
    flow: str | None = Query(None, max_length=200, description="资金流扩展列 (表id.列名), 缺省纯涨幅"),
    top: int = Query(30, ge=5, le=100, description="返回板块数上限"),
    bucket: int = Query(5, description="分钟桶粒度 (1/5/15)"),
    series_names: str | None = Query(None, max_length=2000, description='自定义展示板块 JSON 数组, 如 ["A题材","B题材"]; 缺省=活跃度 Top10'),
):
    """盘中板块切换走势: 全量分钟K聚合到板块, 涨幅 + 扩展资金流综合评分。

    series_names 提供时展示矩阵仅含这些板块 (自定义监控, ≤20, 当日无行情的剔除),
    缺省为活跃度 Top10 (近 30 分钟成分股成交额合计, 量额列缺失退化 score 前 10)。
    数据不可用时返回明确的 no_data/empty 状态与原因, 不静默。
    """
    names: list[str] | None = None
    if series_names:
        try:
            parsed = json.loads(series_names)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="series_names 需为 JSON 字符串数组") from exc
        if not isinstance(parsed, list) or any(not isinstance(x, str) for x in parsed):
            raise HTTPException(status_code=400, detail="series_names 需为字符串数组")
        names = [x for x in parsed if x.strip()]

    return sector_rotation.build_sector_rotation(
        request.app.state.repo,
        kind=kind,
        flow_field=flow,
        top=top,
        bucket_minutes=bucket,
        series_names=names,
    )
