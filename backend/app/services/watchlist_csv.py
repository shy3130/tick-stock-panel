"""自选股 CSV/TXT 导入：解码 → 逐行抽代码/名称 → instruments 校验。

国内行情软件（同花顺/东财/通达信）导出的自选多为 CSV/TXT，且常为 GBK 系编码
（参见 ext_data.ensure_utf8_csv 的说明）。本模块负责把上传字节解析为与截图 OCR
一致的候选结构，前端复用同一套勾选确认流程。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Any

from app.services.watchlist_ocr.pipeline import (
    _CODE_RE,
    ImportCandidate,
    build_instrument_lookups,
    resolve_candidates,
)

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(f"[{chr(0x4E00)}-{chr(0x9FFF)}]")  # CJK 统一表意文字块

# 编码回退链：UTF-8（含 BOM）→ GB18030（GB18030 是 GBK 超集，无需单独回退）
_ENCODINGS = ("utf-8-sig", "gb18030")


def decode_csv_bytes(raw: bytes) -> str:
    """把上传字节解码为文本，兼容 UTF-8 / GBK 系编码。"""
    if not raw:
        raise ValueError("空文件")
    last_err: Exception | None = None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            last_err = e
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 后重试") from last_err


def _is_code_cell(cell: str) -> bool:
    # 调用方（parse_csv_rows）已 strip 过单元格
    return bool(_CODE_RE.fullmatch(cell))


def _pick_name(cells: list[str]) -> str | None:
    """取行内首个含 ≥2 个汉字且非六位代码的单元格作为名称候选。"""
    for cell in cells:
        if not cell or _is_code_cell(cell):
            continue
        if len(_CJK_RE.findall(cell)) >= 2:
            return cell
    return None


def parse_csv_rows(text: str) -> list[tuple[list[str], str | None]]:
    """解析 CSV/TXT 文本为 [(行内代码列表, 名称候选), ...]。

    - 自动识别逗号 / Tab 分隔（同花顺/通达信导出常见 Tab）。
    - 逐行取所有六位数字作为代码候选；无代码行保留给名称兜底（是否输出由
      import_watchlist_csv 决定：表头等名称命不中主数据的行会被忽略）。
    - 返回列表保持文件行序。
    """
    if not text.strip():
        return []

    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    rows: list[tuple[list[str], str | None]] = []
    for raw_row in reader:
        cells = [c.strip() for c in raw_row if c is not None]
        if not cells:
            continue
        codes: list[str] = []
        for cell in cells:
            codes.extend(m.group(1) for m in _CODE_RE.finditer(cell))
        rows.append((codes, _pick_name(cells)))
    return rows


def import_watchlist_csv(
    raw: bytes,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """解析 CSV/TXT 并返回候选列表（不写入自选）。返回结构与 OCR 一致。"""
    text = decode_csv_bytes(raw)
    rows = parse_csv_rows(text)

    code_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    # 名称兜底反向表：CSV 可能只有名称列（无代码）
    name_to_symbol: dict[str, str] = {}
    for symbol, name in symbol_to_name.items():
        name_to_symbol.setdefault(name, symbol)

    existing = existing_symbols or set()
    # 全部唯一代码按出现顺序一次性构建候选（复用 OCR 的构造逻辑，单一来源）
    unique_codes: list[str] = []
    for row_codes, _ in rows:
        for c in row_codes:
            if c not in unique_codes:
                unique_codes.append(c)
    cand_by_code = {
        c.code: c
        for c in resolve_candidates(unique_codes, code_to_symbol, symbol_to_name, existing)
    }

    candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()       # 已发出的已匹配 symbol
    emitted_unmatched: set[str] = set()  # 已发出的未匹配 code
    for row_codes, row_name in rows:
        # 行内代码优先：取第一个已匹配主数据的（避免价格/成交量数字误报）
        matched = next((cand_by_code[c] for c in row_codes if cand_by_code[c].matched), None)
        symbol = matched.symbol if matched else (name_to_symbol.get(row_name) if row_name else None)
        if symbol:
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            cand = matched or ImportCandidate(
                code=row_codes[0] if row_codes else "",
                symbol=symbol,
                name=symbol_to_name.get(symbol),
                matched=True,
                already_in_watchlist=symbol in existing,
            )
        else:
            # 名称兜底失败且无代码 → 表头/杂项行，忽略
            if not row_codes:
                continue
            code = row_codes[0]
            if code in emitted_unmatched:
                continue
            emitted_unmatched.add(code)
            cand = ImportCandidate(
                code=code,
                symbol=None,
                name=row_name,
                matched=False,
                already_in_watchlist=False,
            )
        candidates.append(cand.to_dict())

    matched_count = sum(1 for c in candidates if c["matched"])
    return {
        "provider": "csv",
        "raw_text": text,
        "codes": unique_codes,
        "candidates": candidates,
        "matched_count": matched_count,
        "unmatched_count": len(candidates) - matched_count,
    }
