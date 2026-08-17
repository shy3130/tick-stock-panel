"""自选 CSV/TXT 导入：解码、逐行解析、主数据匹配与名称兜底、API 门禁。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import watchlist as watchlist_api
from app.api.watchlist import import_from_csv
from app.services import watchlist
from app.services.watchlist_csv import (
    decode_csv_bytes,
    import_watchlist_csv,
    parse_csv_rows,
)


def _write_instruments(data_dir: Path) -> None:
    inst = data_dir / "instruments"
    inst.mkdir()
    pl.DataFrame(
        {
            "code": ["600036", "515880"],
            "symbol": ["600036.SH", "515880.SH"],
            "name": ["招商银行", "通信ETF国泰"],
        }
    ).write_parquet(inst / "instruments.parquet")


def _mock_upload(*, content: bytes, content_type: str = "text/csv", filename: str = "watchlist.csv") -> MagicMock:
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


# ---- 解码 ----

def test_decode_utf8_bom_stripped():
    raw = "代码,名称\n600036,招商银行\n".encode("utf-8-sig")
    text = decode_csv_bytes(raw)
    assert not text.startswith("﻿")
    assert "招商银行" in text


def test_decode_gbk_fallback():
    raw = "600519,贵州茅台\n".encode("gbk")
    assert "贵州茅台" in decode_csv_bytes(raw)


def test_decode_unknown_encoding_raises():
    # 同时破坏 UTF-8 与 GBK 系编码的字节序列
    raw = b"\x00\x80\x00\x80\x00\x80"
    with pytest.raises(ValueError, match="编码"):
        decode_csv_bytes(raw)


def test_decode_empty_raises():
    with pytest.raises(ValueError, match="空文件"):
        decode_csv_bytes(b"")


# ---- 解析 ----

def test_parse_comma_csv_with_header():
    text = "股票代码,股票名称,最新价\n600036,招商银行,42.50\n515880,通信ETF,1.34\n"
    rows = parse_csv_rows(text)
    # 表头行保留（无代码），由 import 层按名称是否命中主数据过滤
    assert rows[0] == ([], "股票代码")
    assert [(codes, name) for codes, name in rows[1:]] == [
        (["600036"], "招商银行"),
        (["515880"], "通信ETF"),
    ]


def test_parse_tab_separated():
    text = "600036\t招商银行\t42.50\n515880\t通信ETF\t1.34\n"
    rows = parse_csv_rows(text)
    assert [(codes, name) for codes, name in rows] == [
        (["600036"], "招商银行"),
        (["515880"], "通信ETF"),
    ]


def test_parse_plain_codes_one_per_line():
    text = "600036\n515880\n"
    assert [codes for codes, _ in parse_csv_rows(text)] == [["600036"], ["515880"]]


def test_parse_concatenated_code_name_cell():
    # 通达信风格：代码+名称在同一字段
    text = "600036招商银行\n515880通信ETF国泰\n"
    rows = parse_csv_rows(text)
    assert [(codes, name) for codes, name in rows] == [
        (["600036"], "600036招商银行"),
        (["515880"], "515880通信ETF国泰"),
    ]


# ---- 主数据匹配 / 名称兜底 ----

def test_import_matched_name_fallback_and_unmatched(tmp_path: Path):
    _write_instruments(tmp_path)
    raw = (
        "代码,名称,现价\n"
        "600036,招商银行,42.50\n"
        "通信ETF国泰,1.34\n"
        "000001,平安银行\n"
        "999999,不存在,1.00\n"
    ).encode()
    res = import_watchlist_csv(raw, tmp_path, existing_symbols={"600036.SH"})

    by_code = {c["code"]: c for c in res["candidates"]}
    assert by_code["600036"]["matched"] and by_code["600036"]["already_in_watchlist"]
    assert by_code["600036"]["name"] == "招商银行"
    # 名称兜底命中（无代码行）
    assert any(
        c["symbol"] == "515880.SH" and c["matched"] and c["name"] == "通信ETF国泰"
        for c in res["candidates"]
    )
    assert by_code["000001"]["matched"] is False
    assert by_code["999999"]["matched"] is False
    assert res["matched_count"] == 2
    assert res["unmatched_count"] == 2
    assert res["provider"] == "csv"
    assert res["codes"] == ["600036", "000001", "999999"]


def test_import_dedupe_and_header_skipped(tmp_path: Path):
    _write_instruments(tmp_path)
    raw = "代码,名称\n600036,招商银行\n600036,招商银行\n515880,通信ETF国泰\n".encode()
    res = import_watchlist_csv(raw, tmp_path)
    symbols = [c["symbol"] for c in res["candidates"]]
    assert symbols == ["600036.SH", "515880.SH"]


def test_import_junk_rows_ignored(tmp_path: Path):
    _write_instruments(tmp_path)
    raw = "hello,world\n600036,招商银行\n,,\n".encode()
    res = import_watchlist_csv(raw, tmp_path)
    assert [c["symbol"] for c in res["candidates"]] == ["600036.SH"]


# ---- API 门禁 ----

@pytest.mark.asyncio
async def test_import_csv_rejects_bad_extension(tmp_path: Path):
    request = _mock_request(tmp_path)
    # 类型与扩展名都非白名单 → 拒绝
    file = _mock_upload(
        content=b"x",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="watchlist.xlsx",
    )
    with pytest.raises(HTTPException) as ei:
        await import_from_csv(request, file)
    assert ei.value.status_code == 400
    assert "仅支持" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_csv_rejects_oversized_bytes(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(HTTPException) as ei:
        await import_from_csv(request, file)
    assert ei.value.status_code == 400
    assert "过大" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_csv_rejects_empty(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"")
    with pytest.raises(HTTPException) as ei:
        await import_from_csv(request, file)
    assert ei.value.status_code == 400
    assert "空文件" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_csv_no_candidates_raises(tmp_path: Path, monkeypatch):
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"hello\nworld\n")
    with pytest.raises(HTTPException) as ei:
        await import_from_csv(request, file)
    assert ei.value.status_code == 400
    assert "未识别" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_csv_maps_value_error_to_400(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])

    def boom(*_a, **_k):
        raise ValueError("无法识别文件编码")

    monkeypatch.setattr(watchlist_api, "import_watchlist_csv", boom)
    file = _mock_upload(content=b"x")
    with pytest.raises(HTTPException) as ei:
        await import_from_csv(request, file)
    assert ei.value.status_code == 400
    assert "编码" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_import_csv_success(tmp_path: Path, monkeypatch):
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    file = _mock_upload(content="代码,名称\n600036,招商银行\n515880,通信ETF国泰\n".encode())
    res = await import_from_csv(request, file)
    assert res["provider"] == "csv"
    assert res["matched_count"] == 2
    assert {c["symbol"] for c in res["candidates"]} == {"600036.SH", "515880.SH"}
    assert "raw_text" not in res
