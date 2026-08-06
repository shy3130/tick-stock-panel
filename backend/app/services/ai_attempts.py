"""进程内统一的 attempt / 取消 registry。

一套注册表服务所有流式 AI 入口（会话 Agent、个股分析、结构化运行时…）：
- 持有 ``attempt_id -> AttemptHandle``（task + CancellationToken + status）
- 取消先 ``token.cancel()`` 再 ``task.cancel()``，幂等、可重复调用
- task 完成时自动清理，防止泄漏 / zombie
- 单飞（同一 attempt_id 不重复注册）、完成态查询

复用 ``ai_structured`` 的 ID 工厂与 ``CancellationToken``，不另造第二套。

线程/异步安全：所有方法都应当在事件循环线程内调用（与 ``asyncio.Task`` 一致）；
``register``/``unregister``/``get``/``cancel`` 仅依赖 dict 操作，不阻塞。
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

# ai_structured.__init__ 尚未落地，先从 models 子模块导入（同结构、同名）。
from app.services.ai_structured.models import (
    CancellationToken,
    new_attempt_id,
    new_request_id,
)

__all__ = [
    "AttemptHandle",
    "AttemptRegistry",
    "get_registry",
    "new_attempt_id",
    "new_request_id",
    "CancellationToken",
]


class AttemptHandle(TypedDict):
    """单次 attempt 的运行时句柄。"""

    attempt_id: str
    request_id: str
    token: CancellationToken
    task: "asyncio.Task[Any] | None"
    status: str  # running | completed | cancelled | failed


_RUNNING = "running"
_COMPLETED = "completed"
_CANCELLED = "cancelled"
_FAILED = "failed"


class AttemptRegistry:
    """进程内 attempt 注册表（默认单例）。"""

    def __init__(self) -> None:
        self._handles: dict[str, AttemptHandle] = {}

    # ── 注册 ──────────────────────────────────────────────
    def register(
        self,
        *,
        attempt_id: str | None = None,
        request_id: str | None = None,
        task: "asyncio.Task[Any] | None" = None,
        token: CancellationToken | None = None,
    ) -> AttemptHandle:
        """注册一个 attempt；若已存在同 id 的运行中句柄则返回旧的（单飞）。

        attempt_id 为空时由 ``new_attempt_id`` 生成；token 为空时新建。
        绑定 task 时安装 ``done`` 回调，完成后自动 ``_settle`` 并清理。
        """
        aid = attempt_id or new_attempt_id()
        rid = request_id or new_request_id()
        existing = self._handles.get(aid)
        if existing is not None:
            # 同一 attempt_id 已注册：
            # - 若仍是 running 且 task 未完成，直接返回旧句柄（防重复）
            if existing["status"] == _RUNNING and (
                existing["task"] is None or not existing["task"].done()
            ):
                return existing
            # - 否则覆盖已完成/已取消的旧记录（允许 attempt_id 复用极端场景）

        handle: AttemptHandle = {
            "attempt_id": aid,
            "request_id": rid,
            "token": token or CancellationToken(),
            "task": task,
            "status": _RUNNING,
        }
        self._handles[aid] = handle
        if task is not None:
            task.add_done_callback(lambda t, h=handle: self._on_task_done(t, h))
        return handle

    # ── 注销 ──────────────────────────────────────────────
    def unregister(self, attempt_id: str) -> None:
        """显式移除句柄；幂等（不存在则 no-op）。"""
        handle = self._handles.pop(attempt_id, None)
        if handle is not None and handle["task"] is not None:
            # 回调可能尚未触发（手动 unregister），先解绑避免回调二次 settle
            _safe_remove_done_cb(handle["task"], self._handles.get)  # no-op placeholder

    # ── 查询 ──────────────────────────────────────────────
    def get(self, attempt_id: str) -> AttemptHandle | None:
        return self._handles.get(attempt_id)

    def is_running(self, attempt_id: str) -> bool:
        handle = self._handles.get(attempt_id)
        if handle is None or handle["status"] != _RUNNING:
            return False
        task = handle["task"]
        return task is None or not task.done()

    # ── 取消 ──────────────────────────────────────────────
    def cancel(self, attempt_id: str) -> bool:
        """幂等取消：先 set token（让 stage 边界快速失败），再 cancel task。

        返回 True 表示该 attempt 处于 running 态并已发起取消；
        已完成/不存在返回 False（幂等、可重复调用）。
        """
        handle = self._handles.get(attempt_id)
        if handle is None:
            return False
        if not self.is_running(attempt_id):
            return False
        # 1) 令牌先行：provider/stage 边界立刻感知
        handle["token"].cancel()
        task = handle["task"]
        if task is not None and not task.done():
            # 2) 再请求 task 取消（唤醒 await 阻塞处）
            task.cancel()
        return True

    # ── 内部：task 完成回调 ──────────────────────────────
    def _on_task_done(self, task: "asyncio.Task[Any]", handle: AttemptHandle) -> None:
        """Classify the terminal state, consume task errors, then release the handle."""
        if self._handles.get(handle["attempt_id"]) is not handle:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            handle["status"] = _CANCELLED
        else:
            handle["status"] = _FAILED if exc is not None else _COMPLETED
        finally:
            self._handles.pop(handle["attempt_id"], None)


def _safe_remove_done_cb(_task: "asyncio.Task[Any]", _g: Any) -> None:
    """占位：asyncio.Task 没有公开移除回调的 API；保留钩子，当前 no-op。

    语义上 ``unregister`` 只是从注册表移除引用，task 的 done 回调若稍后触发，
    ``_on_task_done`` 会因 handle 不在注册表而提前返回，安全无副作用。
    """
    return None


_REGISTRY: AttemptRegistry | None = None


def get_registry() -> AttemptRegistry:
    """进程级单例 registry。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = AttemptRegistry()
    return _REGISTRY
