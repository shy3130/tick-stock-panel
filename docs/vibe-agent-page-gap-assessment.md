# Vibe-Trading Agent 页面移植缺口评估

## 结论

当前 `tickflow-stock-panel` 的 `/agent` 已从 Agent Chat MVP 推进到可用会话版：多轮消息、NDJSON 流式回答、轻量工具调用卡片、服务端持久 session、URL `?session=` 恢复、Markdown 渲染、导出/重试和 AI profile 选择器。它还不是 `../Vibe-Trading` 的完整 Agent Runtime。

`../Vibe-Trading` 的 `127.0.0.1:8899/agent` 是服务端持久会话 + SSE 运行态 + 工具进度 + Goal + Swarm + 交易连接器安全面板的复合工作台。很多功能不能只搬前端组件，需要先补后端 `/sessions/*`、SSE event bus、Goal、Swarm 或 live runtime API。

## 当前已移植范围

| 能力 | 当前实现 | 证据 |
|---|---|---|
| 多轮聊天 | 前端发送完整 `messages[]` 历史，后端无状态处理 | `frontend/src/pages/Agent.tsx:34-49`、`backend/app/api/agent.py:66-79` |
| 流式回答 | `POST /api/agent/stream` 返回 NDJSON | `backend/app/api/agent.py:66-79` |
| 轻量工具循环 | `run_agent_stream` 最多 5 轮，排除 `run_backtest` | `backend/app/services/agent_loop.py:9-14`、`:38-85` |
| 工具卡片 | 前端展示 `tool_call` / `tool_result` JSON | `frontend/src/pages/Agent.tsx:121-132` |
| 服务端 session | list/create/rename/delete + message 持久化 | `backend/app/services/agent_sessions.py`、`backend/app/api/agent.py` |
| Session 入口 | 桌面侧栏会话列表 + 移动端下拉；支持 URL `?session=<id>` 恢复 | `frontend/src/pages/Agent.tsx` |
| 本地草稿 | 未选 session 时仍用 `localStorage` 保存最近消息 | `frontend/src/lib/agentChatStore.ts`、`frontend/src/pages/Agent.tsx:140-143` |
| Markdown 渲染 | assistant 消息复用现有 `MarkdownRenderer` | `frontend/src/pages/Agent.tsx:80-82` |
| Welcome / 取消 / 重试 / 导出 | 空态示例入口、服务端 attempt id + cancel endpoint、前端 Abort 停止当前流、错误重试、Markdown 导出 | `backend/app/api/agent.py`、`frontend/src/pages/Agent.tsx:103-126`、`:197-260`、`:288-314` |
| 附件上下文 | 复用 `/api/documents/read` 读取 txt/md/csv/xlsx/xls/pdf，作为本轮只读上下文发送；session 仅持久化用户原问题 | `backend/app/api/documents.py`、`frontend/src/pages/Agent.tsx`、`backend/app/api/agent.py` |
| AI profile 选择 | 使用 `AiProviderSelector entry="agent"` | `frontend/src/pages/Agent.tsx:350` |

## 主要缺口

### 1. 持久 Session 体系

Vibe 有服务端 session 列表、新建、删除、重命名、URL `?session=` 切换、历史加载和侧边栏会话列表。panel 已补服务端 session、URL 恢复和桌面侧栏；移动端保留下拉。

- Vibe API：`../Vibe-Trading/frontend/src/lib/api.ts:97-103`
- Vibe 侧边栏：`../Vibe-Trading/frontend/src/components/layout/Layout.tsx:115-207`
- Vibe 页面加载历史：`../Vibe-Trading/frontend/src/pages/Agent.tsx:349-409`
- 当前 panel：已有 `/api/agent/sessions`、桌面侧栏和移动端下拉；不是 Vibe 的全局 Layout 侧栏，但功能入口已覆盖。

**迁移判断：已落地。**
服务端 session 已足够承载后续 attempt、Goal、Swarm；不再为了形态搬 Vibe 全局侧边栏。

### 2. SSE 运行态与断线恢复

Vibe 通过 `/sessions/{sid}/events` 建立 SSE，支持 reconnect、Last-Event-ID、`replay=active`、attempt 生命周期事件和连接状态 banner。

- Vibe SSE URL：`../Vibe-Trading/frontend/src/lib/api.ts:125-129`
- Vibe SSE hook：`../Vibe-Trading/frontend/src/hooks/useSSE.ts`
- Vibe 事件处理：`../Vibe-Trading/frontend/src/pages/Agent.tsx:311-733`
- 当前 panel：仍是一次性 `POST /api/agent/stream` NDJSON；已补最小 `attempt_start` + cancel endpoint，但没有持久 attempt 状态、SSE replay 或断线恢复。

**迁移判断：高价值，需后端先补。**
不建议直接把 Vibe 前端 SSE 组件搬进来，否则没有对应事件源。

### 3. 工具进度、心跳和 ETA

Vibe 支持 `tool_heartbeat`、`tool_progress`、工具运行中 ETA、进度条和完成摘要。

- Vibe 事件处理：`../Vibe-Trading/frontend/src/pages/Agent.tsx:471-539`
- Vibe 进度组件：`../Vibe-Trading/frontend/src/components/chat/ToolProgressIndicator.tsx`
- 当前 panel：只在最终 NDJSON 里展示工具调用和结果，不能显示长任务进度。

**迁移判断：中高价值。**
当前已定 `POST /api/agent/stream` + NDJSON。若继续保持 NDJSON，可先扩展 `tool_progress` 事件；若要 Vibe 式断线恢复，则另立 P7.3 attempt runtime，而不是直接把前端 SSE 组件搬进来。

### 4. WelcomeScreen 示例入口和能力展示

Vibe 的空态不是一句提示，而是按类别展示示例 prompt 和能力 chip。

- Vibe 组件：`../Vibe-Trading/frontend/src/components/chat/WelcomeScreen.tsx:156-226`
- 当前 panel：已有按 panel 能力重写的空态示例入口，`frontend/src/pages/Agent.tsx:103-126`。

**迁移判断：已落地。**

### 5. 消息组件体系

Vibe 拆了 `AgentAvatar`、`MessageBubble`、`ThinkingTimeline`、`ConversationTimeline`、`RunCompleteCard`、`SwarmStatusCard` 等组件。

- Vibe imports：`../Vibe-Trading/frontend/src/pages/Agent.tsx:11-19`
- Vibe 消息类型：`../Vibe-Trading/frontend/src/types/agent.ts`
- 当前 panel：已在 `frontend/src/pages/Agent.tsx` 内拆出轻量 `AgentAvatar`、`ToolTraceList`、`MessageBubble`、`WelcomeScreen`；尚未拆成独立文件。

**迁移判断：轻量拆分已落地，独立文件暂缓。**
`RunCompleteCard` / `SwarmStatusCard` 依赖后端能力，继续暂缓。

### 6. Markdown 渲染

Vibe 的 assistant 消息支持 Markdown 渲染，真实 AI 回答里常见 `##` 标题、`**加粗**`、编号列表和表格。panel 已复用现有 `MarkdownRenderer` 渲染 assistant 消息。

- Vibe 消息组件：`../Vibe-Trading/frontend/src/components/chat/MessageBubble.tsx`
- 当前 Agent Markdown 渲染：`frontend/src/pages/Agent.tsx:80-82`
- panel 已有零依赖 Markdown 子集渲染器：`frontend/src/components/financials/MarkdownRenderer.tsx`，已被个股分析、财务分析和复盘页复用。

**迁移判断：已落地。**
当前先用零依赖 `MarkdownRenderer`；若要完整 GFM，再引入 `react-markdown + remark-gfm`，`rehype-highlight` 只在确实需要代码块高亮时加入。

### 7. 取消、重试、导出

Vibe 支持取消当前 attempt、错误重试和导出 Markdown。

- 取消：`../Vibe-Trading/frontend/src/pages/Agent.tsx:937-952`
- 重试：`../Vibe-Trading/frontend/src/pages/Agent.tsx:1035-1050`
- 导出：`../Vibe-Trading/frontend/src/pages/Agent.tsx:1052-1078`
- 当前 panel：已有错误重试、Markdown 导出、前端 Abort 停止当前流，并补了服务端 `attempt_start` + cancel endpoint；仍没有持久 replay。

**迁移判断：中高价值。**
当前取消是最小闭环，不是完整 Agent Runtime。可恢复状态、reload 后仍可见、历史 attempt replay 仍需要 session attempt 模型。

### 8. 文件上传附件

Vibe 支持上传文件，带后缀黑名单和 50 MB 大小限制，然后把文件路径注入 prompt。

- Vibe upload API：`../Vibe-Trading/frontend/src/lib/api.ts:70-77`
- Vibe 前端校验：`../Vibe-Trading/frontend/src/pages/Agent.tsx:1080-1108`
- 当前 panel：已有 Agent 附件上传入口，复用 `document_reader` 的大小/文本截断边界；附件文本作为本轮只读上下文注入模型请求，不持久化文件，也不把附件全文写入 session 历史。

**迁移判断：轻量版已落地。**
没有照搬 Vibe 的 `/upload` 文件路径注入；当前不让 agent 任意读文件，只读用户显式上传后由 `document_reader` 截断出的文本。

### 9. Goal / 研究目标模式

Vibe 有研究目标创建、证据、进度、继续、编辑、取消。

- Vibe Goal API：`../Vibe-Trading/frontend/src/lib/api.ts:104-124`
- Vibe Goal prompt：`../Vibe-Trading/frontend/src/pages/Agent.tsx:189-209`
- Vibe Goal 操作：`../Vibe-Trading/frontend/src/pages/Agent.tsx:862-878`、`:976-1033`
- 当前 panel：没有 `/sessions/{sid}/goal` API。

**迁移判断：高价值高成本。**
建议独立立项，先做轻量本地 Goal ledger，不和 MVP 页面混在一个提交里。

### 10. Swarm / 多代理团队运行态

Vibe 有 swarm preset、run、SSE 状态卡和 agent 逐项进度。

- Vibe Swarm API：`../Vibe-Trading/frontend/src/lib/api.ts:131-144`
- Vibe Swarm 事件：`../Vibe-Trading/frontend/src/pages/Agent.tsx:633-657`
- Vibe 状态卡：`../Vibe-Trading/frontend/src/components/chat/SwarmStatusCard.tsx`
- 当前 panel：没有 swarm 后端，也没有状态卡。

**迁移判断：高成本，暂缓。**
除非产品明确要多代理运行时，否则先维持单 agent loop。

### 11. 回测结果卡和 PineScript

Vibe 会把 run 结果渲染成卡片，按需拉 `/runs/{id}`、PineScript 和 equity curve。

- Vibe run API：`../Vibe-Trading/frontend/src/lib/api.ts:87-96`
- Vibe 历史 run 卡恢复：`../Vibe-Trading/frontend/src/pages/Agent.tsx:349-399`
- Vibe 完成事件处理：`../Vibe-Trading/frontend/src/pages/Agent.tsx:560-616`
- 当前 panel：`run_backtest` 已从 agent 白名单排除，避免全市场重任务；没有 agent run card。

**迁移判断：中价值，需先定义安全闸门。**
如果开放回测工具，必须先加 symbols 必填、数量/日期 cap、每对话次数限制，再做结果卡。

### 12. Shadow Account 归属说明

Vibe 的 Shadow Account 不是 Agent 页面路线里的独立功能，而是 C1 Trade Journal Phase 2+3 的同一条产品线：从盈利 roundtrip 抽个人规则、影子回测、今日信号扫描。

- C1 规划：`docs/superpowers/specs/2026-07-04-vibe-frontend-porting-design.md` 子项目 3。
- Vibe 模型：`../Vibe-Trading/agent/src/shadow_account/models.py` 的 `ShadowRule` 包含 `entry_condition`、`exit_condition`、`support_count`、`coverage_rate` 等规则字段。
- Vibe 参考：`../Vibe-Trading/agent/src/shadow_account/{extractor,codegen,backtester,scanner}.py`，对应「抽规则 → 编译条件 → 回测 → 今日信号扫描」四段式管线。
- Vibe 工具：`../Vibe-Trading/agent/src/tools/shadow_account_tool.py` 只是薄工具入口，业务逻辑落在 `src.shadow_account`，工具层没有 LLM 调用。
- C1 红线：规则提取和影子回测保持纯统计/启发式，不经 LLM；如果参考 Vibe `extractor.py`，不得引入其可选自然语言翻译层。

**迁移判断：指向 C1，不在 Agent 页面路线里重复立项。**
Vibe 四段式拆分可作为 C1 Phase 2+3 写实现计划时的算法参考，但不能直接搬。两边数据模型和执行引擎不同：panel 需要读 Trade Journal 的 `source.json`，并把个人规则编译为 entry/exit mask 后接 `BacktestEngine.simulate_portfolio()`；Vibe 使用自己的 shadow backtester。

### 13. 交易连接器 runtime、mandate proposal、kill switch

Vibe 有 live status、授权、runner start/stop、halt、mandate commit 和运行态事件。

- Vibe live API：`../Vibe-Trading/frontend/src/lib/api.ts:193-223`
- Vibe live state：`../Vibe-Trading/frontend/src/pages/Agent.tsx:245-257`
- Vibe live events：`../Vibe-Trading/frontend/src/pages/Agent.tsx:678-728`
- Vibe halt：`../Vibe-Trading/frontend/src/pages/Agent.tsx:954-974`
- 当前 panel：Trading 页尚未实现该 runtime。

**迁移判断：暂不移植功能，只借鉴安全模式。**
这属于交易执行系统，不应作为 Agent Chat 后续小补丁。

## 建议落地顺序

1. **P7.1 前端可直接补齐（已落地）**
   - WelcomeScreen 示例入口（按 panel 能力重写）。
   - 消息展示组件拆分：`AgentAvatar`、`MessageBubble`、`ToolTrace`（当前为页内轻量组件）。
   - Markdown 渲染：已复用现有 `MarkdownRenderer`。
   - 导出 Markdown。
   - 错误重试。

2. **P7.2 Agent Session 地基（核心已落地）**
   - `/api/agent/sessions`：list/create/delete/rename。
   - `/api/agent/sessions/{id}/messages`：持久历史。
   - `/agent?session=<id>`：URL 恢复。
   - 桌面侧栏展示 session；移动端保留下拉。

3. **P7.3 长任务运行态**
   - attempt id、cancel endpoint：最小版已落地（仅内存态，不跨进程/重启）。
   - SSE event bus 或在现有 NDJSON 上扩展进度事件：未落地。
   - `tool_progress` / `tool_heartbeat` / timeout 恢复。
   - 当前不等同于完整服务端 attempt runtime；无持久事件存储、无 replay。

4. **P7.4 文件/文档上下文（轻量版已落地）**
   - 复用 panel 现有 `/api/documents/read` 和 `document_reader` 边界。
   - 附件只作为只读上下文，不允许 agent 任意读写文件。
   - 剩余：多附件、URL 附件、长期附件管理均暂缓。

5. **独立立项**
   - Goal / evidence ledger。
   - Swarm runtime。
   - agent 回测工具 + run card。
   - Shadow Account 不在这里重复立项，归入 C1 Trade Journal Phase 2+3。
   - 交易连接器 mandate / kill switch。

## 不建议直接照搬的部分

- 不直接搬 Vibe 的 `/sessions/*` 前端调用；panel 已有 `/api/agent/sessions`，但路径前缀、事件形态和 runtime 语义不同。
- 不直接搬 `SwarmStatusCard`、`RunnerStatus`、`MandateProposalCard`，它们依赖缺失的 runtime。
- 不把 `run_backtest` 重新放回 agent 工具白名单，除非先补成本闸门。
- 不照搬 Vibe welcome 文案里的期权、加密、连接器、多市场能力；panel 当前能力边界不同。
