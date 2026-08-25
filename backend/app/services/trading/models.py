"""Trading 域数据契约 — 事件类型 / 状态机常量。"""
from __future__ import annotations

# 单笔交易状态
STATUS_PLANNED = "计划中"
STATUS_BUILDING = "建仓中"
STATUS_HOLDING = "持仓中"
STATUS_CLOSED = "已平仓"
STATUS_VOIDED = "已作废"
STATUSES = (STATUS_PLANNED, STATUS_BUILDING, STATUS_HOLDING, STATUS_CLOSED, STATUS_VOIDED)

# 事件类型
KIND_OPEN = "open"          # 建档: 论点 + 失效信号
KIND_PREPARE = "prepare"    # 首次建仓准备
KIND_REVISE = "revise"      # 成交前修订准备
KIND_FILL = "fill"          # 增量成交；complete=true 时显式收口
KIND_ADD = "add"            # 调大建仓计划，不改变仓位事实
KIND_TRIM = "trim"          # 缩减未完成的建仓计划，不改变仓位事实
KIND_TP = "tp"              # 止盈/减仓 (部分卖出)
KIND_SL = "sl"              # 止损 (部分卖出)
KIND_ADJUST = "adjust"      # 调整止损/逻辑退出规则
KIND_CLOSE = "close"        # 全部平仓(终态)
KIND_VOID = "void"          # 零成交计划作废(终态)
EVENT_KINDS = (
    KIND_OPEN, KIND_PREPARE, KIND_REVISE, KIND_FILL,
    KIND_ADD, KIND_TRIM, KIND_TP, KIND_SL, KIND_ADJUST, KIND_CLOSE, KIND_VOID,
)

# 动作模式(决策审计 mode 字段)
MODES = ("buy_new", "fill", "add", "trim", "tp", "sl", "adjust", "close", "void")

SCHEMA_VERSION = 1


class LifecycleError(ValueError):
    """状态机非法迁移 / 事件契约违规。"""
