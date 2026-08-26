from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from time import perf_counter
from typing import Any

from app.services import agent_tools
from app.services.ai_provider import generate_ai_text, generate_ai_with_tools, stream_ai_text

MAX_TOOL_ROUNDS = 5

# F16 进程内并发上限：同一时刻最多 N 路研究对话；超出立即报错，不排队。
MAX_CONCURRENT_AGENT_RUNS = 2
_agent_run_slots = threading.Semaphore(MAX_CONCURRENT_AGENT_RUNS)
_OCCUPANCY_MESSAGE = "已有研究对话在运行，请等待结束后再试"


def try_acquire_agent_slot() -> bool:
    return _agent_run_slots.acquire(blocking=False)


def release_agent_slot() -> None:
    _agent_run_slots.release()


def occupancy_error_line(started_at: float | None = None) -> str:
    elapsed = 0.0 if started_at is None else round((perf_counter() - started_at) * 1000, 1)
    return json.dumps(
        {"type": "error", "message": _OCCUPANCY_MESSAGE, "elapsed_ms": elapsed},
        ensure_ascii=False,
    )


_EXCLUDED_TOOLS: set[str] = set()
ALLOWED_AGENT_TOOLS = [t for t in agent_tools.TOOLS if t["name"] not in _EXCLUDED_TOOLS]
_ALLOWED_NAMES = {t["name"] for t in ALLOWED_AGENT_TOOLS}
_OPENAI_TOOLS = agent_tools.to_openai_tools(ALLOWED_AGENT_TOOLS)

_DSML_INVOKE_RE = re.compile(
    r'<\|\|DSML\|\|invoke\s+name="(?P<name>[^"]+)">(?P<body>.*?)</\|\|DSML\|\|invoke>',
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    r'<\|\|DSML\|\|parameter\s+name="(?P<name>[^"]+)"[^>]*>(?P<value>.*?)</\|\|DSML\|\|parameter>',
    re.DOTALL,
)


def _tools_system() -> str:
    """给原生 function calling 与 DSML/文本降级路径共用的工具契约。"""
    prompt_tools = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool.get("parameters") or tool["input_schema"],
        }
        for tool in ALLOWED_AGENT_TOOLS
    ]
    return (
        "你是 TickFlow Stock Panel 的只读研究助手。你拥有本地 DuckDB A 股数据库，"
        "可查询日线、技术指标、财务数据并运行选股和研究回测。数据问题必须调用下列工具，"
        "禁止以「无法接入数据」为由拒绝。\n"
        "条件选股必须使用强类型工作流：不确定字段时先调用 list_screener_fields，"
        "再调用 screen_stock_pool；严禁生成 SQL、文件路径或在上下文中复制大股票池。"
        "AI 短线池必须调用 screen_stock_pool 并传 preset_id=short_momentum_quality_v1，"
        "不得自行条件化（不接受 conditions/as_of/order_by），只能按需传 limit(5..12)。"
        "短线池由固定确定性策略筛选：模型只解释返回的逐股 evidence，"
        "不得生成、删除或重排候选，也不得改动排序或数量；"
        "逐股引用 evidence 时保持工具返回的顺序与措辞事实。"
        "仅 legacy conditions 分支生成的普通股票池可调用 start_pool_backtest。"
        "short_momentum_quality_v1 的 pool_id 不兼容该工具，禁止传入；"
        "短线池回测只能由前端「送策略回测」显式带入候选。"
        "工具返回的 market_state 是严格 T-1 的确定性市场状态，t_research 是固定研究协议草案；"
        "不得改动状态、阈值、候选或协议，protocol_id 只是研究协议标识而非既有策略。"
        "只有 market_state.state=dispersed 时才可说明前端能展示确认入口，"
        "且必须由用户显式确认创建研究假设，绝不得自动运行回测。"
        "所有回测只用于研究，绝不能调用或虚构下单、交易计划、成交写入工具。"
        "不要给出买入、卖出、加仓、目标价或仓位指令；用数据解释结构与风险。\n"
        "优先使用原生 function calling。若当前 Provider 没有返回原生工具调用，"
        '只能单独输出一个 JSON 对象，格式为 {"tool":"工具名","args":{...}}。'
        "严禁输出 DSML、XML 或其他工具标记；只能使用下列工具，不能虚构工具名。\n"
        "全市场因子分析应直接调用 analyze_factor 并省略 symbols；不得调用 quote_pool。\n"
        "可用工具及参数：" + json.dumps(prompt_tools, ensure_ascii=False, separators=(",", ":"))
    )


def _final_system() -> str:
    return (
        "根据上方工具返回的数据，用中文简洁回答用户的问题，列出具体结论和数据依据。"
        "如回答涉及短线观察池，必须逐股引用工具返回的 evidence 字段，"
        "候选的顺序、数量与内容必须与工具返回完全一致，不得增删或重排；"
        "短线池是确定性筛选的研究观察池、非投资建议，禁止荐股口吻和任何交易指令。"
        "market_state 与 t_research 必须按工具事实解释，不得改动状态、阈值或协议；"
        "不得声称复刻任何未公开公式，也不得把市场状态解释成直接买卖点。"
        "不要给出买入、卖出、加仓、目标价或仓位指令。"
        "只输出最终自然语言答案；严禁输出 JSON、DSML、XML 或任何工具调用标记。"
    )


def _parse_tool(text: str) -> dict | None:
    """解析 JSON 降级调用或 OpenAI-compatible Provider 返回的 DSML 调用。"""
    try:
        data = json.loads(text.strip())
    except Exception:
        data = None
    if isinstance(data, dict) and isinstance(data.get("tool"), str):
        args = data.get("args")
        if args is None or isinstance(args, dict):
            return {"tool": data["tool"], "args": args or {}}
    return _parse_dsml_tool(text)


def _parse_dsml_tool(text: str) -> dict | None:
    """将 GLM DSML 标记转成内部工具请求，避免把模型控制标记展示给用户。"""
    normalized = text.replace("｜", "|")
    invoke = _DSML_INVOKE_RE.search(normalized)
    if invoke is None:
        return None

    args: dict[str, Any] = {}
    for parameter in _DSML_PARAMETER_RE.finditer(invoke.group("body")):
        value = parameter.group("value").strip()
        if value.startswith(("[", "{")):
            try:
                args[parameter.group("name")] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        args[parameter.group("name")] = value
    return {"tool": invoke.group("name"), "args": args}


def _execute_tool(name: str, app_state: Any, args: dict) -> dict:
    """执行工具调用，返回结果 dict（出错时返回 {\"error\": ...}）。"""
    if name not in _ALLOWED_NAMES:
        return {"error": f"tool not allowed: {name}"}
    try:
        return agent_tools.call_tool(name, app_state, args)
    except Exception as exc:  # noqa: BLE001 — 工具失败应留在 tool_result，不能击穿整个 turn
        return {"error": agent_tools.sanitize_tool_error(exc)}


# ── AI 短线池输出边界：候选只能来自确定性工具结果 ─────────────────
_SHORT_POOL_TOOL_NAME = "screen_stock_pool"
_SHORT_POOL_PRESET_ID = "short_momentum_quality_v1"


def _short_pool_outcome(name: str, result: Any) -> dict | None:
    """识别成功的 short_momentum_quality_v1 工具结果，只保留确定性标量。

    命中条件是封套 ``preset.preset_id``（run_short_pool 专有字段）且
    ``status=success``；legacy 普通股票池与失败/出错的调用一律不命中。
    候选列表绝不进入返回值，最终文本模板因此无法引用任何候选。
    """
    if name != _SHORT_POOL_TOOL_NAME or not isinstance(result, dict):
        return None
    if result.get("status") != "success":
        return None
    preset = result.get("preset")
    if not isinstance(preset, dict) or preset.get("preset_id") != _SHORT_POOL_PRESET_ID:
        return None

    def _scalar(key: str) -> Any:
        value = result.get(key)
        return value if isinstance(value, (str, int, float)) else None

    return {key: _scalar(key) for key in ("pool_id", "as_of", "total", "count")}


def _short_pool_final_text(outcome: dict) -> str:
    """服务端确定性最终摘要：只引用标量事实，绝不枚举候选名称/代码/顺序。"""
    facts: list[str] = []
    if isinstance(outcome.get("pool_id"), str):
        facts.append(f"pool_id={outcome['pool_id']}")
    if isinstance(outcome.get("as_of"), str):
        facts.append(f"as_of={outcome['as_of']}")
    if isinstance(outcome.get("total"), int):
        facts.append(f"命中 {outcome['total']} 只")
    if isinstance(outcome.get("count"), int):
        facts.append(f"输出 {outcome['count']} 只")
    joined = "，".join(facts)
    lead = (
        f"AI 短线池已由固定确定性策略生成（{joined}）。"
        if joined
        else "AI 短线池已由固定确定性策略生成。"
    )
    return (
        lead + "候选的名称、代码与顺序以工具返回的结构化结果卡为准，本回答不枚举候选；"
        "该池仅用于研究观察，非投资建议。"
    )


async def run_agent_stream(
    messages: list[dict],
    app_state: Any,
    profile_id: str | None = None,
    *,
    generate_tool: Callable[
        ..., Awaitable[tuple[str | None, list[dict] | None]]
    ] = generate_ai_with_tools,
    generate: Callable[..., Awaitable[str]] = generate_ai_text,
    stream: Callable[..., Any] = stream_ai_text,
) -> AsyncIterator[str]:
    """Multi-round tool loop plus final streamed answer. Yields NDJSON payload strings.

    优先使用 OpenAI 原生 function calling（``generate_tool`` 传 ``tools=`` 参数）。
    对于不支持原生 tools 的 provider（Codex CLI / ACP），``generate_tool`` 返回
    ``(text, None)``，此时降级到 prompt 注入 JSON 模式（``_parse_tool``）。
    """
    tool_ctx: list[dict] = []
    short_pool_outcome: dict | None = None
    started_at = perf_counter()
    if not try_acquire_agent_slot():
        yield occupancy_error_line(started_at)
        return
    from app.services.ai_budgets import resolve_budget

    budget = resolve_budget("agent", max_tokens=1200)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            convo = [{"role": "system", "content": _tools_system()}, *messages, *tool_ctx]
            content, tool_calls = await generate_tool(
                convo,
                _OPENAI_TOOLS,
                profile_id=profile_id,
                temperature=budget.temperature,
                max_tokens=budget.max_tokens,
                timeout=budget.timeout,
            )

            if tool_calls:
                # ── 原生 function calling 路径 ──
                assistant_msg: dict = {"role": "assistant"}
                if content:
                    assistant_msg["content"] = content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls
                ]
                tool_ctx.append(assistant_msg)

                for tc in tool_calls:
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"]) if tc.get("arguments") else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    yield json.dumps(
                        {"type": "tool_call", "name": name, "args": args}, ensure_ascii=False
                    )
                    tool_started_at = perf_counter()
                    result = await asyncio.to_thread(_execute_tool, name, app_state, args)
                    yield json.dumps(
                        {
                            "type": "tool_result",
                            "name": name,
                            "result": result,
                            "elapsed_ms": round((perf_counter() - tool_started_at) * 1000, 1),
                        },
                        ensure_ascii=False,
                    )
                    tool_ctx.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                    if (outcome := _short_pool_outcome(name, result)) is not None:
                        short_pool_outcome = outcome
                        # 成功的固定短线池是终端工具结果：候选已由结构化卡片承载，
                        # 不再执行同批或后续模型追加的工具调用，避免其失败/输出被摘要门控吞没。
                        break
                if short_pool_outcome is not None:
                    break
                continue

            # ── 无 tool_calls：降级解析或直接回答 ──
            if content:
                tool_req = _parse_tool(content)
                if tool_req is not None:
                    name = tool_req["tool"]
                    yield json.dumps(
                        {"type": "tool_call", "name": name, "args": tool_req["args"]},
                        ensure_ascii=False,
                    )
                    tool_started_at = perf_counter()
                    result = await asyncio.to_thread(
                        _execute_tool, name, app_state, tool_req["args"]
                    )
                    yield json.dumps(
                        {
                            "type": "tool_result",
                            "name": name,
                            "result": result,
                            "elapsed_ms": round((perf_counter() - tool_started_at) * 1000, 1),
                        },
                        ensure_ascii=False,
                    )
                    tool_ctx += [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": "Tool result:\n"
                            + json.dumps(result, ensure_ascii=False, default=str),
                        },
                    ]
                    if (outcome := _short_pool_outcome(name, result)) is not None:
                        short_pool_outcome = outcome
                        break
                    continue
            break

        answer_msgs = [
            {"role": "system", "content": _final_system()},
            *messages,
            *tool_ctx,
        ]
        final_budget = resolve_budget("agent", temperature=0.4)
        if short_pool_outcome is not None:
            # 输出边界强制：短线池候选只能经 tool_result/前端结构化卡片表达；
            # 模型最终文本可能编造/删减/重排候选，一律不进入 delta 流。
            yield json.dumps(
                {"type": "delta", "content": _short_pool_final_text(short_pool_outcome)},
                ensure_ascii=False,
            )
        else:
            async for delta in stream(
                answer_msgs,
                profile_id=profile_id,
                temperature=final_budget.temperature,
                max_tokens=final_budget.max_tokens,
                timeout=final_budget.timeout,
            ):
                if delta:
                    yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
        yield json.dumps(
            {
                "type": "done",
                "elapsed_ms": round((perf_counter() - started_at) * 1000, 1),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        message = agent_tools.sanitize_tool_error(exc)
        yield json.dumps(
            {
                "type": "error",
                "message": f"Agent 失败: {message}",
                "elapsed_ms": round((perf_counter() - started_at) * 1000, 1),
            },
            ensure_ascii=False,
        )
    finally:
        release_agent_slot()
