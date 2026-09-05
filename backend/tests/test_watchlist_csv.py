"""自选 CSV/TXT 与粘贴代码导入：解码、逐行/整段解析、主数据匹配、API 门禁。

导入落点统一走自选分组语义（watchlist.add_batch 的 group_ids M:N 并入），
本测试同时覆盖该并入语义，作为前端「并入目标分组」交互的服务端依据。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import watchlist as watchlist_api
from app.api.watchlist import ImportCodesRequest, import_from_codes, import_from_csv
from app.config import settings
from app.services import watchlist
from app.services.watchlist_csv import (
    decode_csv_bytes,
    import_watchlist_codes,
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
    # 像真实 UploadFile 一样: 第一次 read 返回全部内容, 之后返回 b"" 表示读尽
    # (端点已改为分块读取, 一直返回同一段内容的 mock 会被当成无限大的文件)
    file.read = AsyncMock(side_effect=[content, b""])
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


# ---- 主数据匹配 / 名称兜底（CSV 行级） ----

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


# ---- 粘贴代码（整段抽码，不压缩同行多码） ----

def test_codes_dedupe_order_and_counts(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_codes("600036\n515880\n999999\n600036", tmp_path)
    assert res["provider"] == "codes"
    assert res["codes"] == ["600036", "515880", "999999"]
    assert res["matched_count"] == 2
    assert res["unmatched_count"] == 1
    by_code = {c["code"]: c for c in res["candidates"]}
    assert by_code["600036"]["symbol"] == "600036.SH"
    assert by_code["999999"]["matched"] is False


def test_codes_same_line_multiple_codes_not_collapsed(tmp_path: Path):
    # 同行多码（逗号/空格分隔）不得被压成单候选
    _write_instruments(tmp_path)
    res = import_watchlist_codes("600036, 515880   601636", tmp_path)
    assert res["codes"] == ["600036", "515880", "601636"]
    assert res["matched_count"] == 2
    assert res["unmatched_count"] == 1


def test_codes_blank_returns_empty_without_parquet(tmp_path: Path):
    # 无码时不读主数据直接返回空结果（数据目录甚至不存在）
    res = import_watchlist_codes("  \n  ", tmp_path)
    assert res["codes"] == []
    assert res["candidates"] == []
    assert res["matched_count"] == 0 and res["unmatched_count"] == 0


def test_codes_over_max_raises(tmp_path: Path):
    text = "\n".join(f"{600000 + i}" for i in range(1001))
    with pytest.raises(ValueError, match="最多"):
        import_watchlist_codes(text, tmp_path, max_codes=1000)


def test_import_functions_do_not_write_watchlist(tmp_path: Path, monkeypatch):
    _write_instruments(tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    before = watchlist.list_symbols()
    assert before == []
    import_watchlist_csv("600036,招商银行\n".encode(), tmp_path)
    import_watchlist_codes("515880", tmp_path)
    assert watchlist.list_symbols() == []


# ---- 分组合并语义（导入落点，M:N 并入服务端依据） ----

def test_add_batch_merges_into_target_group_mn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, g1 = watchlist.create_group("组合一")
    _, g2 = watchlist.create_group("组合二")
    watchlist.add("600036.SH", group_id=g1["id"])

    rows, added = watchlist.add_batch(["600036.SH", "515880.SH"], group_id=g2["id"])
    by_symbol = {r["symbol"]: r for r in rows}
    # 已在自选且属组合一：并入组合二，两组共存（M:N）
    assert g1["id"] in by_symbol["600036.SH"]["group_ids"]
    assert g2["id"] in by_symbol["600036.SH"]["group_ids"]
    # 真正新增的才计 added
    assert g2["id"] in by_symbol["515880.SH"]["group_ids"]
    assert added == 1


def test_add_batch_merges_into_multiple_groups_mn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, g1 = watchlist.create_group("组合一")
    _, g2 = watchlist.create_group("组合二")
    _, g3 = watchlist.create_group("组合三")
    watchlist.add("600036.SH", group_id=g1["id"])

    # 一次批量调用同时并入 g2/g3（多组导入，原子单写；含重复 id 验证去重）
    rows, added = watchlist.add_batch(
        ["600036.SH", "515880.SH"],
        group_ids=[g2["id"], g3["id"], g2["id"]],
    )
    by_symbol = {r["symbol"]: r for r in rows}
    gids600 = by_symbol["600036.SH"]["group_ids"]
    # 保留原有 g1 且并入 g2/g3
    assert sorted(gids600) == sorted([g1["id"], g2["id"], g3["id"]])
    assert sorted(by_symbol["515880.SH"]["group_ids"]) == sorted([g2["id"], g3["id"]])
    assert added == 1


# ---- API 门禁：CSV ----

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


# ---- API 门禁：粘贴代码 ----

def test_import_codes_rejects_empty(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    with pytest.raises(HTTPException) as ei:
        import_from_codes(ImportCodesRequest(text="   "), request)
    assert ei.value.status_code == 400
    assert "请输入" in str(ei.value.detail)


def test_import_codes_no_codes_raises(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    with pytest.raises(HTTPException) as ei:
        import_from_codes(ImportCodesRequest(text="hello world"), request)
    assert ei.value.status_code == 400
    assert "未识别" in str(ei.value.detail)


def test_import_codes_over_max_raises(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    text = "\n".join(f"{600000 + i}" for i in range(1001))
    with pytest.raises(HTTPException) as ei:
        import_from_codes(ImportCodesRequest(text=text), request)
    assert ei.value.status_code == 400
    assert "最多" in str(ei.value.detail)


def test_import_codes_maps_value_error_to_400(tmp_path: Path, monkeypatch):
    request = _mock_request(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])

    def boom(*_a, **_k):
        raise ValueError("一次最多导入 1000 个")

    monkeypatch.setattr(watchlist_api, "import_watchlist_codes", boom)
    with pytest.raises(HTTPException) as ei:
        import_from_codes(ImportCodesRequest(text="600036"), request)
    assert ei.value.status_code == 400
    assert "最多" in str(ei.value.detail)


def test_import_codes_success(tmp_path: Path, monkeypatch):
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    res = import_from_codes(ImportCodesRequest(text="600036\n515880\n999999"), request)
    assert res["provider"] == "codes"
    assert res["matched_count"] == 2
    assert res["unmatched_count"] == 1
    assert {c["symbol"] for c in res["candidates"] if c["matched"]} == {"600036.SH", "515880.SH"}
    assert "raw_text" not in res
