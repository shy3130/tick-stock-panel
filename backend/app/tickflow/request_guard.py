"""SDK 请求守护;供数据源与现有服务共用,不包含任务调度。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# ── SDK 批量调用守护 deadline ────────────────────────────────────────────
# TickFlow SDK 自带 timeout=30s×3 重试, 但只保护其覆盖的请求阶段; 真挂死在
# 不可中断的网络读里时调用永不返回 → 持有独占槽的重任务线程被连坐卡死,
# 后续所有重任务无限排队(「分钟K同步卡死空转」的持有侧根因)。
# 正常单批(限速+重试)远低于该值; 超时按分块失败处理, 心跳与协作取消照常推进。
_SDK_CALL_DEADLINE_S = 600.0


class SDKCallTimeoutError(TimeoutError):
    """单次 SDK 批量调用超过守护 deadline 仍未返回(调用线程已放弃等待)。"""


def _sdk_call_with_deadline(fn: Callable[[], Any], *, timeout_s: float = _SDK_CALL_DEADLINE_S) -> Any:
    """在 daemon 线程执行一次 SDK 调用, join 超时即放弃并抛 SDKCallTimeoutError。

    放弃后调用方按既有分块失败路径继续(记 warning / 标记本批失败), 协作式
    取消在下一分块边界生效, 重任务槽得以释放。被放弃的线程为 daemon:
    不阻塞进程退出, 迟到的结果被丢弃。
    """
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — 原样转交调用方
            outcome["error"] = e

    t = threading.Thread(target=_worker, daemon=True, name="sdk-call-deadline")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise SDKCallTimeoutError(f"SDK 调用超过 {timeout_s:.0f}s 未返回, 已放弃等待")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")
