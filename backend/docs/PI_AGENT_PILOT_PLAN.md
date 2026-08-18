# Pi Agent Harness `/agent` 试点实施方案

> 日期：2026-08-18  
> 状态：source/dev-only 试点实现完成；默认关闭，未进入正式发行  
> 决策依据：[AI Runtime 统一与 Pi Agent Harness 可行性评估](AI_RUNTIME_UNIFICATION_ASSESSMENT.md)

## 1. 目标

在不改变前端接口、不迁移交易纪律域、不替换现有 AI provider 主链的前提下，为 `/agent` 增加一个默认关闭的 Pi Agent Harness 运行时适配器，验证：

- 官方 Pi 工具循环能否复用现有 13 个只读/研究工具；
- 事件、取消、终态和 append-only 落盘能否保持现有语义；
- OpenAI-compatible profile 能否在真实工具调用下工作；
- Node 不可用或 worker 异常时能否显式失败且不遗留进程。

本试点不进入 Docker 或 PyInstaller 正式发行。

## 2. 试点范围

### 2.1 包含

- `/api/agent/sessions/{session_id}/messages`
- `/api/agent/sessions/{session_id}/stream`
- `/api/agent/attempts/{attempt_id}/cancel`
- `agent_runner.py` 消费的 Agent 事件流 seam
- 现有 `agent_tools.TOOLS` 只读工具清单
- `openai_compat` profile
- 官方 `@earendil-works/pi-agent-core` 和 `@earendil-works/pi-ai` 0.84.2

### 2.2 不包含

- Report Runtime；
- Structured Runtime；
- trading plan check、autopsy、proposals；
- provider 路由整体迁移；
- Codex CLI、ACP；
- MCP server；
- Pi SessionRepo、compaction、会话树；
- Docker、桌面安装包和 release workflow；
- 前端协议或页面重构。

## 3. 核心决策

### 3.1 每个 attempt 派生一个 worker

不使用常驻 HTTP sidecar。每个 Pi attempt 由 Python 启动一个受控 Node 子进程：

```text
agent_runner
  -> agent_runtime.run_agent_stream
       -> python adapter: legacy agent_loop
       -> pi adapter: spawn Node worker
            <-> stdin/stdout NDJSON RPC
            -> Python agent_tools.call_tool
```

理由：

- 没有监听端口、Bearer token、CORS 或横向访问面；
- API key 只通过 stdin 进入单次 worker 内存；
- attempt 结束即释放 worker；
- 取消可以直接终止对应子进程；
- 不引入长期 Node 会话和 Python 会话双源；
- 本地试点不需要改变 FastAPI lifespan、Docker 或桌面打包。

代价是每轮对话有一次 Node 启动开销；试点优先安全和隔离，不优化常驻性能。

### 3.2 Python 是唯一事实源

以下职责保持在 Python：

- session 和消息持久化；
- attempt 状态；
- AgentBus 回放和 SSE；
- 取消注册表；
- assistant/tool trace 落盘；
- 工具权限、参数入口和执行；
- 错误脱敏；
- 终态合成。

Node worker 不写任何项目数据。

### 3.3 真实运行时 seam

外部深模块接口保持现有形状：

```python
run_agent_stream(messages, app_state, profile_id) -> AsyncIterator[str]
```

每行继续是旧 Agent NDJSON 事件。`agent_runner`、AgentBus、API 和前端不需要知道 adapter 类型。

### 3.4 不静默回退

`AGENT_RUNTIME=python` 是默认值。设置为 `pi` 后，以下情况必须产生明确 error 终态，不能在同一个 attempt 内静默切换回 Python：

- frozen/PyInstaller 环境；
- Node 版本低于 22.19；
- Node 命令或 worker 文件不存在；
- worker 未在超时内 ready；
- profile 不是 `openai_compat`；
- profile 缺少 base URL、API key 或 model；
- IPC 协议错误；
- worker 非零退出或中途死亡。
- 显式 profile ID 不存在；
- worker 在响应超时内无任何模型事件。

这样可避免同一会话中出现不可审计的运行时漂移。

## 4. 运行时配置

```dotenv
# 默认 python；试点时显式设为 pi
AGENT_RUNTIME=python

# Pi worker 启动配置
AGENT_PI_NODE_COMMAND=node
AGENT_PI_WORKER_PATH=
AGENT_PI_READY_TIMEOUT_S=10
AGENT_PI_RESPONSE_TIMEOUT_S=90
```

语义：

- `AGENT_PI_WORKER_PATH` 为空时按源码树约定定位根目录 worker；
- 显式路径无效时 fail closed；
- 不提供持久化 token 或 API key 配置；
- 模型密钥继续来自现有 AI profile 存储。
- worker 子进程只继承 Node 运行所需的 allowlist 环境变量，`AI_API_KEY`、认证密码等父进程 secrets 不继承；
- 每次等待模型事件默认 90 秒；收到 delta、工具请求或其它协议事件后重新计时。

## 5. IPC 协议

一行一个 JSON 对象，stdout 不允许输出其它文本；诊断只能写 stderr，并且必须脱敏。

### 5.1 worker -> Python

```json
{"type":"ready"}
{"type":"delta","content":"..."}
{"type":"tool_request","request_id":"...","tool_call_id":"...","name":"get_quote","args":{}}
{"type":"done","elapsed_ms":1234}
{"type":"fatal","message":"..."}
```

### 5.2 Python -> worker

```json
{
  "type":"start",
  "profile":{"base_url":"...","api_key":"...","model":"..."},
  "messages":[{"role":"user","content":"..."}],
  "system_prompt":"...",
  "final_prompt":"...",
  "tools":[{"name":"get_quote","description":"...","input_schema":{},"read_only":true}],
  "max_tool_rounds":5
}
{"type":"tool_result","request_id":"...","ok":true,"result":{}}
{"type":"tool_result","request_id":"...","ok":false,"error":"..."}
```

`start` 是单次消息；worker 收到第二个 `start` 必须拒绝。

## 6. Pi Agent 配置

- 使用官方 `Agent` 类，不使用 SessionRepo；
- `streamFn = models.streamSimple.bind(models)`；
- `toolExecution = "sequential"`；
- 每个 Python JSON Schema 转换为 TypeBox schema，未知 schema fail closed；
- 工具错误必须 throw，不能作为成功文本返回；
- 只把 assistant `text_delta` 映射为旧 `delta`；
- `agent_end` 映射为 `done`；
- `fatal` 不得包含 API key、完整路径或原始 provider body。

### 6.1 五轮工具上限

Pi 在每个 `turn_end` 后调用 `prepareNextTurnWithContext`。实现必须：

1. 只统计 assistant 消息内含 `toolCall` 的轮次；
2. 第五个工具轮完成后返回 `{ context: { ...context, tools: [] } }`；
3. 保留完整 context/messages；
4. 第六轮无工具生成最终文本；
5. 另设总 LLM 轮数上限 6，防止 tools 已移除后模型继续幻觉 tool call。

不能在第五轮直接结束，否则会丢失面向用户的最终回答。

## 7. 工具安全

- Node 只能看到工具的名称、描述和输入 schema；
- Node 不读取 DuckDB、Parquet、文件系统或网络业务接口；
- Python 必须从现有 `agent_tools.TOOLS` 建表；
- 非 `read_only=true` 工具不得发送给 worker；
- 未登记工具名直接拒绝；
- 工具通过现有 Python 安全入口执行；
- 工具异常必须经现有路径脱敏；
- 工具保持顺序执行，不能并发。
- 上述约束是应用层工具/代码边界，不是 OS sandbox；正式发行前仍需完成 sidecar 进程隔离评估。

## 8. 取消与进程回收

取消路径：

```text
AttemptRegistry.cancel
  -> task.cancel
  -> Pi async generator finally
  -> close stdin
  -> terminate worker
  -> bounded wait
  -> kill worker
  -> await wait
```

任何正常完成、错误、消费者提前关闭或 `CancelledError` 都必须进入同一回收逻辑。禁止遗留孤儿 Node 进程。

## 9. 验收门

### 9.1 Python 契约

- 默认配置仍调用 legacy Python runtime；
- `agent_runner.run_agent_stream` 仍可被现有测试 monkeypatch；
- Pi 的 delta/tool_call/tool_result/done/error 与旧事件字段兼容；
- unsupported profile、missing worker、ready timeout 均 fail closed；
- 取消最终执行 terminate/kill/wait；
- API key 不出现在事件、stderr tail 或异常文本。

### 9.2 Node 契约

- Node 版本约束生效；
- 单次 start；
- schema 转换严格；
- 工具 request/result 可往返；
- 工具顺序执行；
- 五个工具轮后产生一次无工具最终回答；
- fatal 脱敏；
- stdout 只有协议 JSON。

### 9.3 端到端

使用本地假 OpenAI-compatible SSE 服务，必须覆盖：

1. 模型发起至少一次工具调用；
2. Python 执行工具并返回结果；
3. 模型生成最终文本；
4. 前端协议观察到 tool_call、tool_result、delta、done；
5. attempt 成功落盘；
6. 中途取消后进程回收且 attempt 为 cancelled；
7. worker 中途退出时 attempt 为 error。

真实模型冒烟必须由显式 `AGENT_RUNTIME=pi` 启动，不得改默认值。

### 9.4 2026-08-18 实现验证

- Node worker 契约：`15 passed`，覆盖真实 Pi `validateToolArguments`、开放值/数组 schema、单次 start、工具成功/失败往返、五轮上限、脱敏、协议错误与 abort；
- production dependency audit（npm 官方 registry）：`0 vulnerabilities`；
- Python Agent 测试族：`100 passed`；后端全量：`2482 passed, 3 skipped`；
- 本地假 OpenAI-compatible SSE + **真实 Pi SDK** + 真实 Node 子进程：`list_strategies` 工具从 Node 请求、Python 执行、结果返回 Node，随后生成最终文本；事件顺序为 `tool_call -> tool_result -> delta -> done`；
- 同一冒烟经 `agent_runner` 后，session `last_attempt_status=done`、assistant 文本和 tool trace 成功落盘，AgentBus 保持原事件序列并追加 `attempt_completed`；
- 真实慢请求取消后 Node 子进程以 `-15` 退出，回收耗时 `0.016s`；Python 取消/异常测试覆盖 terminate -> bounded wait -> kill；
- 前端 TypeScript 与 Vite production build 通过；前端协议和页面无需改动。

### 9.5 试点已知限制

- runtime 只能由服务进程环境变量选择，暂不提供 UI 或单 profile 切换；
- Pi attempt 暂不接入 `ai_usage_snapshot` / profile health 指标；不伪造 token usage；
- 旧兼容端点 `/api/agent/chat` 继续走原 Python loop，正式切换前应删除重复入口而不是维护第三套循环；
- sidecar 仅做应用层最小权限，不等价于 OS sandbox；
- Python 侧另设每 attempt 25 个 `tool_request` 的绝对安全上限，但当前没有跨 session 的全局 worker/费用并发上限；
- `start_pool_backtest` 虽不改交易事实，却会创建计算任务和 artifact；正式权限模型需区分“纯读”与“资源型只读”；
- 取消会立即回收 Node；已经进入 `asyncio.to_thread` 的同步 Python 工具不能被强杀，与原 Python loop 边界一致；
- 当前 lockfile 的 tarball URL 来自开发机 npm 镜像，虽有 integrity 且官方 audit 为 0，正式发行前仍须重建信任锚；
- `http://` OpenAI-compatible 端点会明文传输 Authorization，只适合用户明确控制的隔离本地网络；
- worker 刻意不继承 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`，试点环境需能直连 `AI_BASE_URL`；
- 不支持 Codex CLI、ACP、profile fallback、MCP、Docker 或桌面安装包。


## 10. 正式发布前置条件

试点通过不等于可进入正式发行。正式切换前仍需完成：

- Docker Node 22 runtime 或独立 worker 容器决策；
- Windows/macOS/Linux Node runtime 携带和签名；
- PyInstaller 桌面退出时的孤儿进程防护；
- 安装包和镜像体积评估；
- 三平台发布矩阵验证；
- sidecar OS sandbox / 进程权限边界与依赖供应链审计；
- 使用 npm 官方 registry 重建 lockfile，安装继续强制 `npm ci --ignore-scripts`；
- 约束 worker 路径、预检 Node 版本并决定是否固定 worker 摘要；
- 增加全局 attempt/worker 并发与费用上限；
- 把“纯读”和“资源型只读”拆成不同授权类别；
- Pi 版本升级策略和变更日志审计；
- 运行时切换后的旧 Python Agent Loop 清理计划。

## 11. 回滚

开发和试点阶段的回滚只需：

```dotenv
AGENT_RUNTIME=python
```

回滚不改变 session 格式、前端协议、工具注册表或历史 attempt，因此已有会话和记录仍可继续读取。
