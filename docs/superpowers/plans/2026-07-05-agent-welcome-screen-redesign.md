# Agent WelcomeScreen 改版 实现计划

> **面向 AI 代理的工作者：** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 `/agent` 页面的空态从 4 条扁平示例，改成按面板真实 11 个 agent 工具分类的"能力标签 + 4 分类 × 2~3 示例（共 9 条）"欢迎屏，让用户一眼看到 AI 助手实际能做什么（含刚上线的 P7.5 量化工具），而不是照搬 Vibe-Trading 那种面板不支持的期权/连接器/Swarm 类别。

**架构：** 纯前端改动，只涉及 `frontend/src/pages/Agent.tsx` 里的 `EXAMPLES` 常量和 `WelcomeScreen` 组件。不新增依赖、不改后端、不改其它组件。

**技术栈：** React + TypeScript + Vite + Tailwind。前端无单元测试框架（仓库里没有任何 `.test.ts(x)` 文件），验证手段是 `pnpm tsc --noEmit` + `pnpm build` + 手动视觉核对，与本仓库前端既有惯例一致。

## Global Constraints

- 分类和示例文案必须对应面板**真实存在**的 agent 工具（`backend/app/services/agent_tools.py::TOOLS`），不能写用户点了没反应的空头示例。
- 不加期权、交易连接器、Swarm 多智能体辩论、多市场（美股/加密）相关的示例或标签——这些是 gap-assessment 里明确排除、面板当前不支持的能力。
- 不加交易复盘（Trade Journal）相关示例——`upload_journal`/`diagnose` 目前不是 agent tool（P7.7 尚未做），写进去会变成点了没反应的空承诺。
- 保持现有视觉语言（`rounded-card`/`border-border`/`bg-surface`/`text-muted` 等 Tailwind token），不引入新样式体系。
- commit 需用户授权；永不 push。

---

### Task 1: WelcomeScreen 改版——分类示例 + 能力标签

**文件：**
- 修改：`frontend/src/pages/Agent.tsx:20-25`（`EXAMPLES` 常量）
- 修改：`frontend/src/pages/Agent.tsx:102-127`（`WelcomeScreen` 组件）

**接口：**
- Consumes：`WelcomeScreen` 的 props 签名不变——`{ disabled: boolean; onExample: (prompt: string) => void }`（第 447 行 `<WelcomeScreen disabled={streaming} onExample={sendPrompt} />` 调用点不用改）。
- Produces：无（叶子组件，不被其它任务消费）。

**当前状态**（供对照，不是要保留的目标代码）：

```tsx
const EXAMPLES = [
  { title: '内置策略', prompt: '有哪些内置策略？分别适合什么行情？' },
  { title: '个股走势', prompt: '看下 600519 最近走势和关键风险。' },
  { title: '市场概览', prompt: '总结今天市场概览，给出板块和情绪线索。' },
  { title: '回测思路', prompt: '帮我设计一个低频趋势策略，并说明需要哪些数据验证。' },
]
```

```tsx
function WelcomeScreen({ disabled, onExample }: { disabled: boolean; onExample: (prompt: string) => void }) {
  return (
    <div className="flex min-h-[42vh] flex-col items-center justify-center gap-5 px-2 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent/15 text-accent">
        <Bot className="h-6 w-6" />
      </div>
      <div>
        <div className="text-sm font-semibold text-foreground">AI 助手</div>
        <div className="mt-1 text-xs text-muted">询问策略、行情、复盘线索，必要时会调用面板只读工具。</div>
      </div>
      <div className="grid w-full max-w-2xl grid-cols-1 gap-2 sm:grid-cols-2">
        {EXAMPLES.map(ex => (
          <button
            key={ex.title}
            disabled={disabled}
            onClick={() => onExample(ex.prompt)}
            className="rounded-card border border-border bg-surface px-3 py-2 text-left hover:bg-elevated disabled:opacity-50"
          >
            <div className="text-xs font-medium text-foreground">{ex.title}</div>
            <div className="mt-1 text-[11px] leading-relaxed text-muted">{ex.prompt}</div>
          </button>
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 1: 替换 `EXAMPLES` 常量为分类结构**

用下面这段替换 `frontend/src/pages/Agent.tsx:20-25` 的 `EXAMPLES` 常量（保留在文件里同样的位置，即 `ChatMsg` 接口之后、`AgentAvatar` 函数之前）：

```tsx
interface ExampleItem {
  title: string
  prompt: string
}

interface ExampleCategory {
  label: string
  items: ExampleItem[]
}

const CAPABILITY_CHIPS = ['策略筛选', '因子分析', '组合优化', '回测验证', '只读数据工具']

const EXAMPLE_CATEGORIES: ExampleCategory[] = [
  {
    label: '策略与选股',
    items: [
      { title: '内置策略', prompt: '有哪些内置策略？分别适合什么行情？' },
      { title: '选股筛选', prompt: '帮我筛选连续放量、涨幅超5%的股票。' },
    ],
  },
  {
    label: '行情与市场',
    items: [
      { title: '个股走势', prompt: '看下 600519 最近走势和关键风险。' },
      { title: '市场概览', prompt: '总结今天市场概览，给出板块和情绪线索。' },
    ],
  },
  {
    label: '量化分析',
    items: [
      { title: '单因子分析', prompt: '分析一下 momentum_20d 这个因子最近半年的 IC 表现。' },
      { title: '多因子对比', prompt: '帮我对比一下 rsi_14、macd_hist 和 momentum_60d 这几个因子的 IC 表现，哪个更强。' },
      { title: '多因子合成', prompt: '把 rsi_14 和 macd_hist 按 IC 加权，给这些股票合成打分排名。' },
    ],
  },
  {
    label: '组合与回测',
    items: [
      { title: '组合优化', prompt: '用风险平价方法给这几只股票算组合权重。' },
      { title: '策略回测', prompt: '帮我跑个回测验证这个策略最近表现。' },
    ],
  },
]
```

- [ ] **Step 2: 改写 `WelcomeScreen` 组件渲染分类网格 + 能力标签**

用下面这段替换 `frontend/src/pages/Agent.tsx:102-127` 的 `WelcomeScreen` 函数：

```tsx
function WelcomeScreen({ disabled, onExample }: { disabled: boolean; onExample: (prompt: string) => void }) {
  return (
    <div className="flex min-h-[42vh] flex-col items-center justify-center gap-5 px-2 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent/15 text-accent">
        <Bot className="h-6 w-6" />
      </div>
      <div>
        <div className="text-sm font-semibold text-foreground">AI 助手</div>
        <div className="mt-1 text-xs text-muted">询问策略、行情、因子、组合，必要时会调用面板只读工具。</div>
      </div>
      <div className="flex flex-wrap justify-center gap-1.5">
        {CAPABILITY_CHIPS.map(label => (
          <span
            key={label}
            className="rounded-full border border-border bg-elevated px-2.5 py-1 text-[11px] text-muted"
          >
            {label}
          </span>
        ))}
      </div>
      <div className="grid w-full max-w-2xl grid-cols-1 gap-3 text-left sm:grid-cols-2">
        {EXAMPLE_CATEGORIES.map(cat => (
          <div key={cat.label} className="space-y-1.5">
            <div className="px-1 text-[11px] font-medium text-secondary">{cat.label}</div>
            {cat.items.map(ex => (
              <button
                key={ex.title}
                disabled={disabled}
                onClick={() => onExample(ex.prompt)}
                className="block w-full rounded-card border border-border bg-surface px-3 py-2 text-left hover:bg-elevated disabled:opacity-50"
              >
                <div className="text-xs font-medium text-foreground">{ex.title}</div>
                <div className="mt-1 text-[11px] leading-relaxed text-muted">{ex.prompt}</div>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: 类型检查**

运行：`cd frontend && pnpm tsc --noEmit`
预期：exit 0，无报错。（如果报 `EXAMPLES`/`ExampleItem`/`ExampleCategory` 相关的"declared but never used"或找不到符号，说明 Step 1 替换时漏删了旧的 `EXAMPLES` 声明或者命名不一致，检查文件里是否只有一份 `EXAMPLE_CATEGORIES`/`CAPABILITY_CHIPS` 声明。）

- [ ] **Step 4: 构建验证**

运行：`cd frontend && pnpm build`
预期：成功（允许既有的 chunk-size / dynamic import 警告）。

- [ ] **Step 5: 手动视觉验证**

启动前端 dev 服务（如尚未运行）：`cd frontend && pnpm dev`，浏览器打开 `/agent`，确认：
- 空态显示 5 个能力标签（策略筛选/因子分析/组合优化/回测验证/只读数据工具）横向排列在标题下方。
- 下方是 2×2 网格，4 个分类（策略与选股/行情与市场/量化分析/组合与回测），"量化分析"下面 3 个可点击的示例卡片（单因子分析/多因子对比/多因子合成），其余 3 个分类各 2 个。
- 点击任意示例卡片，输入框应该被填充/直接发送该 prompt（沿用现有 `onExample={sendPrompt}` 行为，点击即发送，不是先填充等用户手动点发送——这是现有 `sendPrompt` 的既定行为，不用改）。
- 发送一条消息后，空态应该消失，只剩对话内容（现有逻辑 `{msgs.length === 0 && <WelcomeScreen .../>}` 保证这一点，不用改）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/Agent.tsx
git commit -m "feat(ui): redesign agent WelcomeScreen with categorized examples + capability chips"
```

---

## 自检

**1. 规格覆盖度：** 4 分类共 9 条示例（策略与选股 2 条、行情与市场 2 条、量化分析 3 条、组合与回测 2 条），逐一对应面板全部 11 个 agent 工具中除 `get_capabilities`/`list_ext_data`（这两个是元信息类工具，不适合做成"示例 prompt"，用户不会主动问"告诉我你的能力标签"）之外的 9 个：`list_strategies`/`run_screener`/`get_kline`/`get_market_overview`/`analyze_factor`/`compare_factors`/`compose_factor_score`/`optimize_portfolio`/`run_backtest`。**（panel 3 评审 Medium，已verify属实并修正：最初版本漏了 `compare_factors` 专属示例，"多因子合成"只对应 `compose_factor_score`，两者是不同工具——`compare_factors` 逐个列出多个因子的 IC/IR 不合成，`compose_factor_score` 按 IC 加权合成一个综合打分。已在"量化分析"类目下补第 3 条"多因子对比"专门对应 `compare_factors`。）** 能力标签用简短概括覆盖工具边界。不含期权/连接器/Swarm/多市场/交易复盘相关内容，符合 Global Constraints。

**2. 占位符扫描：** 无 TBD/TODO，Step 1/2 均为完整可直接替换的代码。

**3. 类型一致性：** `WelcomeScreen` 的 props 签名（`disabled`/`onExample`）在 Step 2 里保持不变，第 447 行调用点不需要跟着改；新增的 `ExampleItem`/`ExampleCategory` 接口只在本文件内部使用，不导出、不影响其它文件。

**4. 范围边界：** 这是纯前端、单文件、无后端依赖的小改动，不需要拆分成多个任务；不涉及 `agentChatStore.ts`/`api.ts`/`router.tsx`，也不影响 P7.5 刚验收通过的后端工具。
