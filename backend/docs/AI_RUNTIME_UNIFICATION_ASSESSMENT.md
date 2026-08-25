# AI Runtime 统一与 Pi Agent Harness 可行性评估

> 日期：2026-08-18  
> 状态：评估完成  
> 适用范围：tickflow-stock-panel 的 AI 页面、后端运行时、provider、会话与发行链路

## 1. 结论

当前项目的 AI provider/profile 层已基本统一，但执行层并非同一套逻辑。现有能力应按三类运行时保留：

1. **Report Runtime**：个股、财务和大盘复盘等长文本流式报告；
2. **Structured Runtime**：自然语言条件解析、交易归因、计划检查和策略体检等强 schema 工作流；
3. **Agent Runtime**：`/agent` 多轮工具调用。

不应把所有 AI 页面强行改造成自主 Agent。Pi Agent Harness 最合理的试点范围仅为 `/agent`，其余业务继续保留 Python 侧既有程序门禁、Pydantic 校验、append-only 审计和持久化。

决策：

| 方案 | 结论 |
|---|---|
| 在 Python 架构内统一 AI 接口、事件和前端状态 | Go |
| 只对 `/agent` 试点 Pi Agent Harness | Conditional Go |
| 用 Pi 立即替换全部 provider | 暂不实施 |
| 把全部 AI 页面迁成 Pi Agent Session | No-Go |
| 浏览器直接运行 Pi 并持有模型密钥 | No-Go |
| 采用非官方 Python 移植 | No-Go |

## 2. AI 功能面盘点

| 页面或入口 | 用途 | 当前执行方式 |
|---|---|---|
| `/agent` | 多轮问答、行情查询、选股、不可变股票池、研究回测 | Python 自定义工具循环、会话、取消、SSE、工具轨迹 |
| `/stock-analysis` | 个股技术/基本面/财务/消息面分析 | Markdown 流式报告 |
| `/financials` | 三张财务报表和财务质量解读 | Markdown 流式报告 |
| `/review` 大盘复盘 | 市场情绪、连板、题材轮动和结构复盘 | Markdown 流式报告、定时归档 |
| `/condition-screener` | 自然语言转强类型筛选条件 | Pydantic 结构化输出和有限纠错 |
| `/screener` | 生成或修改 Python 策略代码 | 文本生成后执行 AST/META 安全校验 |
| `/trading` 计划检查 | Stage1 数据诊断和 Stage2 计划审查 | 两阶段结构化 AI，程序门禁不可升级 |
| `/trading` 交易归因 | 单笔和盘后批量问题归类 | 结构化输出、不可变交易标识、落盘 |
| `/settings?tab=proposals` | 策略机械体检后的 AI 语义审查 | 结构化输出和 invariant 校验 |
| `/settings?tab=ai` | profile、默认模型和 fallback 配置 | AI 控制面，不执行研究任务 |

`/research` 的定时模板目前主要是确定性数据汇总，不是独立 LLM Agent。`/review` 红旗检测同样是机械逻辑，只有归因和复盘入口调用模型。

## 3. 已统一的底层能力

主要模型调用最终收敛到：

```text
业务入口
  -> ai_profiles.py
  -> ai_routing.py
  -> ai_provider.py
```

关键模块：

- `backend/app/services/ai_provider.py`
- `backend/app/services/ai_profiles.py`
- `backend/app/services/ai_routing.py`
- `backend/app/services/ai_budgets.py`

前端已有的共享模块：

- `frontend/src/components/AiProviderSelector.tsx`
- `frontend/src/components/AiExecutionMetaBadge.tsx`
- `frontend/src/lib/aiProfile.ts`
- `frontend/src/components/MarkdownRenderer.tsx`

这些模块统一了 profile 选择、provider 路由和部分执行元信息，但没有统一不同业务的执行语义。

## 4. 未统一的执行模式

### 4.1 Report Runtime

个股、财务和大盘复盘直接流式生成 Markdown，不执行 schema 纠错和工具循环。

### 4.2 Structured Runtime

`run_structured_ai` 负责：

```text
模型调用
  -> JSON 提取
  -> Pydantic schema
  -> immutable/invariant 校验
  -> 有限格式或语义重试
  -> usage/attempt/audit
```

这套运行时承载交易纪律和确定性业务约束，不得由通用 Agent Loop 替代。

### 4.3 Agent Runtime

`backend/app/services/agent_loop.py` 实现 `/agent` 的多轮工具循环：

- 最多五个工具轮；
- OpenAI 原生 function calling；
- Codex CLI JSON 降级；
- GLM DSML 清洗；
- 13 个只读/研究工具顺序执行；
- 最终回答单独流式生成。

会话、attempt、取消、事件回放和落盘由以下模块负责：

- `agent_runner.py`
- `agent_bus.py`
- `agent_sessions.py`
- `ai_attempts.py`

### 4.4 策略代码生成

策略构建流程是“生成代码 -> AST 安全检查 -> META 校验 -> 用户确认保存”，不应转换为具备文件写权限的通用 Agent。

## 5. 当前重复与维护成本

Pi 本身不会自动解决以下重复，后续应在 Python/React 内独立收敛：

1. `services/agent_loop.py` 与旧 `/api/agent/chat` 的工具循环重复；
2. 多个前端页面分别实现 NDJSON/SSE 读取；
3. 个股分析和财务分析各自维护任务状态、Dialog、Host、Bubble 和历史报告；
4. 多页面重复 profile 查询与 `resolveEntryProfile` 模板；
5. 财务、大盘复盘等入口的 `ai_meta` 覆盖不一致；
6. AI 健康检查和预算登记仍有分散入口。

## 6. 官方 Pi Agent Harness 事实

本评估针对官方 `earendil-works/pi`：

- `@earendil-works/pi-agent-core`：`0.84.2`
- `@earendil-works/pi-ai`：`0.84.2`
- 纯 ESM；
- Node `>=22.19.0`；
- MIT License；
- 无官方 Python 实现；
- 默认工具执行为 parallel，项目接入时必须显式改为 sequential；
- 不内置业务权限或沙箱，工具继承宿主进程能力；
- pre-1.0，接口仍存在较快演进。

官方资料：

- <https://github.com/earendil-works/pi>
- <https://github.com/earendil-works/pi/blob/main/packages/agent/README.md>
- <https://github.com/earendil-works/pi/blob/main/packages/ai/README.md>
- <https://github.com/earendil-works/pi/blob/main/packages/agent/package.json>
- <https://github.com/earendil-works/pi/blob/main/packages/agent/CHANGELOG.md>

## 7. 全量迁移阻塞项

### 7.1 Python/Node 运行时断层

项目后端为 Python/FastAPI，Pi 只有 TypeScript/Node 实现。浏览器运行会暴露模型密钥，也无法可靠承接后台会话和调度任务；非官方 Python 移植版本与官方实现不同步。因此只能采用受控 Node worker。

### 7.2 桌面与容器发行

当前生产 Docker 最终层为 `python:3.11-slim`，Node 20 仅用于构建前端。桌面客户端通过 PyInstaller 发布 Windows x64、macOS ARM64 和 Linux x64，也不包含 Node runtime。

Pi 要求 Node `>=22.19`。正式发行前必须另行解决 Node runtime 携带、签名、公证、进程回收、镜像体积和升级策略。本次试点不进入 Docker/PyInstaller 发行。

### 7.3 provider 语义不等价

现有 provider 包括：

- 任意 OpenAI-compatible `base_url/model`；
- 本地 `codex_cli` 子进程；
- GLM DSML/JSON 降级；
- 尚未接入的 ACP 配置。

Pi 的 OpenAI Codex provider 不等价于当前本地 Codex CLI。因此试点只支持 `openai_compat`，不替换 `ai_provider.py` 的统一 seam。

### 7.4 交易纪律不能迁入通用 Agent

以下约束继续只由 Python 实现：

- Pydantic `extra=forbid`；
- immutable context；
- 两阶段程序门禁；
- 模型不得升级门禁；
- 禁止订单方向、价格和执行动作；
- append-only artifact；
- parent attempt 连续性；
- 红旗和事件计数幂等。

### 7.5 会话不得双源

Python 必须继续作为 session、attempt、事件回放、取消和落盘的唯一事实源。试点不引入 Pi SessionRepo、会话树或 compaction 持久化。

## 8. 目标架构

```text
React AI surfaces
  -> FastAPI AI control plane
       -> Report Runtime -> ai_provider
       -> Structured Runtime -> ai_provider + Pydantic/gates/audit
       -> Agent Runtime
            -> Python adapter（默认）
            -> Pi worker adapter（试点）
                 -> Python typed agent_tools
```

长期应统一运行时 seam、事件协议、profile/预算/观测和前端任务状态，而不是统一业务语义。本次 Pi 试点只落地 Agent runtime seam 与事件兼容；usage/profile health 和 UI 级 runtime 选择仍未接入。

## 9. 不变量

Pi 试点必须保持：

- 前端 endpoint 和 NDJSON/SSE 事件协议不变；
- Python 独占 session、attempt、bus、取消和 append-only 落盘；
- 所有行情仍经 `data_providers`；
- 只注册现有只读/研究工具；
- 不注册文件、Shell、任意网络或交易执行工具；
- 工具严格顺序执行；
- 最多五个工具轮，然后无工具生成最终回答；
- Pi 不可用时显式失败，不在同一 attempt 中静默切换运行时；
- 默认运行时仍为 Python。

## 10. 最终建议

先通过默认关闭的 `/agent` 试点验证 Pi 的工具循环、事件和取消语义。只有在契约测试、真实端到端冒烟、故障终态和发行决策全部通过后，才讨论正式切换。Report Runtime、Structured Runtime、交易纪律域和 provider 主链不在本次迁移范围内。
