"""统一失败语义 — AppError 与 app_error_handler 输出格式测试。"""
from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse

from app.errors import (
    ALL_CODES,
    AI_OUTPUT_INVALID,
    AI_PROVIDER_ERROR,
    BLOCKED_BY_DEPENDENCY,
    DATA_INCOMPLETE,
    KERNEL_NOT_READY,
    NO_CHANGE,
    STALE_INPUT,
    AppError,
    app_error_handler,
)


def test_error_codes_stable():
    assert ALL_CODES == (
        DATA_INCOMPLETE,
        STALE_INPUT,
        BLOCKED_BY_DEPENDENCY,
        NO_CHANGE,
        KERNEL_NOT_READY,
        AI_OUTPUT_INVALID,
        AI_PROVIDER_ERROR,
    )


def test_app_error_attributes():
    err = AppError(DATA_INCOMPLETE, "采样不足")
    assert err.code == DATA_INCOMPLETE
    assert err.detail == "采样不足"
    assert err.http_status == 422


def test_app_error_custom_status():
    err = AppError(STALE_INPUT, "行情已过期", http_status=409)
    assert err.http_status == 409


def test_app_error_message_includes_code_and_detail():
    err = AppError(NO_CHANGE, "已是目标态")
    assert str(err) == f"{NO_CHANGE}: 已是目标态"


def test_handler_renders_422_json():
    err = AppError(DATA_INCOMPLETE, "缺少 thesis.invalidation")
    resp = asyncio.run(app_error_handler(request=None, exc=err))  # request 未使用
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 422

    body = json.loads(resp.body)
    assert body == {"code": DATA_INCOMPLETE, "detail": "缺少 thesis.invalidation"}

def test_handler_preserves_code_and_detail_for_each_standard_code():
    for code in ALL_CODES:
        resp = asyncio.run(app_error_handler(request=None, exc=AppError(code, "x")))
        assert resp.status_code == 422

        body = json.loads(resp.body)
        assert body == {"code": code, "detail": "x"}


def test_kernel_not_ready_code_present():
    # 单独确认 KERNEL_NOT_READY 常量值 (前端会据此提示策略内核未就绪)
    assert KERNEL_NOT_READY == "kernel_not_ready"
