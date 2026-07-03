# 多 AI Provider 配置 + 随处切换 改动方案（设计）

- **日期**：2026-07-03
- **需求**：AI provider 配置支持配置**多个**，并在**任何可触发 AI 对话/任务的地方**切换使用哪个。
- **性质**：设计文档（改动方案）。经确认后转 writing-plans 出实现计划。

---

## 一、设计决策（已确认）

| 决策 | 选择 | 含义 |
|---|---|---|
| **切换语义** | **每入口记忆 + 全局默认兜底** | 每个 AI 触发点（个股分析/复盘/财务/策略生成/agent）各自记住上次选的 profile，下次沿用；没选过则用全局默认。切换只影响该入口，不改全局。 |
| **入口范围** | **全部 5 个 AI 触发点** | 用户明确"任何触发 AI 的地方都能切"。 |
| **定时/自动 AI 任务** | 用**全局默认** | 定时复盘等无 UI 现场切换，跟全局默认 profile。 |

---

## 二、现状（改动前）

**单一扁平配置**，存于 `data/user_data/secrets.json`：
`ai_provider`（`openai_compat` / `codex_cli`）、`ai_base_url`、`ai_api_key`、`ai_model`、`ai_codex_command`、`ai_user_agent`。

**解析**：`ai_provider.py` 的 `current_ai_provider()/current_ai_model()/current_codex_command()/_openai_client()(base_url)/secrets_store.get_ai_key()` 全读这一份。

**AI 触发点（5 个，全部经 `generate_ai_text`/`stream_ai_text`）**：
| # | 入口 | 代码 | API |
|---|---|---|---|
| 1 | 个股分析 | `stock_analyzer.py:337`（stream） | `POST /api/stock-analysis/analyze` |
| 2 | 盘后复盘 | `market_recap.py:300`（stream） | market recap 生成 + 定时复盘 |
| 3 | 财务分析 | `financial_analyzer.py:174`（stream） | `POST /api/financials/analyze` |
| 4 | AI 策略构建 | `strategy/ai_generator.py:79`（generate） | **`POST /api/strategies/build`**（前端 `strategyBuild` 两步构建真实入口，codex review High 3）+ `/ai/generate`、`/ai/test`（次要） |
| 5 | Agent 对话 | `agent.py:9`（generate） | `POST /api/agent/chat` |

> `trade_journal._narrative`（`trade_journal.py:238`）当前是**本地模板、不走 AI**，非切换点（未来若接 Hybrid LLM 叙事再纳入）。

**配置入口**：`POST /api/settings/ai`（单配置保存）；`GET /api/settings` 返回 AI 字段；前端 `settings/AI.tsx` 单表单。

---

## 三、目标数据模型

`secrets.json` 从"扁平字段"改为"**profile 列表 + 默认指针**"：

```jsonc
{
  "ai_profiles": [
    {
      "id": "p_ab12cd34",          // 稳定 id（后端生成）
      "name": "OpenAI 官方",        // 用户可读名（切换器/管理页显示）
      "provider": "openai_compat", // openai_compat | codex_cli
      "base_url": "https://...",
      "api_key": "sk-...",          // 仅 openai_compat；返回前端必须脱敏
      "model": "gpt-4o",
      "codex_command": "",          // 仅 codex_cli
      "user_agent": ""
    },
    { "id": "p_ff99", "name": "本地 Codex", "provider": "codex_cli", "codex_command": "codex", ... }
  ],
  "ai_default_profile_id": "p_ab12cd34"
}
```

**迁移（一次性、自动、无感）**：启动或首次读取时，若检测到旧扁平字段（`ai_provider` 等）且 `ai_profiles` 不存在 → 用旧字段构造一个 `name="默认"` 的 profile、设为默认；迁移后可保留旧字段一版兼容或清除（实现计划定）。**用户无需重配。**

### 3.1 Provider 类型（Provider Kind）与 CLI 适配器

`provider` 字段分三支（已确认：ACP 统一传输 + codex 保留专属）：

1. **HTTP 类**：`openai_compat` —— OpenAI 兼容 HTTP API，靠 `base_url/api_key/model` 区分（OpenAI、任何兼容网关，含 hermes 的 `gateway`/`serve` 网关模式→指 localhost）。
2. **ACP 类**：`acp` —— **一个统一 ACP 客户端适配器**（[[ACP 传输]]），驱动会说 Agent Client Protocol 的本机 agent。AI 配置存 `{launch_command, model, + 工具特有字段}`（如 `hermes acp`）。
   - **hermes**：原生 `hermes acp` ✅（`hermes acp --check` 通过）——**本期 ACP 只承诺 Hermes**（codex review High 2）。
   - **opencode**：`opencode acp` 存在但为 **server 形态**（`--port/--hostname`），非 stdio 子进程 → 需确认 transport/桥接，**本期不接**。
   - **claude**：**无原生 `acp` 子命令** → 本期不列，除非另立 bridge。
   - 报文/权限按**真实 ACP schema**（`agent_message_chunk` content block；拒绝=`DeniedOutcome(outcome="cancelled")`），见后端计划任务 5。
3. **codex 专属**：`codex_cli`（保留现有适配器）—— codex 只有 `mcp-server`、无原生 ACP，暂不并入 ACP 传输；后续若接 codex-acp bridge 再归并。

> **关键约束（安全）**：这些 agent 是全功能的（可改文件/跑命令）。panel 只当**文本生成器**用：
> - **ACP 类**天然满足——ACP 权限模型下，客户端**拒绝一切 tool 权限请求**，agent 退化为纯文本回复，不在 panel 目录做 agentic 动作。这是 ACP 优于 bespoke CLI 的关键。
> - **codex_cli** 沿用现有隔离（`codex exec` + 独立 codex-home）。
>
> 各工具的 ACP 启动命令 / codex 受限调用，在实现计划逐一核实落定。

---

## 四、改动范围

### 后端

1. **`app/services/ai_profiles.py`（新建）** —— profile 存取单一事实源：
   - `list_profiles()`（脱敏用另一函数）、`get_profile(id)`、`create/update/delete_profile()`、`get_default_profile_id()/set_default()`、`resolve_profile(profile_id: str | None) -> Profile`（None/找不到 → 默认）、`migrate_legacy_if_needed()`。
2. **`app/services/ai_provider.py`（改）** —— 解析从"全局单配置"改为"按 profile"：
   - `generate_ai_text(...)` / `stream_ai_text(...)` 各**新增 keyword `profile_id: str | None = None`**（默认 None → 走默认 profile，**完全向后兼容**现有调用）。
   - 内部按 profile 的 [[Provider 类型]] 分派：`openai_compat` → 现有 HTTP 路径（吃 profile 的 base_url/key/model）；`acp` → 新 ACP 适配器；`codex_cli` → 现有 codex 路径（重构为吃 profile 字段而非全局）。
2b. **`app/services/ai_acp.py`（新建）** —— [[ACP 传输]] 客户端适配器：spawn 配置的启动命令、ACP handshake、发 prompt、**拒绝一切 tool 权限请求**、收集并返回文本；`is_available()` 探测启动命令。各工具确切启动命令在实现计划核实。
3. **5 个 AI 端点（改）** —— 各接收可选 `profile_id`（query/body/form），透传给 AI 调用：
   `stock_analysis.analyze`、market recap 生成、`financials.analyze`、`strategy.ai/generate|test`、`agent.chat`。**缺省不传 → 默认 profile**，兼容旧客户端。
4. **定时/自动任务（改）** —— 定时复盘等显式用默认 profile（`resolve_profile(None)`）。
5. **Settings API（改）** —— 单配置 CRUD 换成 profile CRUD：
   - `GET /api/settings/ai/profiles`（列表，key 脱敏）
   - `POST /api/settings/ai/profiles`（新建）、`PUT .../{id}`（编辑）、`DELETE .../{id}`
   - `POST /api/settings/ai/profiles/{id}/default`（设默认）
   - `POST /api/settings/ai/profiles/{id}/test`（连通性测试）
   - 旧 `POST /api/settings/ai` 保留一版：映射为"更新默认 profile"，过渡后移除。

### 前端

6. **`settings/AI.tsx`（重构）** —— 单表单 → **多 profile 管理页**：列表 + 新增/编辑/删除 + 设默认 + 逐个测试。复用现有 PRESETS（OpenAI/Codex CLI 等预设）作为新建模板。
7. **`<AiProviderSelector>`（新建共享组件）** —— 下拉选 profile，放到 5 个入口：个股分析、复盘(Review)、财务分析、策略生成、agent chat。
   - **每入口记忆**：`localStorage["ai_profile:<entry>"]`（entry 如 `stock_analysis`）。
   - **兜底**：无本地记忆或该 profile 已被删 → 回落全局默认（来自 settings）。
   - 选中值随该入口的 AI 请求以 `profile_id` 发送。
8. **`lib/api.ts`（改）** —— profile CRUD 类型/调用；5 个 AI 触发调用加可选 `profile_id`。

---

## 五、切换语义细节

- **记忆粒度（分入口而定）**：
  - **个股分析 / 复盘 / 财务分析 / 策略生成**：**功能级**，前端 `localStorage["ai_profile:<feature>"]`。切股/再次进入沿用上次选择。
  - **agent 对话**：**per-thread**（绑定 conversation 状态，每个对话线程各存一个 profile_id），新建线程继承全局默认。支持"线程1用 claude、线程2用 opencode 对比着问"。
    > **现实约束**：前端**目前无 agent 对话 UI**（`/api/agent/chat` 前端零调用，agent 为 P7 后端骨架）。后端已支持 `profile_id`；**前端 per-thread 选择器等 agent 对话页落地后再做**。当下有 UI 的触发点是：个股分析 / 财务分析 / 盘后复盘 / 策略 AI 生成（均功能级）。
- **不**跨入口共享——用户在个股分析选 A、在 agent 某线程选 B，互不影响。
- **兜底链**：入口本地记忆 → （无/失效）全局默认 profile → （无默认）第一个 profile。
- **全局默认**：仅在设置页切换；用于定时任务与"没选过"的入口。
- **删除 profile**：若删的是默认 → 要求先改默认或自动落到第一个；入口本地记忆指向已删 profile 时静默回落默认（不报错）。
- **不能删到 0**：至少保留一个 profile（或允许 0 但 AI 功能显示"未配置"）。

---

## 五、5 可用性与执行位置

- **执行位置**：`acp` 与 `codex_cli` 类的 agent 命令，一律在 **panel 后端服务所在的宿主机**上 spawn 子进程执行（与用户浏览器/客户端在哪**无关**）。`openai_compat` 类是纯 HTTP，与宿主机无关。
- **可用性探测**：每条 `acp`/`codex_cli` AI 配置带 `is_available()`——探测其启动命令能否在宿主机 spawn（复用/扩展现有 `codex_cli_available()`）。
- **不可用降级**：命令不在宿主机 → 切换器**灰显该配置** + 设置页标注"未检测到 `<命令>`" + 后端调用兜底报清晰错误（"该 AI 配置依赖的宿主机命令不可用"），**不** 500、不打断其它配置。
- **部署形态**：本机部署时宿主机=用户机器，CLI/ACP 全可用；远端部署时以宿主机实际安装为准（缺则灰显），`openai_compat` 始终可用作远端主力。**无需**为远端单独写降级分支——可用性探测统一覆盖两种形态。

## 六、安全与边界

| 项 | 处理 |
|---|---|
| 多个 api_key | 仍存 secrets.json（0600）；**列表/详情接口一律脱敏**，绝不返回明文 key；编辑时不回填明文，留空=不改。 |
| codex_cli profile | 本机只有一个 codex CLI，多个 profile 可都指向它（无冲突）；codex profile 无 api_key/base_url。 |
| profile_id 校验 | 后端 `resolve_profile` 对非法/不存在 id 静默回落默认（不 500），避免坏 localStorage 打断 AI。 |
| 迁移幂等 | `migrate_legacy_if_needed` 只在无 `ai_profiles` 时跑一次，重复调用无副作用。 |
| 向后兼容 | AI 调用新增参数全默认 None；旧端点/旧前端不传 profile_id 一律走默认 profile，行为不变。 |

---

## 七、分期与风险

**建议单计划一次做完**（改动虽多但高度对称、无外部依赖）：
- 后端：数据模型 + ai_profiles + ai_provider 解析 + 5 端点透传 + settings CRUD + 迁移。
- 前端：AI.tsx 重构 + AiProviderSelector + 5 入口接入 + api.ts。

| 风险 | 缓解 |
|---|---|
| 迁移出错致用户丢配置 | 迁移幂等 + 保留旧字段一版；单测覆盖"旧扁平→profile" |
| key 泄漏 | 全接口脱敏 + 编辑不回填；复用现有 `secrets_store.mask` |
| 5 端点透传遗漏 | 以本文件"AI 触发点"表为清单逐一核对，全端点集成测试 |
| 前端记忆指向已删 profile | 兜底回落默认，测试覆盖 |
| 定时任务误用非默认 | 定时路径显式 `resolve_profile(None)`，测试断言 |
| ACP 各工具启动命令/能力不一 | 实现计划逐工具核实（hermes 原生 `acp` ✅；claude/opencode 待定确切命令或桥接）；单工具接不通不阻断其余 |
| ACP 未拒绝 tool 权限致 agent 乱动 | 适配器**默认拒绝所有 tool 权限请求**，集成测试断言 agent 不触碰文件系统 |
| CLI/ACP 宿主机不可用 | 每配置 `is_available()` 探测 + 灰显 + 兜底清晰报错（见 §五.5） |

---

## 八、开放项（实现计划前可留默认）

1. **旧 `POST /api/settings/ai` 是否保留过渡**：建议保留一版（映射默认 profile），降低前端切换期风险。
2. **删除到 0 profile 的行为**：建议允许 0，AI 入口显示"未配置 AI"引导去设置页。
3. **profile 是否带"用途标签"**（如"仅个股分析用"）：**YAGNI**，本期不做，per-entry 记忆已够。

---

## 九、验收要点（供后续计划对齐）

- 配 ≥2 个 profile，在个股分析与 agent 分别选不同 provider，各自请求走各自 profile（可从响应/日志的 model 名验证）。
- 切换只影响当前入口，不改全局默认；刷新后每入口沿用上次选择。
- 定时复盘走全局默认。
- 删除某 profile 后，曾选它的入口静默回落默认、不报错。
- 旧单配置用户升级后自动迁移为一个默认 profile，AI 功能无感可用。
- 任何接口都不返回明文 api_key。
