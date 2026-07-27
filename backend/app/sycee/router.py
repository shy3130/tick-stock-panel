"""Single integration point for Sycee-owned API modules."""

from fastapi import APIRouter

from app.sycee.data_backup import router as data_backup_router
from app.sycee.portfolio import router as portfolio_router
from app.sycee.portfolio_sell_alert import router as portfolio_sell_alert_router
from app.sycee.research_ledger import router as research_ledger_router
from app.sycee.research_sharing import public_router as research_public_router
from app.sycee.research_sharing import router as research_sharing_router
from app.sycee.strategy_guard import SyceeStrategyAuthoringGuardMiddleware
from app.sycee.strategy_security import strategy_authoring_requires_admin as requires_admin
from app.sycee.strategy_tracking import router as strategy_tracking_router
from app.sycee.trade_reviews import router as trade_reviews_router

__all__ = ["SyceeStrategyAuthoringGuardMiddleware", "requires_admin", "router"]

router = APIRouter()
router.include_router(data_backup_router)
router.include_router(portfolio_router)
router.include_router(portfolio_sell_alert_router)
router.include_router(research_ledger_router)
router.include_router(research_sharing_router)
router.include_router(research_public_router)
router.include_router(strategy_tracking_router)
router.include_router(trade_reviews_router)
