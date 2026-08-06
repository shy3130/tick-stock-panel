"""Trading 域数据契约 — 事件类型 / 状态机常量。"""
from __future__ import annotations

# 单笔交易状态
STATUS_PLANNED = "计划中"
STATUS_HOLDING = "持仓中"
STATUS_CLOSED = "已平仓"
STATUSES = (STATUS_PLANNED, STATUS_HOLDING, STATUS_CLOSED)

# 事件类型
KIND_OPEN = "open"          # 建档: 论点 + 失效信号
KIND_PREPARE = "prepare"    # 首次建仓准备
KIND_REVISE = "revise"      # 成交前修订准备
KIND_FILL = "fill"          # 确认成交(只能一次)
KIND_ADD = "add"            # 加仓 (planOnly=true 为计划, false 为实际成交)
KIND_TP = "tp"              # 止盈/减仓 (部分卖出)
KIND_SL = "sl"              # 止损 (部分卖出)
KIND_ADJUST = "adjust"      # 调整止损/逻辑退出规则
KIND_CLOSE = "close"        # 全部平仓(终态)
EVENT_KINDS = (
    KIND_OPEN, KIND_PREPARE, KIND_REVISE, KIND_FILL,
    KIND_ADD, KIND_TP, KIND_SL, KIND_ADJUST, KIND_CLOSE,
)

# 动作模式(决策审计 mode 字段)
MODES = ("buy_new", "add", "tp", "sl", "adjust", "close")

SCHEMA_VERSION = 1


class LifecycleError(ValueError):
    """状态机非法迁移 / 事件契约违规。"""
