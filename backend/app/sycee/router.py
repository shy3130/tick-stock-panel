"""Single integration point for Sycee-owned API modules."""

from fastapi import APIRouter

from app.sycee.portfolio import router as portfolio_router
from app.sycee.research_ledger import router as research_ledger_router

router = APIRouter()
router.include_router(portfolio_router)
router.include_router(research_ledger_router)
