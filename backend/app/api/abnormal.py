"""交易所口径近似异动监测 API — 只读总览。

契约:
  GET /api/abnormal/overview?status=&board=&direction=&hide_st=
  返回 {rows, warnings, provenance}; 数据缺失 fail-soft 返回空 rows +
  明确 warnings, 绝不伪造 0 值。无持久化、不触发任何外部行情拉取。
  检测行字段: symbol/name/board/is_st/window/direction/deviation_pct/
  threshold_pct/ratio/status/benchmark_symbol/benchmark_available。
  声明: 交易所规则近似监测, 非交易所公告。
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Query, Request

from app.services.abnormal_moves import build_overview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/abnormal", tags=["abnormal"])

_STATUS_VALUES = ("triggered", "edge", "watch", "normal")
_BOARD_VALUES = ("主板", "创业板", "科创板", "北交所")
_DIRECTION_VALUES = ("up", "down")


@router.get("/overview")
def overview(
    request: Request,
    status: str | None = Query(None, description="状态过滤: triggered/edge/watch/normal"),
    board: str | None = Query(None, description="板块过滤: 主板/创业板/科创板/北交所"),
    direction: str | None = Query(None, description="方向过滤: up/down"),
    hide_st: bool = Query(False, description="隐藏 ST/*ST (默认 false, 前端默认传 true)"),
    limit: int = Query(500, ge=1, le=2000, description="返回行数上限"),
    offset: int = Query(0, ge=0, description="分页偏移"),
) -> dict:
    repo = getattr(request.app.state, "repo", None)
    qs = getattr(request.app.state, "quote_service", None)
    if repo is None:
        return {
            "rows": [],
            "total": 0,
            "warnings": ["数据层未就绪, 异动总览不可用"],
            "provenance": {"as_of": date.today().isoformat(), "source": "unavailable"},
            "disclaimer": "交易所规则近似监测, 非交易所公告",
        }
    if status is not None and status not in _STATUS_VALUES:
        status = None
    if board is not None and board not in _BOARD_VALUES:
        board = None
    if direction is not None and direction not in _DIRECTION_VALUES:
        direction = None
    result = build_overview(
        repo,
        qs,
        status=status,
        board=board,
        direction=direction,
        hide_st=hide_st,
    )
    result["total"] = len(result["rows"])
    result["rows"] = result["rows"][offset : offset + limit]
    result["disclaimer"] = "交易所规则近似监测, 非交易所公告"
    return result
