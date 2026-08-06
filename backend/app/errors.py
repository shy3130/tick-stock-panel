"""统一失败语义 — API 标准错误码 (默认 HTTP 422)。

照搬 YMOS 失败语义原则:用结构化错误码代替裸 500,让前端能据此区分
「可重试(数据过期/内核未就绪)」vs「需用户介入(数据不完整/依赖缺失)」vs「无需动作(无变化)」。

主会话负责把 ``app_error_handler`` 注册为 FastAPI exception handler:
    ``app.add_exception_handler(AppError, app_error_handler)``
本模块只交付异常类、错误码常量与 handler 函数,不触碰 main.py 接线。
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

# ── 标准错误码 (语义稳定,前端可据此分支) ──────────────────
DATA_INCOMPLETE = "data_incomplete"               # 采样不足/缺字段,需补数据
STALE_INPUT = "stale_input"                       # 输入行情/数据已过期,需刷新
BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"   # 上游(账户/策略)未就绪
NO_CHANGE = "no_change"                           # 当前态与目标态一致,无可写入
KERNEL_NOT_READY = "kernel_not_ready"             # 策略内核未就绪/未声明
AI_OUTPUT_INVALID = "ai_output_invalid"           # AI 输出语法/schema/不变量无效
AI_PROVIDER_ERROR = "ai_provider_error"           # AI provider 配额/鉴权/网络错误

ALL_CODES = (
    DATA_INCOMPLETE,
    STALE_INPUT,
    BLOCKED_BY_DEPENDENCY,
    NO_CHANGE,
    KERNEL_NOT_READY,
    AI_OUTPUT_INVALID,
    AI_PROVIDER_ERROR,
)


class AppError(Exception):
    """携带结构化错误码的业务异常,由 app_error_handler 渲染为 422 JSON。

    code        取 ALL_CODES 之一 (语义稳定,前端据此分支)
    detail      人类可读的中文说明
    http_status 默认 422 (Unprocessable Entity);可按需覆盖
    """

    def __init__(self, code: str, detail: Any, http_status: int = 422):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.http_status = http_status


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
    """把 AppError 统一渲染为 ``{"code": ..., "detail": ...}`` JSON 响应。

    签名与 main.py 现有 ``@app.exception_handler`` 处理器一致 (request, exc) -> JSONResponse,
    直接 ``app.add_exception_handler(AppError, app_error_handler)`` 即可接线。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "detail": exc.detail},
    )
