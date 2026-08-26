"""市场抱团/拥挤度状态 API — 做T适用性研究的市场轴。

语义分离(与其它数据 API 一致, 绝不吞错):
  - 503: canonical 日线或 PIT 行业快照读取失败(脱敏, 不含本地路径);
  - 422: as_of 非法格式(FastAPI 日期解析);
  - 200 + state='unavailable': 可计算但覆盖/平滑窗口/历史校准不满足。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.services.market_concentration import (
    MarketStateDataError,
    market_state_for_date,
)

router = APIRouter(prefix="/api/research/t-suitability", tags=["market-state"])


@router.get("/market-state")
def get_market_state(request: Request, as_of: date | None = None):
    """返回 MarketStateSnapshot(无包装层); as_of 缺省 = canonical 最新交易日。"""
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="market data repository unavailable")
    try:
        snapshot = market_state_for_date(repo, as_of)
    except MarketStateDataError as exc:
        raise HTTPException(status_code=503, detail="market state data unavailable") from exc
    return JSONResponse(content=snapshot.model_dump(mode="json"))
