"""自选截图 OCR：代码抽取与 instruments 匹配（不依赖本机 tesseract）。"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import polars as pl
import pytest
from PIL import Image

from app.services.watchlist_ocr.pipeline import (
    extract_codes,
    import_watchlist_image,
    resolve_candidates,
)
from app.services.watchlist_ocr.provider import OcrProvider, preprocess_for_ocr


class _FakeOcr(OcrProvider):
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text

    def available(self) -> bool:
        return True

    def extract_text(self, image_bytes: bytes) -> str:
        return self._text


def _png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def test_extract_codes_order_and_dedupe():
    text = "563230 融\n515880\n价格 1.340\n563230 重复\nXAUUSD\n601636"
    assert extract_codes(text) == ["563230", "515880", "601636"]


def test_extract_codes_joins_ocr_split():
    text = "科创半导体\n5881 70 [融]\n创业板\n159382"
    assert extract_codes(text) == ["588170", "159382"]


def test_resolve_candidates_matched_and_unmatched():
    code_to_symbol = {"600036": "600036.SH", "515880": "515880.SH"}
    symbol_to_name = {"600036.SH": "招商银行", "515880.SH": "通信ETF国泰"}
    rows = resolve_candidates(
        ["600036", "999999", "515880"],
        code_to_symbol,
        symbol_to_name,
        existing_symbols={"600036.SH"},
    )
    assert rows[0].matched and rows[0].already_in_watchlist
    assert rows[0].name == "招商银行"
    assert not rows[1].matched and rows[1].symbol is None
    assert rows[2].matched and not rows[2].already_in_watchlist


def test_import_watchlist_image_with_fake_ocr(tmp_path: Path):
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame(
        {
            "code": ["600036", "515880"],
            "symbol": ["600036.SH", "515880.SH"],
            "name": ["招商银行", "通信ETF国泰"],
        }
    ).write_parquet(inst / "instruments.parquet")

    fake_text = "招商银行\n600036\n通信ETF\n515880\n伦敦金 XAUUSD"
    result = import_watchlist_image(
        b"fake-bytes",
        tmp_path,
        existing_symbols=set(),
        provider=_FakeOcr(fake_text),
    )
    assert result["provider"] == "fake"
    assert result["codes"] == ["600036", "515880"]
    assert result["matched_count"] == 2
    assert result["unmatched_count"] == 0


def test_preprocess_rejects_excessive_pixels():
    # 4000×8000 = 32M 像素 > 12M 上限
    with pytest.raises(ValueError, match="分辨率过高"):
        preprocess_for_ocr(_png_bytes(4000, 8000))


def test_preprocess_downsamples_large_edge():
    # 2500×1000 = 2.5M 像素未超限，但长边 > 2000，应降采样
    out = preprocess_for_ocr(_png_bytes(2500, 1000))
    assert max(out.size) <= 2000
