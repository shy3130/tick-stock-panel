"""自选导入上传体积上限回归测试 (import-csv / import-image)。

两个端点原先 `data = await file.read()` 之后才比较长度: 上限只在整个文件已经进入
内存之后生效, 与 ext_data 上传修复 (issue #204) 前的问题同类。_read_upload_capped
分块读取, 越过上限即拒绝(400), 内存占用不超过上限 + 一块。纯逻辑, 不需真实数据源。
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.api import watchlist as watchlist_api
from app.api.watchlist import _read_upload_capped


class _CountingStream(io.BytesIO):
    """记录被读取的字节数, 用来证明越限后不再继续读。"""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size)
        self.consumed += len(chunk)
        return chunk


def _upload(stream: io.BytesIO) -> UploadFile:
    return UploadFile(stream, filename="x.csv")


async def test_under_limit_returns_full_content():
    data = "code,name\n600519.SH,贵州茅台\n".encode()
    got = await _read_upload_capped(_upload(io.BytesIO(data)), 1024, "too large")
    assert got == data


async def test_at_limit_is_allowed():
    data = b"x" * 64
    got = await _read_upload_capped(_upload(io.BytesIO(data)), 64, "too large")
    assert got == data


async def test_over_limit_raises_400_with_the_message_the_endpoint_passes():
    data = b"x" * 200
    with pytest.raises(HTTPException) as exc:
        await _read_upload_capped(_upload(io.BytesIO(data)), 64, "文件过大")
    assert exc.value.status_code == 400
    assert exc.value.detail == "文件过大"


async def test_over_limit_stops_reading_at_the_first_chunk_past_the_cap(monkeypatch):
    # 4 块的文件, 上限 1.5 块: 第 2 块越限即拒绝, 第 3/4 块不应被读取。
    monkeypatch.setattr(watchlist_api, "_UPLOAD_CHUNK_BYTES", 16)
    stream = _CountingStream(b"x" * 64)
    with pytest.raises(HTTPException):
        await _read_upload_capped(_upload(stream), 24, "too large")
    assert stream.consumed == 32


async def test_empty_upload_returns_empty_bytes():
    got = await _read_upload_capped(_upload(io.BytesIO(b"")), 64, "too large")
    assert got == b""
