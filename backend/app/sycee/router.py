"""Single integration point for Sycee-owned API modules."""

from fastapi import APIRouter

from app.sycee.research_ledger import router as research_ledger_router

router = APIRouter()
router.include_router(research_ledger_router)
