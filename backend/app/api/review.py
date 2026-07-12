"""复盘数据分区 API。

对齐 `../fquant` 的 `/api/review/{section}` 分区语义,按前端 Tab 懒加载。
AI 复盘报告走 `/api/market-recap/*`,与本模块互不重叠。

数据全部来自本地 DuckDB/parquet 的 enriched 面板(见 services/review_series)。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Query, Request

from app.services import review_hk, review_series

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("/emotion")
def emotion(
    request: Request,
    as_of: Optional[date] = None,
    days: int = Query(30, ge=2, le=review_series.MAX_TRADING_DAYS, description="回扫交易日数"),
) -> dict:
    """情绪周期 —— 近 N 日涨停/跌停/炸板/封板率/最高连板/成交额时序。"""
    return review_series.emotion_series(request.app.state.repo, as_of, days)


@router.get("/ladder")
def ladder(
    request: Request,
    as_of: Optional[date] = None,
    days: int = Query(20, ge=2, le=review_series.MAX_TRADING_DAYS, description="回扫交易日数"),
) -> dict:
    """连板天梯 —— 近 N 日板层分布 + 晋级率序列。"""
    return review_series.ladder_series(request.app.state.repo, as_of, days)


@router.get("/rotation")
def rotation(
    request: Request,
    as_of: Optional[date] = None,
    days: int = Query(10, ge=2, le=review_series.MAX_TRADING_DAYS, description="回扫交易日数"),
    top: int = Query(8, ge=3, le=20, description="题材行数"),
) -> dict:
    """题材轮动 —— 近 N 日 × Top 题材的涨停矩阵(依赖 ext_data 概念映射)。"""
    return review_series.theme_rotation(request.app.state.repo, as_of, days, top)


@router.get("/clues")
def clues(
    request: Request,
    as_of: Optional[date] = None,
    limit: int = Query(20, ge=5, le=100, description="每张清单条数"),
) -> dict:
    """风险与线索 —— 炸板池 / 跌停池 / 冲高回落 / 成交额榜 / 反包股。"""
    return review_series.review_clues(request.app.state.repo, as_of, limit)


# ================================================================
# 港股分区
#
# 港股无涨跌停制度(markets.py: has_price_limit=False),且 fstore 里港股的
# 概念 tags 为空、换手/高开低收列全 NULL —— 上面 A 股那四个分区在港股下要么
# 语义不存在、要么无数据。所以港股走自己这两个更薄的分区,而不是复用。
# 详见 services/review_hk 模块头。
# ================================================================

@router.get("/hk/breadth")
def hk_breadth(
    as_of: Optional[date] = None,
    days: int = Query(30, ge=2, le=review_series.MAX_TRADING_DAYS, description="回扫交易日数"),
) -> dict:
    """港股市场宽度 —— 近 N 日涨跌家数 / 成交额 / 平均涨幅时序。"""
    return review_hk.hk_breadth_series(as_of, days)


@router.get("/hk/movers")
def hk_movers(
    as_of: Optional[date] = None,
    limit: int = Query(20, ge=5, le=100, description="每张榜单条数"),
) -> dict:
    """港股涨跌榜 —— 涨幅榜 / 跌幅榜 / 成交额榜 + 板块分布 + 涨跌分布。"""
    return review_hk.hk_movers(as_of, limit)
