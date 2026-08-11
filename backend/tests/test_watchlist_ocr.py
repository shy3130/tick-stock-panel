"""自选截图 OCR：代码抽取、预处理、API 门禁与并发限制（不依赖本机 tesseract）。"""
from __future__ import annotations

import threading
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import anyio
import polars as pl
import pytest
from fastapi import HTTPException
from PIL import Image

from app.api import watchlist as watchlist_api
from app.api.watchlist import BatchAddRequest, add_batch, import_from_image, ocr_status
from app.config import settings
from app.services import watchlist
from app.services.watchlist_ocr.pipeline import (
    extract_codes,
    import_watchlist_image,
    resolve_candidates,
)
from app.services.watchlist_ocr.provider import OcrProvider, preprocess_for_ocr


class _FakeOcr(OcrProvider):
    name = "fake"

    def __init__(self, text: str, *, available: bool = True) -> None:
        self._text = text
        self._available = available

    def available(self) -> bool:
        return self._available

    def extract_text(self, image_bytes: bytes) -> str:
        return self._text


def _png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def _mock_upload(
    *,
    content: bytes = b"img",
    content_type: str = "image/png",
    filename: str = "shot.png",
) -> MagicMock:
    file = MagicMock()
    file.content_type = content_type
    file.filename = filename
    file.read = AsyncMock(return_value=content)
    return file


def _mock_request(data_dir: Path) -> MagicMock:
    request = MagicMock()
    request.app.state.repo.store.data_dir = data_dir
    request.app.state.repo.get_name_map = lambda _symbols: {}
    return request


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


def test_import_unavailable_mentions_windows():
    with pytest.raises(RuntimeError, match="Windows|choco|UB Mannheim"):
        import_watchlist_image(
            b"x",
            Path("/tmp"),
            provider=_FakeOcr("", available=False),
        )


def test_preprocess_rejects_excessive_pixels_before_load(monkeypatch):
    """超限图应在完整解码前拒绝，避免 load() 造成 OOM。"""
    loaded = {"called": False}
    real_open = Image.open

    def open_tracking(fp, *args, **kwargs):
        img = real_open(fp, *args, **kwargs)
        original_load = img.load

        def load_tracking(*a, **k):
            loaded["called"] = True
            return original_load(*a, **k)

        img.load = load_tracking  # type: ignore[method-assign]
        return img

    monkeypatch.setattr(Image, "open", open_tracking)
    # 4000×8000 = 32M 像素 > 12M 上限
    with pytest.raises(ValueError, match="分辨率过高"):
        preprocess_for_ocr(_png_bytes(4000, 8000))
    assert loaded["called"] is False


def test_preprocess_maps_decompression_bomb_to_value_error(monkeypatch):
    class _BombImg:
        width = 100
        height = 100

        def load(self):
            raise Image.DecompressionBombError("bomb")

    monkeypatch.setattr(Image, "open", lambda *_a, **_k: _BombImg())
    with pytest.raises(ValueError, match="分辨率过高"):
        preprocess_for_ocr(b"fake")


def test_preprocess_downsamples_large_edge():
    # 2500×1000 = 2.5M 像素未超限，但长边 > 2000，应降采样
    out = preprocess_for_ocr(_png_bytes(2500, 1000))
    assert max(out.size) <= 2000


def test_ocr_status_reflects_provider(monkeypatch):
    monkeypatch.setattr(
        watchlist_api,
        "get_ocr_provider",
        lambda: _FakeOcr("", available=True),
    )
    assert ocr_status() == {"provider": "fake", "available": True}

    monkeypatch.setattr(
        watchlist_api,
        "get_ocr_provider",
        lambda: _FakeOcr("", available=False),
    )
    assert ocr_status() == {"provider": "fake", "available": False}


@pytest.mark.asyncio
async def test_import_image_rejects_bad_mime(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = _mock_upload(content_type="image/svg+xml", filename="x.svg")
    with pytest.raises(HTTPException) as ei:
        await import_from_image(request, file)
    assert ei.value.status_code == 400
    assert "仅支持" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_image_rejects_oversized_bytes(tmp_path: Path):
    request = _mock_request(tmp_path)
    huge = b"x" * (12 * 1024 * 1024 + 1)
    file = _mock_upload(content=huge)
    with pytest.raises(HTTPException) as ei:
        await import_from_image(request, file)
    assert ei.value.status_code == 400
    assert "过大" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_image_maps_value_error_to_400(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])

    def boom(*_a, **_k):
        raise ValueError("图片分辨率过高,请裁剪后重试")

    monkeypatch.setattr(watchlist_api, "import_watchlist_image", boom)
    file = _mock_upload(content=_png_bytes(64, 64))
    with pytest.raises(HTTPException) as ei:
        await import_from_image(request, file)
    assert ei.value.status_code == 400
    assert "分辨率过高" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_ocr_limiter_caps_concurrency(tmp_path: Path, monkeypatch):
    """第三路 OCR 应排队，同时进入同步 OCR 的不超过 2。"""
    current = 0
    max_seen = 0
    lock = threading.Lock()

    def slow_import(*_a, **_k):
        nonlocal current, max_seen
        with lock:
            current += 1
            max_seen = max(max_seen, current)
        time.sleep(0.15)
        with lock:
            current -= 1
        return {
            "provider": "fake",
            "candidates": [],
            "codes": [],
            "matched_count": 0,
            "unmatched_count": 0,
            "raw_text": "",
        }

    monkeypatch.setattr(watchlist_api, "import_watchlist_image", slow_import)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    # 使用独立 limiter，避免与其它用例共享状态
    monkeypatch.setattr(watchlist_api, "_OCR_LIMITER", anyio.CapacityLimiter(2))

    request = _mock_request(tmp_path)

    async def one():
        file = _mock_upload(content=_png_bytes(64, 64))
        return await import_from_image(request, file)

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(one)

    assert max_seen <= 2
    assert max_seen >= 1


def test_add_batch_reports_net_new(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    watchlist.add("600036.SH")
    request = _mock_request(tmp_path)
    result = add_batch(
        BatchAddRequest(symbols=["600036.SH", "515880.SH", "000001.SZ"]),
        request,
    )
    assert result["added"] == 2
    symbols = {r["symbol"] for r in result["symbols"]}
    assert symbols == {"600036.SH", "515880.SH", "000001.SZ"}
