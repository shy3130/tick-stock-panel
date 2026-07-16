"""OCR 引擎抽象层 — 当前实现为 Tesseract。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageEnhance, ImageOps

logger = logging.getLogger(__name__)

# 像素上限：防 JPEG 高分辨率解码后 OOM（字节上限挡不住高压缩大图）
_MAX_PIXELS = 12_000_000
# 长边上限：过大则降采样，减内存并加快 OCR
_MAX_EDGE = 2000
# 过窄时适度放大，提升小字识别率
_MIN_WIDTH = 1400


class OcrProvider(ABC):
    """OCR 引擎接口。实现类只负责「图 → 文本」，代码抽取与证券匹配在 pipeline 中完成。"""

    name: str

    @abstractmethod
    def extract_text(self, image_bytes: bytes) -> str:
        """从图片字节提取纯文本（可含换行）。"""

    @abstractmethod
    def available(self) -> bool:
        """运行时依赖是否就绪（二进制/模型等）。"""


def preprocess_for_ocr(image_bytes: bytes) -> Image.Image:
    """暗色券商截图预处理：像素上限、降采样、灰度、反相、增强对比。

    Raises:
        ValueError: 图片分辨率过高（像素数超限）。
    """
    img = Image.open(BytesIO(image_bytes))
    img.load()  # 强制解码，便于尽早失败 / 量尺寸

    pixels = img.width * img.height
    if pixels > _MAX_PIXELS:
        raise ValueError("图片分辨率过高,请裁剪后重试")

    # 大图降采样：既减内存又提速 OCR（只放大不缩小的旧逻辑已去掉）
    if max(img.size) > _MAX_EDGE:
        img.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    gray = ImageOps.grayscale(img)
    # 暗底白字 → 白底黑字，Tesseract 更稳
    inverted = ImageOps.invert(gray)
    contrasted = ImageEnhance.Contrast(inverted).enhance(1.8)
    # 小字适度放大
    w, h = contrasted.size
    if w < _MIN_WIDTH:
        scale = _MIN_WIDTH / w
        contrasted = contrasted.resize(
            (int(w * scale), int(h * scale)), Image.Resampling.LANCZOS
        )
    return contrasted


class TesseractOcrProvider(OcrProvider):
    name = "tesseract"

    def available(self) -> bool:
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("tesseract unavailable: %s", e)
            return False

    def extract_text(self, image_bytes: bytes) -> str:
        import pytesseract

        img = preprocess_for_ocr(image_bytes)
        # 优先数字+字母（股票代码）；中文语言包可选，缺失时回退 eng
        configs = [
            ("chi_sim+eng", "--psm 6"),
            ("eng", "--psm 6"),
            ("eng", "--psm 11"),
        ]
        last_err: Exception | None = None
        for lang, cfg in configs:
            try:
                text = pytesseract.image_to_string(img, lang=lang, config=cfg)
                if text and text.strip():
                    return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.debug("tesseract lang=%s failed: %s", lang, e)
        if last_err:
            raise RuntimeError(f"Tesseract OCR 失败: {last_err}") from last_err
        return ""


@lru_cache(maxsize=1)
def get_ocr_provider() -> OcrProvider:
    """返回当前 OCR 引擎（Tesseract）。"""
    return TesseractOcrProvider()
