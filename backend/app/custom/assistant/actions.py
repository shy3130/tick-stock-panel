"""AI 助手动作工具确认闸门 — 写操作/重计算先经用户点按确认再执行。

动作工具(chat_service.execute 检测 ACTION_TOOLS 时挂闸门):
- create_signal_strategy: 写 user_data/custom_signals/*.json 并失效多层缓存
- run_backtest:           spawn 回测子进程(重计算, 占共享限流槽)
- add_to_watchlist:       修改用户自选列表
- sync_data:              触发数据补全后台任务(盘后管道/财务/分钟K扩展)

实现: 进程内 PendingRegistry, call_id → PendingAction(asyncio.Event)。
对话流(NDJSON)发出 action_confirm 事件后挂起等待; 前端确认卡 POST
/actions/{call_id}/decision 唤醒。决策超时视为拒绝(不执行), 注册表
条目回收。无跨进程/持久化需求 — 对话流与决策端点在同一 uvicorn 进程。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 须确认才能执行的工具名(工具实现在 tools.py / tool_catalog)。
ACTION_TOOLS: frozenset[str] = frozenset({
    "create_signal_strategy",
    "run_backtest",
    "add_to_watchlist",
    "sync_data",
})

# 确认卡展示元数据: label=动作名, risk=一句话影响说明。
ACTION_META: dict[str, dict[str, str]] = {
    "create_signal_strategy": {
        "label": "创建自定义信号",
        "risk": "将在信号库写入一个新的信号定义并清空策略/指标缓存",
    },
    "run_backtest": {
        "label": "运行策略回测",
        "risk": "将启动回测子进程, 占用共享计算资源, 可能需要数秒到数十秒",
    },
    "add_to_watchlist": {
        "label": "加入自选",
        "risk": "将修改你的自选列表",
    },
    "sync_data": {
        "label": "数据补全/同步",
        "risk": "将从外部数据源拉取数据并写入本地(后台执行, 耗时视网络而定; 已有任务会复用)",
    },
}

_DECISION_TIMEOUT_S = 120.0
_MAX_PENDING = 100


@dataclass
class PendingAction:
    call_id: str
    name: str
    args: dict[str, Any]
    created: float = field(default_factory=time.monotonic)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None  # approved / denied / expired(超时或溢出回收, 用户未作答)


class PendingRegistry:
    """call_id → 待决策动作; await_decision() 阻塞至确认/拒绝/超时。"""

    def __init__(self, timeout_s: float = _DECISION_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, PendingAction] = {}

    async def register(self, call_id: str, name: str, args: dict[str, Any]) -> PendingAction:
        action = PendingAction(call_id=call_id, name=name, args=dict(args or {}))
        self._sweep_expired()
        self._pending[call_id] = action
        # 挂起的确认卡超过上限时按最旧丢弃(视为拒绝), 防注册表无界增长
        overflow = len(self._pending) - _MAX_PENDING
        for stale_id in list(self._pending)[:max(overflow, 0)]:
            stale = self._pending.pop(stale_id)
            stale.decision = "expired"
            stale.event.set()
        return action

    async def resolve(self, call_id: str, approve: bool) -> PendingAction | None:
        """决策端点调用; 条目不存在/已过期返回 None。"""
        action = self._pending.pop(call_id, None)
        if action is None:
            return None
        action.decision = "approved" if approve else "denied"
        action.event.set()
        return action

    async def await_decision(self, action: PendingAction) -> str:
        """返回 'approved' | 'denied'(用户拒绝) | 'expired'(超时未作答); 后两者均不执行。

        超时与拒绝分开: 调用方回填给模型的说法不同 — 拒绝是用户不要这个操作,
        超时只是没人点, 不能说成用户拒绝。
        """
        try:
            await asyncio.wait_for(action.event.wait(), timeout=self.timeout_s)
        except TimeoutError:
            self._pending.pop(action.call_id, None)
            return "expired"
        return action.decision or "denied"

    def _sweep_expired(self) -> None:
        """register 时顺手回收早已超时但等待方已消失的条目(如客户端断连)。"""
        now = time.monotonic()
        for stale_id in [k for k, v in self._pending.items() if now - v.created > self.timeout_s + 60]:
            del self._pending[stale_id]


registry = PendingRegistry()


def new_call_id() -> str:
    """足迹 call_id 与确认卡 call_id 使用同一值, 前端据此关联两条事件。"""
    return uuid.uuid4().hex[:12]
