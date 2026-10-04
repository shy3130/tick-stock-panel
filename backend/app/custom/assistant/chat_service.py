"""AI 助手对话编排 — 无状态、NDJSON 事件流、SSE 逐字流式 + 有界工具循环。

流式实现见 streaming.py(模块自建, 不改核心): 每轮对话以 stream=True +
tools= 调用模型, 文本 delta 逐段推给前端; 模型请求工具时执行并回填,
进入下一轮, 正文继续在同一个占位消息上追加。生成任务与事件消费通过
asyncio.Queue 并行 — 用户在模型思考/取数期间就能看到足迹事件。

失败语义 fail-closed: 无 Key / Codex CLI(无 tools 协议)在入口直接给
error 事件, 不做纯文本降级; 工具执行错误按既有契约回填给模型继续。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.prompt import build_system_prompt
from app.custom.assistant.streaming import stream_openai_round
from app.services.ai_provider import (
    ai_configured,
    current_ai_context_window,
    current_ai_max_output_tokens,
    is_codex_cli_provider,
)

# 单轮最多保留的用户/助手消息条数(约 8 轮对话), 更早历史截断以控 token。
_MAX_HISTORY_MESSAGES = 16

# 工具轮次检查点(非硬限): 到达后弹「是否继续」卡(与动作确认卡同机制),
# 继续则重置计数再跑一个周期, 停止或超时优雅收尾 — 防失控展开的同时不掐断复杂编排。
# 实际值在设置页 AI 配置里可调(ai_round_checkpoint, 0=不检查), 此处仅兜底默认。
_TOOL_ROUND_CHECKPOINT = 100


def _round_checkpoint() -> int:
    """本次对话的轮次检查点(0=不检查)。"""
    from app.services.ai_provider import current_ai_round_checkpoint

    try:
        return max(0, int(current_ai_round_checkpoint()))
    except Exception:  # 配置异常时退回默认, 不因配置问题掐断对话
        return _TOOL_ROUND_CHECKPOINT

_SETTINGS_HINT = "/settings?tab=ai"


def _error_event(kind: str, message: str, hint: str = "") -> dict[str, str]:
    event = {"type": "error", "kind": kind, "message": message}
    if hint:
        event["hint"] = hint
    return event


def _classify_exception(exc: Exception) -> dict[str, str]:
    text = str(exc)
    if "API Key" in text and "未配置" in text:
        return _error_event("no_key", text, _SETTINGS_HINT)
    if "上下文窗口" in text or "输入过长" in text:
        return _error_event(
            "input_too_long",
            f"{text} 可开启新对话或缩短提问后重试。",
        )
    if "超时" in text or "Timeout" in exc.__class__.__name__ or "连接" in text or "Connection" in exc.__class__.__name__:
        return _error_event("network", "AI 服务连接或超时失败, 请稍后重试或检查 AI Base URL / 网络。")
    return _error_event("model", text or exc.__class__.__name__)


def _truncate_history(history: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    """保留最近 N 条消息; 返回 (截断后历史, 是否发生了截断)。"""
    clean = [
        {"role": item["role"], "content": str(item.get("content") or "")}
        for item in history
        if item.get("role") in {"user", "assistant"} and str(item.get("content") or "").strip()
    ]
    if len(clean) <= _MAX_HISTORY_MESSAGES:
        return clean, False
    start = len(clean) - _MAX_HISTORY_MESSAGES
    # 截断后必须以 user 开头, 丢弃窗口开头的连续 assistant 消息。
    while start < len(clean) and clean[start]["role"] != "user":
        start += 1
    if start >= len(clean):
        return clean[-1:], False
    return clean[start:], True


def _estimate_input_tokens(messages: list[dict[str, Any]]) -> int:
    """与核心 ai_provider 相同口径的粗估: 中文 1 字 1 token, 其余 4 字符 1 token。"""
    total = 0
    for message in messages:
        text = str(message.get("content") or "")
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        total += cjk + (len(text) - cjk) // 4 + 1
    return max(1, total)


def _guard_input_budget(messages: list[dict[str, Any]]) -> None:
    """输入估算超出上下文窗口时提前报错(口径同核心 _check_input_budget)。"""
    window = current_ai_context_window()
    if window <= 0:
        return
    reserve = current_ai_max_output_tokens()
    estimated = _estimate_input_tokens(messages)
    if estimated + reserve > window:
        raise ValueError(
            f"输入过长: 估算输入约 {estimated} tokens, 加上输出预算 {reserve} tokens, "
            f"超过上下文窗口 {window}。请缩短输入, 或在 AI 设置中调大『上下文窗口』。"
        )


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """把 tool call 的 arguments JSON 字符串解析为 dict, 解析失败返回空 dict。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _assistant_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """把本轮累积的工具调用还原成回传给上游的 assistant.tool_calls[] 元素。

    extra_content (Gemini 3 的 thought_signature) 必须原样带回, 否则上游 400。
    """
    item: dict[str, Any] = {
        "id": call["id"],
        "type": "function",
        "function": {"name": call.get("name", ""), "arguments": call.get("arguments", "")},
    }
    if call.get("extra_content"):
        item["extra_content"] = call["extra_content"]
    return item


async def chat_stream(
    *,
    history: list[dict[str, str]],
    context: dict | None = None,
    engine: Any = None,
    data_dir: str | None = None,
    repo: Any = None,
    quote_service: Any = None,
    depth_service: Any = None,
    capabilities: Any = None,
    financial_scheduler: Any = None,
) -> AsyncIterator[str]:
    """执行一轮对话, 逐行 yield NDJSON 事件(见模块 __init__ 的协议注释)。"""
    if not ai_configured():
        yield _line(_error_event(
            "no_key",
            "AI 未配置: 请先在设置页配置 AI 供应商和 API Key。",
            _SETTINGS_HINT,
        ))
        yield _line({"type": "done"})
        return

    if is_codex_cli_provider():
        yield _line(_error_event(
            "provider",
            "当前 AI 供应商(Codex CLI)不支持工具调用, AI 助手需要 OpenAI 兼容模型。",
            _SETTINGS_HINT,
        ))
        yield _line({"type": "done"})
        return

    truncated, was_truncated = _truncate_history(history)
    if not truncated:
        yield _line(_error_event("model", "对话历史为空, 请先输入问题。"))
        yield _line({"type": "done"})
        return

    if was_truncated:
        yield _line({"type": "notice", "message": f"对话较长, 已仅保留最近 {_MAX_HISTORY_MESSAGES // 2} 轮作为上下文。"})

    req_messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(context)},
        *truncated,
    ]
    schemas = assistant_tools.build_tool_schemas()
    tool_ctx = assistant_tools.ToolContext.build(
        repo=repo,
        quote_service=quote_service,
        depth_service=depth_service,
        engine=engine,
        data_dir=data_dir,
        capabilities=capabilities,
        financial_scheduler=financial_scheduler,
    )

    queue: asyncio.Queue = asyncio.Queue()
    sentinel: object = object()

    async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        call_id = assistant_actions.new_call_id()
        await queue.put({
            "type": "tool_call",
            "call_id": call_id,
            "name": name,
            "args": _compact_args(args),
        })
        started = time.monotonic()

        # 动作工具确认闸门: 先发确认卡事件并挂起, 用户点确认后才真正执行。
        # 拒绝/超时按工具错误契约回填(ok=False), 模型据此改走文字建议而非重试。
        if name in assistant_actions.ACTION_TOOLS:
            action = await assistant_actions.registry.register(call_id, name, args)
            meta = assistant_actions.ACTION_META.get(name, {"label": name, "risk": ""})
            await queue.put({
                "type": "action_confirm",
                "call_id": call_id,
                "name": name,
                "label": meta["label"],
                "risk": meta["risk"],
                "expires_in": int(assistant_actions.registry.timeout_s),
            })
            decision = await assistant_actions.registry.await_decision(action)
            if decision != "approved":
                denied = (
                    "用户已拒绝该操作, 未执行。不要重复尝试同一操作。"
                    if decision == "denied"
                    else f"确认超时({int(assistant_actions.registry.timeout_s)} 秒未确认), 未执行。"
                )
                result = {"ok": False, "error": denied}
                await queue.put({
                    "type": "tool_result",
                    "call_id": call_id,
                    "name": name,
                    "ok": False,
                    "summary": assistant_tools.summarize_tool_result(name, result),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                return result
        result = await assistant_tools.execute_assistant_tool(name, args, tool_ctx)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        event = {
            "type": "tool_result",
            "call_id": call_id,
            "name": name,
            "ok": bool(result.get("ok")),
            "summary": assistant_tools.summarize_tool_result(name, result),
            "elapsed_ms": elapsed_ms,
        }
        # 可绘图数据(分时/日K)随事件透传, 前端自动附图
        inner = result.get("result")
        charts = inner.get("charts") if isinstance(inner, dict) else None
        if charts:
            event["charts"] = charts
        await queue.put(event)
        return result

    async def run() -> None:
        try:
            _guard_input_budget(req_messages)
            text_seen = False
            tool_rounds = 0
            round_checkpoint = _round_checkpoint()
            while True:
                round_text: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                async for event in stream_openai_round(req_messages, schemas, temperature=0.3, timeout=240.0):
                    if event["type"] == "text":
                        round_text.append(event["delta"])
                        await queue.put({"type": "delta", "content": event["delta"]})
                    elif event["type"] == "round_end":
                        tool_calls = event.get("tool_calls") or []

                if not tool_calls:
                    if not round_text and not text_seen:
                        await queue.put(_error_event("model", "本轮未产出回答, 请重试。"))
                    return

                tool_rounds += 1
                if round_checkpoint and tool_rounds >= round_checkpoint:
                    # 轮次检查点: 复用动作确认闸门(PendingRegistry + 决策端点)。
                    # 批准 → 重置计数继续; 拒绝/超时 → 保留已生成内容, 优雅收尾。
                    checkpoint_id = assistant_actions.new_call_id()
                    action = await assistant_actions.registry.register(
                        checkpoint_id, "rounds_continue", {"reached": tool_rounds},
                    )
                    await queue.put({
                        "type": "rounds_confirm",
                        "call_id": checkpoint_id,
                        "reached": tool_rounds,
                        "expires_in": int(assistant_actions.registry.timeout_s),
                    })
                    decision = await assistant_actions.registry.await_decision(action)
                    if decision != "approved":
                        await queue.put({
                            "type": "notice",
                            "message": (
                                f"已在 {tool_rounds} 轮工具调用的检查点停止本轮对话, "
                                "已生成的内容保留。可缩小问题范围后重新提问。"
                            ),
                        })
                        return
                    tool_rounds = 0

                # 部分兼容网关的流式 tool_calls 不带 id: 补一次并写回 call, 助手消息的
                # tool_calls[].id 与下方 role:tool 回填的 tool_call_id 必须是同一个值。
                for call in tool_calls:
                    if not call.get("id"):
                        call["id"] = uuid.uuid4().hex[:8]

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [_assistant_tool_call(call) for call in tool_calls],
                }
                if round_text:
                    assistant_message["content"] = "".join(round_text)
                    text_seen = True
                req_messages.append(assistant_message)

                for call in tool_calls:
                    result = await execute(
                        str(call.get("name", "")),
                        _parse_tool_arguments(str(call.get("arguments") or "")),
                    )
                    req_messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
        except Exception as exc:  # 单轮失败收敛为 error 事件, 不打断流
            await queue.put(_classify_exception(exc))
        finally:
            await queue.put({"type": "done"})
            await queue.put(sentinel)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield _line(item)
    finally:
        # 客户端断连/消费方停止时终止生成任务, 不遗留挂起协程。
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _compact_args(args: dict[str, Any]) -> dict[str, Any]:
    """足迹事件的 args 预览: 递归保留 dict/list 结构(动作确认卡需要展示完整
    待执行参数), 截断超长字符串与超大集合, 防单行事件过大。"""
    def compact(value: Any, depth: int = 0) -> Any:
        if isinstance(value, dict) and depth < 3:
            return {str(k): compact(v, depth + 1) for k, v in list(value.items())[:20]}
        if isinstance(value, (list, tuple)) and depth < 3:
            return [compact(v, depth + 1) for v in list(value)[:20]]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        text = str(value)
        return text[:120] + "…" if len(text) > 120 else text

    return {str(k): compact(v) for k, v in args.items()}


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False)
