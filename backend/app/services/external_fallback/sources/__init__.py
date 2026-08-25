"""Fallback source adapters (P1: tencent_quote only)."""
from __future__ import annotations

from app.services.external_fallback.sources.tencent_quote import TencentQuoteSource

__all__ = ["TencentQuoteSource"]
