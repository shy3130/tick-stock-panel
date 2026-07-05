# P7 Agent 对话（完备版）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤用复选框（`- [ ]`）跟踪。设计见 `docs/superpowers/specs/2026-07-04-vibe-frontend-porting-design.md`（子项目 2）。

**目标：** 新增 AI 助手对话页——多轮对话 + 流式回答 + 多轮工具循环（最多 5 轮），复用现有多 AI 配置。

**架构：** 后端加一个 `agent_loop` 服务（多轮工具决策 → 执行 → 回喂 → 最终 `stream_ai_text` 流式回答），经 `POST /api/agent/stream` 以 NDJSON 推给前端；旧 `POST /api/agent/chat` 保留。前端新建 `/agent` 页，复用 NDJSON async-generator 读取 + `AiProviderSelector` + localStorage 会话持久化。

**技术栈：** 后端 FastAPI + async generator；前端 React + TS + Vite + @tanstack/react-query。

## 当前实现状态（2026-07-04）

P7 MVP 已落地，且额外补了 Vibe gap 文档里的低风险项：

- 已实现：`agent_loop` 多轮工具循环、`POST /api/agent/stream` NDJSON、`run_backtest` 白名单排除、前端 `/agent`、路由和菜单。
- 已实现：服务端 session list/create/rename/delete、消息持久化、桌面侧栏、移动端下拉、`/agent?session=<id>` 恢复。
- 已实现：Welcome 示例、页内 `AgentAvatar`/`MessageBubble`/`ToolTraceList` 拆分、assistant Markdown 渲染、导出 Markdown、错误重试、前端 Abort 停止当前流。
- 已实现：P7.3 最小取消闭环：session 流发 `attempt_start`，`POST /api/agent/attempts/{attempt_id}/cancel` 标记取消；前端停止按钮先请求 cancel 再中断本地流。该实现仅为内存态，不提供跨进程 replay。
- 已实现：附件上下文轻量版，复用 `/api/documents/read`；附件全文只进本轮模型上下文，session 仅持久化用户原问题（`display_content`）。
- 已验证：`cd backend && uv run --extra dev pytest tests/api/test_agent_stream.py tests/services/test_agent_loop.py -q` → `10 passed`；`cd frontend && pnpm tsc --noEmit` → 通过。
- 未做/不在本计划内：完整持久 attempt runtime、reload 后恢复进行中任务、tool progress/heartbeat、Goal、Swarm、agent 回测 run card。
- 未提交：本计划里的 commit 步骤需要用户明确授权后执行。

## Global Constraints

- 后端测试：`cd backend && uv run --extra dev pytest <path> -q`；前端 `cd frontend && pnpm tsc --noEmit` 必须全绿（`noUnusedLocals=true`，勿留未用变量）。
- **流式用 `POST /api/agent/stream` + NDJSON**（对齐 `stock_analysis.py:155-176` 的 `StreamingResponse(media_type="application/x-ndjson")`，每行 `json.dumps({...})+"\n"`）；**不用 GET SSE**。
- NDJSON 事件类型固定：`{"type":"tool_call","name","args"}`、`{"type":"tool_result","name","result"}`、`{"type":"delta","content"}`、`{"type":"done"}`、`{"type":"error","message"}`（`delta/error/done` 沿用现有约定）。
- 底层复用 `app.services.ai_provider.generate_ai_text(messages, *, profile_id, temperature, max_tokens)`（非流式，用于工具决策）和 `stream_ai_text(messages, *, profile_id, ...) -> AsyncIterator[str]`（流式，用于最终回答）。
- 工具执行走 `app.services.agent_tools.call_tool(name, app_state, args) -> dict`；工具列表 `agent_tools.TOOLS`（7 个，全 read_only）。
- **工具循环上限 5 轮**。**（codex review High）`run_backtest` 排除出 agent 白名单**——它虽 read_only 但可跑全市场回测（`strategy_id` only、`symbols` 可 None、默认 180 天，`agent_tools.py:109-126`），5 轮循环里最坏 5 次全市场回测。P7 用 `ALLOWED_AGENT_TOOLS`（= `TOOLS` 去掉 `run_backtest`）：既不放进 system prompt 的工具清单，也在执行前拦截（模型硬要调则返回 error 结果，不执行）。策略在 `agent_loop` 本地化，不改 `agent_tools.py`。
- **（codex review Medium）provider 流式差异**：`openai_compat` 真流式；`codex_cli` 命令退出后 yield 一整块；**`acp` 当前不可用**——走 `generate_ai_text` 会 `raise RuntimeError("ACP AI 配置尚未接入")`（`ai_provider.py:105-120`）。P7 不实现 ACP；用户选到 ACP profile 时，`run_agent_stream` 的 try/except 会把该异常兜成 `{"type":"error"}` 事件推给前端（前端照常显示错误，不崩）。
- 前端多轮：每次请求发**完整 `messages[]` 历史**（后端无状态）。
- 前端流式消费复用 `api.ts` 现有 async-generator 范式（`getReader()`+`TextDecoder`+按 `\n` 分行+`JSON.parse`，见 `financialAnalyzeStream` 约 `api.ts:1571-1598`）。
- 样式复用现有组件与 Tailwind token；不引新 UI 风格。commit 需用户授权；永不 push。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/agent_loop.py` | 多轮工具循环 + 流式回答，yield NDJSON 行 | 创建 |
| `backend/tests/services/test_agent_loop.py` | agent_loop 单测（注入 fake AI） | 创建 |
| `backend/app/api/agent.py` | 加 `POST /api/agent/stream` | 修改 |
| `backend/tests/api/test_agent_stream.py` | 端点集成测试 | 创建 |
| `frontend/src/lib/api.ts` | 加 `agentStream()` async gen + `agentTools()` + 类型 | 修改 |
| `frontend/src/pages/Agent.tsx` | 对话页 | 创建 |
| `frontend/src/lib/agentChatStore.ts` | 会话 localStorage 持久化 | 创建 |
| `frontend/src/router.tsx` | 加 `/agent` 路由 | 修改 |
| `frontend/src/components/Layout.tsx` | 加"AI 助手"菜单项 | 修改 |

---

### Task 1：后端 agent_loop 服务

**Files:**
- Create: `backend/app/services/agent_loop.py`
- Test: `backend/tests/services/test_agent_loop.py`

**Interfaces:**
- Produces: `run_agent_stream(messages: list[dict], app_state, profile_id: str | None = None, *, generate=..., stream=...) -> AsyncIterator[str]` — yield NDJSON 行（**不含**结尾换行）。`generate`/`stream` 可注入以便测试。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/services/test_agent_loop.py
import json
import pytest

from app.services.agent_loop import run_agent_stream


class _FakeState:
    """call_tool 里 list_strategies 走 strategy_engine；这里给个最小桩。"""
    class _Engine:
        def list_strategies(self):
            return []
    strategy_engine = _Engine()


async def _collect(agen):
    return [json.loads(line) async for line in agen]


@pytest.mark.asyncio
async def test_agent_loop_single_tool_then_answer():
    calls = {"n": 0}

    async def fake_generate(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool":"list_strategies","args":{}}'
        return "no more tools"  # 不会被用到（第二轮直接进 stream）

    async def fake_stream(messages, **kw):
        for chunk in ["答", "案"]:
            yield chunk

    events = await _collect(run_agent_stream(
        [{"role": "user", "content": "有哪些策略"}], _FakeState(),
        generate=fake_generate, stream=fake_stream,
    ))
    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "delta", "delta", "done"]
    assert events[0]["name"] == "list_strategies"
    assert events[1]["result"] == {"strategies": []}
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "答案"


@pytest.mark.asyncio
async def test_agent_loop_direct_answer_no_tool():
    async def fake_generate(messages, **kw):
        return "直接回答，无需工具"  # 非 JSON → 不是工具请求

    async def fake_stream(messages, **kw):
        yield "你好"

    events = await _collect(run_agent_stream(
        [{"role": "user", "content": "hi"}], _FakeState(),
        generate=fake_generate, stream=fake_stream,
    ))
    assert [e["type"] for e in events] == ["delta", "done"]


@pytest.mark.asyncio
async def test_agent_loop_caps_at_five_rounds():
    async def fake_generate(messages, **kw):
        return '{"tool":"list_strategies","args":{}}'  # 永远请求工具

    async def fake_stream(messages, **kw):
        yield "最终"

    events = await _collect(run_agent_stream(
        [{"role": "user", "content": "loop"}], _FakeState(),
        generate=fake_generate, stream=fake_stream,
    ))
    assert sum(1 for e in events if e["type"] == "tool_call") == 5  # 上限 5
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_rejects_excluded_and_unknown_tools_then_done():
    """run_backtest(白名单外) 和未知 tool 都返回 error 结果，最终仍到 done，不执行工具。"""
    seq = iter(['{"tool":"run_backtest","args":{"strategy_id":"x"}}',
                '{"tool":"nope","args":{}}'])

    async def fake_generate(messages, **kw):
        try:
            return next(seq)
        except StopIteration:
            return "普通回答"  # 非 JSON → 退出工具循环

    async def fake_stream(messages, **kw):
        yield "好"

    events = await _collect(run_agent_stream(
        [{"role": "user", "content": "跑个全市场回测"}], _FakeState(),
        generate=fake_generate, stream=fake_stream,
    ))
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert all("error" in r["result"] for r in results)   # run_backtest 被拒 + 未知 tool 被拒
    assert events[-1]["type"] == "done"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --extra dev pytest tests/services/test_agent_loop.py -q`
预期：FAIL（`ModuleNotFoundError: app.services.agent_loop`）。

- [ ] **Step 3: 写实现**

```python
# backend/app/services/agent_loop.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Awaitable, Callable

from app.services import agent_tools
from app.services.ai_provider import generate_ai_text, stream_ai_text

MAX_TOOL_ROUNDS = 5

# codex review High：run_backtest 可跑全市场回测，排除出 agent 白名单。
_EXCLUDED_TOOLS = {"run_backtest"}
ALLOWED_AGENT_TOOLS = [t for t in agent_tools.TOOLS if t["name"] not in _EXCLUDED_TOOLS]
_ALLOWED_NAMES = {t["name"] for t in ALLOWED_AGENT_TOOLS}


def _tools_system() -> str:
    return (
        "You are TickFlow Stock Panel assistant. If you need a tool, reply with ONLY JSON "
        '{"tool":"<name>","args":{...}}. Otherwise answer the user directly. Available tools: '
        + json.dumps(ALLOWED_AGENT_TOOLS, ensure_ascii=False)
    )


def _parse_tool(text: str) -> dict | None:
    try:
        data = json.loads(text.strip())
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and isinstance(data.get("tool"), str):
        args = data.get("args")
        if args is None or isinstance(args, dict):
            return {"tool": data["tool"], "args": args or {}}
    return None


async def run_agent_stream(
    messages: list[dict],
    app_state: Any,
    profile_id: str | None = None,
    *,
    generate: Callable[..., Awaitable[str]] = generate_ai_text,
    stream: Callable[..., Any] = stream_ai_text,
) -> AsyncIterator[str]:
    """多轮工具决策循环 + 最终流式回答，yield NDJSON 行（不含结尾换行）。"""
    tool_ctx: list[dict] = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            convo = [{"role": "system", "content": _tools_system()}, *messages, *tool_ctx]
            decision = await generate(convo, profile_id=profile_id, temperature=0.2, max_tokens=1200)
            tr = _parse_tool(decision)
            if tr is None:
                break
            yield json.dumps({"type": "tool_call", "name": tr["tool"], "args": tr["args"]}, ensure_ascii=False)
            if tr["tool"] not in _ALLOWED_NAMES:
                result = {"error": f"tool not allowed: {tr['tool']}"}
            else:
                try:
                    result = agent_tools.call_tool(tr["tool"], app_state, tr["args"])
                except ValueError as e:
                    result = {"error": str(e)}
            yield json.dumps({"type": "tool_result", "name": tr["tool"], "result": result}, ensure_ascii=False)
            tool_ctx += [
                {"role": "assistant", "content": decision},
                {"role": "user", "content": "Tool result:\n" + json.dumps(result, ensure_ascii=False)},
            ]

        answer_msgs = [
            {"role": "system", "content": "Answer the user concisely using any tool results above."},
            *messages,
            *tool_ctx,
        ]
        async for delta in stream(answer_msgs, profile_id=profile_id, temperature=0.4, max_tokens=1600):
            if delta:
                yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        yield json.dumps({"type": "error", "message": f"Agent 失败: {e}"}, ensure_ascii=False)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --extra dev pytest tests/services/test_agent_loop.py -q`
预期：PASS（4 passed，含 run_backtest/未知工具被拒仍到 done 的边界）。（仓库 `pyproject.toml` 已配 `asyncio_mode = "auto"` 且依赖含 `pytest-asyncio`，现有测试如 `tests/api/test_trade_journal.py` 也带 `@pytest.mark.asyncio`，本测试写法一致，无需额外配置。）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_loop.py backend/tests/services/test_agent_loop.py
git commit -m "feat(agent): multi-round tool loop + streaming answer service"
```

---

### Task 2：后端 `POST /api/agent/stream` 端点

**Files:**
- Modify: `backend/app/api/agent.py`
- Test: `backend/tests/api/test_agent_stream.py`

**Interfaces:**
- Consumes: Task 1 的 `run_agent_stream`。
- Produces: `POST /api/agent/stream` NDJSON 流；入参 `{messages: [{role,content}], profile_id?}`。

- [ ] **Step 1: 写失败测试**（monkeypatch `run_agent_stream` 避免真调 LLM）

```python
# backend/tests/api/test_agent_stream.py
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agent as agent_api


def _client(monkeypatch):
    async def fake_run(messages, app_state, profile_id=None, **kw):
        yield json.dumps({"type": "tool_call", "name": "list_strategies", "args": {}})
        yield json.dumps({"type": "tool_result", "name": "list_strategies", "result": {"strategies": []}})
        yield json.dumps({"type": "delta", "content": "答案"})
        yield json.dumps({"type": "done"})
    monkeypatch.setattr(agent_api, "run_agent_stream", fake_run, raising=False)
    app = FastAPI()
    app.include_router(agent_api.router)
    app.state.repo = object()
    return TestClient(app)


def test_agent_stream_returns_ndjson_events(monkeypatch):
    client = _client(monkeypatch)
    with client.stream("POST", "/api/agent/stream",
                       json={"messages": [{"role": "user", "content": "hi"}]}) as resp:
        assert resp.status_code == 200
        assert "x-ndjson" in resp.headers["content-type"]
        lines = [json.loads(l) for l in resp.iter_lines() if l.strip()]
    assert [e["type"] for e in lines] == ["tool_call", "tool_result", "delta", "done"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --extra dev pytest tests/api/test_agent_stream.py -q`
预期：FAIL（404）。

- [ ] **Step 3: 写实现**（`backend/app/api/agent.py`，顶部加 import，末尾加端点）

```python
# 顶部 import 区加：
from fastapi.responses import StreamingResponse
from app.services.agent_loop import run_agent_stream

# 末尾加：
class AgentStreamIn(BaseModel):
    messages: list[dict]
    profile_id: str | None = None


@router.post("/stream")
async def chat_stream(req: AgentStreamIn, request: Request):
    """多轮 + 流式 + 工具循环的 Agent 对话（NDJSON）。"""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages empty")

    async def gen():
        async for line in run_agent_stream(req.messages, request.app.state, req.profile_id):
            yield line + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

> 注：为让 Task 2 测试能 monkeypatch，端点内**直接引用模块级 `run_agent_stream`**（顶部 import 进来的名字），不要在函数内 `from ... import`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --extra dev pytest tests/api/test_agent_stream.py -q`
预期：PASS（1 passed）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/agent.py backend/tests/api/test_agent_stream.py
git commit -m "feat(api): POST /api/agent/stream NDJSON agent endpoint"
```

---

### Task 3：前端 api.agentStream + agentTools + 类型

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Produces:
  - `api.agentTools()` → `{ tools: AgentTool[] }`。
  - `api.agentStream(messages, profileId?)` → async generator，yield `AgentEvent`。
  - 类型 `AgentMsg`、`AgentTool`、`AgentEvent`。

- [ ] **Step 1: 加类型与调用**（`api` 对象内）

```ts
export interface AgentMsg { role: 'user' | 'assistant'; content: string }
export interface AgentTool { name: string; description: string; read_only?: boolean }
export type AgentEvent =
  | { type: 'tool_call'; name: string; args: Record<string, any> }
  | { type: 'tool_result'; name: string; result: any }
  | { type: 'delta'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string }

// api 对象内新增：
agentTools: () => request<{ tools: AgentTool[] }>('/api/agent/tools'),

async *agentStream(messages: AgentMsg[], profileId?: string): AsyncGenerator<AgentEvent> {
  const res = await fetch('/api/agent/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, ...(profileId ? { profile_id: profileId } : {}) }),
  })
  if (!res.ok) {
    let detail = ''
    try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }
  if (!res.body) throw new Error('响应无 body')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      const s = line.trim()
      if (!s) continue
      try { yield JSON.parse(s) as AgentEvent } catch { /* ignore */ }
    }
  }
  if (buf.trim()) { try { yield JSON.parse(buf.trim()) as AgentEvent } catch { /* ignore */ } }
},
```

- [ ] **Step 2: tsc + Commit**

```bash
cd frontend && pnpm tsc --noEmit
git add src/lib/api.ts && git commit -m "feat(ui): agentStream async generator + agentTools + types"
```
预期：tsc EXIT 0。

---

### Task 4：前端会话持久化 helper

**Files:**
- Create: `frontend/src/lib/agentChatStore.ts`

**Interfaces:**
- Produces: `loadAgentChat(): AgentMsg[]`、`saveAgentChat(msgs: AgentMsg[]): void`、`clearAgentChat(): void`。

- [ ] **Step 1: 写实现**

```ts
// frontend/src/lib/agentChatStore.ts
import type { AgentMsg } from '@/lib/api'

const KEY = 'agent_chat_history'

export function loadAgentChat(): AgentMsg[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr : []
  } catch { return [] }
}

export function saveAgentChat(msgs: AgentMsg[]): void {
  try { localStorage.setItem(KEY, JSON.stringify(msgs.slice(-50))) } catch { /* ignore */ }
}

export function clearAgentChat(): void {
  try { localStorage.removeItem(KEY) } catch { /* ignore */ }
}
```

- [ ] **Step 2: tsc + Commit**

```bash
cd frontend && pnpm tsc --noEmit
git add src/lib/agentChatStore.ts && git commit -m "feat(ui): agent chat localStorage persistence"
```

---

### Task 5：前端 Agent 对话页 + 路由 + 菜单

**Files:**
- Create: `frontend/src/pages/Agent.tsx`
- Modify: `frontend/src/router.tsx`、`frontend/src/components/Layout.tsx`

**Interfaces:**
- Consumes: Task 3 的 `api.agentStream`、`AgentMsg`、`AgentEvent`；Task 4 的 `loadAgentChat/saveAgentChat/clearAgentChat`；`AiProviderSelector`（`@/components/AiProviderSelector`，props `{entry,value,onChange}`）；`PageHeader`。

- [ ] **Step 1: 写页面组件**

```tsx
// frontend/src/pages/Agent.tsx
import { useEffect, useRef, useState } from 'react'
import { Send, Trash2, Loader2, Wrench } from 'lucide-react'
import { api, type AgentMsg } from '@/lib/api'
import { loadAgentChat, saveAgentChat, clearAgentChat } from '@/lib/agentChatStore'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { PageHeader } from '@/components/PageHeader'

interface ToolTrace { name: string; args?: any; result?: any }
interface ChatMsg extends AgentMsg { tools?: ToolTrace[] }

export function Agent() {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadAgentChat())
  const [input, setInput] = useState('')
  const [profileId, setProfileId] = useState<string>()
  const [streaming, setStreaming] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => { saveAgentChat(msgs.map(m => ({ role: m.role, content: m.content }))) }, [msgs])
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }) }, [msgs, streaming])

  async function send() {
    const text = input.trim()
    if (!text || streaming) return
    const history: AgentMsg[] = [...msgs.map(m => ({ role: m.role, content: m.content })), { role: 'user', content: text }]
    setMsgs(prev => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '', tools: [] }])
    setInput('')
    setStreaming(true)
    try {
      for await (const evt of api.agentStream(history, profileId)) {
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role !== 'assistant') return prev
          if (evt.type === 'delta') last.content += evt.content
          else if (evt.type === 'tool_call') last.tools = [...(last.tools ?? []), { name: evt.name, args: evt.args }]
          else if (evt.type === 'tool_result') {
            const t = (last.tools ?? []).slice()
            const i = t.map(x => x.name).lastIndexOf(evt.name)
            if (i >= 0) t[i] = { ...t[i], result: evt.result }
            last.tools = t
          } else if (evt.type === 'error') last.content += `\n[错误] ${evt.message}`
          return next
        })
      }
    } catch (e) {
      setMsgs(prev => {
        const next = [...prev]; const last = next[next.length - 1]
        if (last?.role === 'assistant') last.content += `\n[请求失败] ${(e as Error).message}`
        return next
      })
    } finally { setStreaming(false) }
  }

  return (
    <div className="p-4 flex flex-col h-[calc(100vh-3rem)]">
      <div className="flex items-center justify-between">
        <PageHeader title="AI 助手" subtitle="多轮对话 · 可调用面板数据工具" />
        <div className="flex items-center gap-2">
          <AiProviderSelector entry="agent" value={profileId} onChange={setProfileId} />
          <button onClick={() => { clearAgentChat(); setMsgs([]) }}
            className="h-7 px-2 rounded-btn bg-elevated text-muted text-xs flex items-center gap-1 hover:text-secondary">
            <Trash2 className="h-3.5 w-3.5" />清空
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-auto space-y-3 py-3">
        {msgs.length === 0 && <div className="text-xs text-muted text-center pt-10">问点什么，比如"有哪些内置策略""看下 600519 最近走势"</div>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
            <div className={`max-w-[80%] rounded-card px-3 py-2 text-xs whitespace-pre-wrap ${
              m.role === 'user' ? 'bg-accent/20 text-foreground' : 'bg-surface border border-border text-foreground'}`}>
              {m.tools && m.tools.length > 0 && (
                <div className="mb-1.5 space-y-1">
                  {m.tools.map((t, j) => (
                    <details key={j} className="rounded bg-elevated/60 px-2 py-1">
                      <summary className="cursor-pointer text-[11px] text-muted flex items-center gap-1">
                        <Wrench className="h-3 w-3" />{t.name}
                      </summary>
                      <pre className="mt-1 text-[10px] text-muted overflow-auto max-h-40">{JSON.stringify(t.result ?? t.args, null, 2)}</pre>
                    </details>
                  ))}
                </div>
              )}
              {m.content || (m.role === 'assistant' && streaming && i === msgs.length - 1
                ? <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" /> : '')}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-end gap-2 border-t border-border pt-3">
        <textarea
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
          rows={2} placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
          className="flex-1 resize-none rounded-input border border-border bg-elevated px-2.5 py-2 text-xs" />
        <button disabled={streaming || !input.trim()} onClick={send}
          className="h-9 px-4 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 flex items-center gap-1.5">
          {streaming ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}发送
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: 注册路由**（`frontend/src/router.tsx`）

顶部加 `import { Agent } from './pages/Agent'`；`children` 数组加 `{ path: 'agent', element: <Agent /> },`。

- [ ] **Step 3: 加菜单项**（`frontend/src/components/Layout.tsx`）

nav 数组加 `{ to: '/agent', label: 'AI 助手', icon: Bot },`；确保 `Bot` 在 `lucide-react` import 里。

- [ ] **Step 4: tsc + 手测**

Run: `cd frontend && pnpm tsc --noEmit`（EXIT 0）。手测 `http://m4max.wf:3011/agent`：发"有哪些内置策略"→ 出现 `list_strategies` 工具卡 + 流式回答；多轮追问保留上下文；切换 AI 配置生效；清空后历史清除；刷新后历史仍在（localStorage）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Agent.tsx frontend/src/router.tsx frontend/src/components/Layout.tsx
git commit -m "feat(ui): agent chat page + route + nav"
```

---

## 自检（规格覆盖）

- ✅ POST /api/agent/stream NDJSON（Task 2，spec H2）
- ✅ 多轮历史（前端发完整 messages[]，Task 5）+ 无状态后端（Task 1）
- ✅ 多轮工具循环上限 5（Task 1 `MAX_TOOL_ROUNDS`，含 caps 测试）
- ✅ **run_backtest 排除出白名单 + 拦截（Task 1 `ALLOWED_AGENT_TOOLS`，含被拒测试，codex High）**
- ✅ 流式回答复用 `stream_ai_text`（Task 1）
- ✅ 工具调用卡片（Task 5 tools details）
- ✅ 复用 AiProviderSelector `entry="agent"`（Task 5）
- ✅ 会话本地持久化 + 清空（Task 4 + Task 5）
- ✅ 旧 `/chat` 保留（Task 2 只新增 `/stream`，不删 `/chat`）
- 依赖顺序：Task 1 → 2 → 3 → 4 → 5，逐个可独立测试（后端 1/2 可先于前端 3/4/5）。

## 已知限制（codex review 后定稿）
- `run_agent_stream` 每轮"工具决策"用非流式 `generate_ai_text`，仅最终回答流式——因为工具请求需要完整 JSON 才能解析，无法边流边判定。robust vs token-streaming 的取舍。
- **provider 流式**：`openai_compat` 真流式；`codex_cli` 整块；**`acp` 当前不可用**（`generate_ai_text` 对 ACP raise，异常兜成 error 事件）。P7 不实现 ACP。
- **`run_backtest` 不对 agent 开放**（成本闸门，codex High）；若后续要让 agent 跑回测，需先给它加 symbols 必填 + 数量/日期跨度 cap + 每对话最多 1 次。
- 工具决策与最终回答是两段 prompt；若某 provider 不擅长"回 JSON 或直接答"的二选一协议，可能多绕工具轮。上限 5 轮 + 未知/被拒工具返回 error 结果兜底（已有测试）。
