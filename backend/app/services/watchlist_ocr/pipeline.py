"""截图 → OCR 文本 → 抽代码 → instruments 校验。"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl

from app.services.watchlist_ocr.provider import OcrProvider, get_ocr_provider

logger = logging.getLogger(__name__)

# A 股 / ETF 六位代码；含 OCR 常见拆分：5881 70 / 5881\n70
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_SPLIT_CODE_RE = re.compile(r"(?<!\d)(\d{3,5})\s+(\d{1,3})(?!\d)")


@dataclass
class ImportCandidate:
    code: str
    symbol: str | None
    name: str | None
    matched: bool
    already_in_watchlist: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_codes(text: str) -> list[str]:
    """从 OCR 文本按出现顺序去重抽取六位代码。"""
    if not text:
        return []

    # 先把「5881 70」这类拆分拼回六位，再统一匹配
    def _join_split(m: re.Match[str]) -> str:
        joined = m.group(1) + m.group(2)
        return joined if len(joined) == 6 else m.group(0)

    normalized = _SPLIT_CODE_RE.sub(_join_split, text)

    seen: set[str] = set()
    codes: list[str] = []
    for m in _CODE_RE.finditer(normalized):
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def build_instrument_lookups(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """构建 code→symbol、symbol→name（股票 + ETF）。"""
    code_to_symbol: dict[str, str] = {}
    symbol_to_name: dict[str, str] = {}

    paths: list[Path] = [
        data_dir / "instruments" / "instruments.parquet",
    ]
    etf_dir = data_dir / "instruments_etf"
    if etf_dir.is_dir():
        paths.extend(sorted(etf_dir.glob("*.parquet")))

    for path in paths:
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path)
            if "symbol" not in df.columns:
                continue
            has_code = "code" in df.columns
            has_name = "name" in df.columns
            for row in df.iter_rows(named=True):
                symbol = str(row.get("symbol") or "").strip()
                if not symbol:
                    continue
                code = str(row.get("code") or "").strip() if has_code else ""
                if not (len(code) == 6 and code.isdigit()):
                    bare = symbol.split(".", 1)[0]
                    if len(bare) == 6 and bare.isdigit():
                        code = bare
                if len(code) == 6 and code.isdigit():
                    code_to_symbol.setdefault(code, symbol)
                if has_name:
                    name = str(row.get("name") or "").strip()
                    if name:
                        symbol_to_name.setdefault(symbol, name)
        except Exception as e:  # noqa: BLE001
            logger.debug("read instruments %s failed: %s", path, e)

    return code_to_symbol, symbol_to_name


def resolve_candidates(
    codes: list[str],
    code_to_symbol: dict[str, str],
    symbol_to_name: dict[str, str],
    existing_symbols: set[str] | None = None,
) -> list[ImportCandidate]:
    existing = existing_symbols or set()
    out: list[ImportCandidate] = []
    for code in codes:
        symbol = code_to_symbol.get(code)
        matched = symbol is not None
        name = symbol_to_name.get(symbol) if symbol else None
        out.append(
            ImportCandidate(
                code=code,
                symbol=symbol,
                name=name,
                matched=matched,
                already_in_watchlist=bool(symbol and symbol in existing),
            )
        )
    return out


def import_watchlist_image(
    image_bytes: bytes,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
    provider: OcrProvider | None = None,
) -> dict[str, Any]:
    """识别截图并返回候选列表（不写入自选）。"""
    ocr = provider or get_ocr_provider()
    if not ocr.available():
        raise RuntimeError(
            f"OCR 引擎「{ocr.name}」不可用。请安装 Tesseract（macOS: brew install tesseract；"
            "Docker 镜像已内置）。"
        )

    text = ocr.extract_text(image_bytes)
    codes = extract_codes(text)
    code_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    candidates = resolve_candidates(codes, code_to_symbol, symbol_to_name, existing_symbols)

    matched = [c for c in candidates if c.matched]
    unmatched = [c for c in candidates if not c.matched]

    return {
        "provider": ocr.name,
        "raw_text": text,
        "codes": codes,
        "candidates": [c.to_dict() for c in candidates],
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
    }
