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
    series_names: str | None = Query(None, max_length=2000, description='自定义展示板块 JSON 数组, 如 ["A题材","B题材"]; 缺省=自动榜'),
    exclude_sectors: str | None = Query(None, max_length=4000, description='自动活跃榜排除板块 JSON 字符串数组; 缺省=内置属性板块名单, 传 [] = 清空名称过滤'),
    auto_rows: int | None = Query(None, ge=1, le=20, description="自动模式展示行数 (前N), 缺省 10"),
    sort_by: Literal["activity", "score", "pct", "rank_change", "momentum", "flow"] = Query(
        "activity",
        description="自动榜排序维度: activity=近30分钟成交额, score=综合分, pct=现涨幅, rank_change=1h排名跃升, momentum=近1小时动量(走强→走弱), flow=扩展资金流",
    ),
):
    """盘中板块切换走势: 全量分钟K聚合到板块, 涨幅 + 扩展资金流综合评分。

    series_names 提供时展示矩阵仅含这些板块 (自定义监控, ≤20, 当日无行情的剔除, 不过滤),
    缺省为自动榜 (sort_by 维度降序, 先剔除排除名单与超成员数上限的板块,
    不足时回退不过滤)。
    数据不可用时返回明确的 no_data/empty 状态与原因, 不静默。
    """
    names: list[str] | None = None
    if series_names:
        names = _parse_str_array(series_names, "series_names")
    excluded: list[str] | None = None
    if exclude_sectors is not None:
        excluded = _parse_str_array(exclude_sectors, "exclude_sectors")

    return sector_rotation.build_sector_rotation(
        request.app.state.repo,
        kind=kind,
        flow_field=flow,
        top=top,
        bucket_minutes=bucket,
        series_names=names,
        exclude_sectors=excluded,
        auto_rows=auto_rows,
        sort_by=sort_by,
    )


def _parse_str_array(raw: str, param_name: str) -> list[str]:
    """解析 JSON 字符串数组查询参数 (strip + 去空), 非法时报 400。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{param_name} 需为 JSON 字符串数组") from exc
    if not isinstance(parsed, list) or any(not isinstance(x, str) for x in parsed):
        raise HTTPException(status_code=400, detail=f"{param_name} 需为字符串数组")
    return [x for x in parsed if x.strip()]
