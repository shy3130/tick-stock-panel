"""自选股截图 OCR 导入。

引擎通过 OcrProvider 抽象，默认 Tesseract；后续可换成 RapidOCR 等而不改 API。
"""
from __future__ import annotations

from app.services.watchlist_ocr.pipeline import ImportCandidate, import_watchlist_image

__all__ = ["ImportCandidate", "import_watchlist_image"]
