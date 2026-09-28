"""AI 对话助手扩展 — 完全解耦的 L2 二开模块。

通过 AI 对话调用面板已有能力(因子目录 / 策略目录 / 数据能力 / 策略回测)。
删除本目录即可整体卸载, 不影响核心功能; 详见模块内 README 说明与
docs/secondary-development.md 的扩展契约。

事件流协议(NDJSON, 每行一个 JSON):
    {"type":"notice","message":"..."}                       历史截断等提示
    {"type":"tool_call","call_id","name","args"}            工具调用开始(前端足迹卡)
    {"type":"tool_result","call_id","name","ok","summary","elapsed_ms","charts?"}
                                                            charts=可绘图数据列表(分时/日K, 前端自动附图)
    {"type":"action_confirm","call_id","name","label","risk","expires_in"}
                                                            动作工具确认卡(创建信号/回测/加自选);
                                                            前端按钮 POST /actions/{call_id}/decision
                                                            {approve:bool}, 超时视为拒绝
    {"type":"delta","content":"..."}                        最终正文(M1 整段一次)
    {"type":"error","kind","message","hint?"}               no_key/provider/input_too_long/rounds/model
    {"type":"done"}                                         本轮结束(含失败)
    {"type":"ping"}                                         心跳保活(空闲 >15s)
"""
from __future__ import annotations

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)

EXTENSION_ID = "assistant.chat"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.custom.assistant.routes import build_router

    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    # 对话无状态, 无需启动期初始化; 策略引擎与数据目录在请求期从 app.state 获取。
    _ = context
