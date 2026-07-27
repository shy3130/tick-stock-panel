"""Single integration point for Sycee-owned API modules."""

from fastapi import APIRouter

from app.sycee.portfolio import router as portfolio_router
from app.sycee.research_ledger import router as research_ledger_router
from app.sycee.strategy_guard import SyceeStrategyAuthoringGuardMiddleware
from app.sycee.strategy_security import strategy_authoring_requires_admin as requires_admin

__all__ = ["SyceeStrategyAuthoringGuardMiddleware", "requires_admin", "router"]

router = APIRouter()
router.include_router(portfolio_router)
router.include_router(research_ledger_router)
