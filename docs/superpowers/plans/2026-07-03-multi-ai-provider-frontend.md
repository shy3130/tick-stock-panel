# 多 AI 配置（前端）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。设计见 `docs/superpowers/specs/2026-07-03-multi-ai-provider-design.md`；**依赖后端计划** `2026-07-03-multi-ai-provider-backend.md` 已落地（profile CRUD API + 各 AI 端点收 `profile_id`）。

**目标：** 前端把单 AI 配置表单改成**多 [[AI 配置]] 管理页**，并在各 AI 触发入口加**配置选择器**（功能级记忆 + 全局默认兜底），选中的 `profile_id` 随 AI 请求发送。

**架构：** `settings/AI.tsx` 重构为 profile 列表管理（增删改 + 设默认 + 逐个测试 + CLI/ACP 可用性状态）。新建共享 `<AiProviderSelector>`（下拉选 profile，localStorage 按入口记忆）。接到有 UI 的 4 个入口：个股分析 / 财务分析 / 盘后复盘 / 策略 AI 生成。`api.ts` 加 profile CRUD 类型与调用、4 个 AI 流式调用加 `profile_id`。

**技术栈：** React + TS + Vite。验证 `cd frontend && pnpm tsc --noEmit` + 手工过 UI。

**范围现实（重要）：**
- 有 UI 的 AI 触发点 = **个股分析（StockAnalysis）/ 财务分析（financials AiAnalysisDialog）/ 盘后复盘（Review→market-recap）/ 策略 AI 生成（strategy）**，全部**功能级**记忆。
- **agent 对话前端 UI 目前不存在**（`/api/agent/chat` 前端零调用）→ 设计里的 **agent per-thread 选择器本计划不做**，等 agent 对话页落地后另接（后端已支持 profile_id）。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `frontend/src/lib/api.ts` | API 类型/调用 | 加 profile CRUD；4 个 AI 流式调用加 `profileId` 参数 |
| `frontend/src/lib/aiProfile.ts` | 入口记忆 helper | 创建（localStorage 读写 + 兜底默认） |
| `frontend/src/components/AiProviderSelector.tsx` | 共享配置选择器 | 创建 |
| `frontend/src/pages/settings/AI.tsx` | 多配置管理页 | 重构 |
| `frontend/src/pages/StockAnalysis.tsx`、`components/financials/AiAnalysisDialog.tsx`、`pages/Review.tsx`、strategy AI 触发处 | 接入选择器 | 改：渲染选择器 + 请求带 profileId |

---

### 任务 1：api.ts —— profile CRUD 类型/调用 + AI 调用加 profileId

**文件：**
- 修改：`frontend/src/lib/api.ts`

- [ ] **步骤 1：加 profile 类型与 CRUD**

```ts
export interface AiProfileMasked {
  id: string; name: string; provider: string
  base_url?: string; model?: string; codex_command?: string; launch_command?: string
  has_api_key: boolean; api_key_masked?: string; is_default: boolean
  available?: boolean            // 后端 CLI/ACP 可用性探测（openai_compat 恒 true）
}

// api 对象内新增：
aiProfiles: () => request<{ profiles: AiProfileMasked[]; default_id: string }>('/api/settings/ai/profiles'),
createAiProfile: (p: Partial<AiProfileMasked> & { api_key?: string }) =>
  request<{ id: string }>('/api/settings/ai/profiles', { method: 'POST', body: JSON.stringify(p) }),
updateAiProfile: (id: string, p: Partial<AiProfileMasked> & { api_key?: string }) =>
  request<{ ok: boolean }>(`/api/settings/ai/profiles/${id}`, { method: 'PUT', body: JSON.stringify(p) }),
deleteAiProfile: (id: string) =>
  request<{ ok: boolean }>(`/api/settings/ai/profiles/${id}`, { method: 'DELETE' }),
setDefaultAiProfile: (id: string) =>
  request<{ ok: boolean }>(`/api/settings/ai/profiles/${id}/default`, { method: 'POST' }),
```

- [ ] **步骤 2：4 个 AI 调用加 `profileId`**

各加可选 `profileId?: string`，附到请求体（`profile_id: profileId`）。缺省不传 → 后端默认。确切调用点：
- `financialAnalyzeStream`（api.ts:1468）
- stock analysis stream（api.ts:1548）
- market recap（api.ts:1610）
- **`strategyBuild`（api.ts:1750-1753 → `POST /api/strategies/build`）**——codex review High 3：这是策略 AI 的**真实入口**（两步构建），不是 `/ai/generate`。给 `strategyBuild(step, payload)` 的 payload 或签名加 `profileId`，随 `/build` 请求体发送。

- [ ] **步骤 3：`pnpm tsc --noEmit` 通过 + Commit**

```bash
cd frontend && pnpm tsc --noEmit
git add src/lib/api.ts && git commit -m "feat(ui): AI profile CRUD API + profile_id on AI stream calls"
```

---

### 任务 2：入口记忆 helper + AiProviderSelector 组件

**文件：**
- 创建：`frontend/src/lib/aiProfile.ts`
- 创建：`frontend/src/components/AiProviderSelector.tsx`

- [ ] **步骤 1：记忆 helper**

```ts
// frontend/src/lib/aiProfile.ts
const KEY = (entry: string) => `ai_profile:${entry}`

export function getRememberedProfile(entry: string): string | null {
  return localStorage.getItem(KEY(entry))
}
export function rememberProfile(entry: string, profileId: string): void {
  localStorage.setItem(KEY(entry), profileId)
}
// 解析当前入口应使用的 profileId：本地记忆(若仍存在) → 否则全局默认
export function resolveEntryProfile(entry: string, profiles: { id: string }[], defaultId: string): string {
  const remembered = getRememberedProfile(entry)
  if (remembered && profiles.some(p => p.id === remembered)) return remembered
  return defaultId
}
```

- [ ] **步骤 2：AiProviderSelector 组件**

```tsx
// frontend/src/components/AiProviderSelector.tsx
import { useQuery } from '@tanstack/react-query'   // 项目现有数据获取方式，按实际替换
import { api } from '@/lib/api'
import { resolveEntryProfile, rememberProfile } from '@/lib/aiProfile'

export function AiProviderSelector({ entry, value, onChange }: {
  entry: string; value?: string; onChange: (id: string) => void
}) {
  const q = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles })
  const profiles = q.data?.profiles ?? []
  const defaultId = q.data?.default_id ?? ''
  const current = value ?? resolveEntryProfile(entry, profiles, defaultId)
  if (profiles.length <= 1) return null   // 只有 0/1 条配置时不显示选择器
  return (
    <select value={current} onChange={e => { rememberProfile(entry, e.target.value); onChange(e.target.value) }}>
      {profiles.map(p => (
        <option key={p.id} value={p.id} disabled={p.available === false}>
          {p.name}{p.available === false ? '（未检测到命令）' : ''}{p.is_default ? ' · 默认' : ''}
        </option>
      ))}
    </select>
  )
}
```

（样式/下拉组件按项目现有 UI kit 替换；`useQuery`/数据获取按项目实际封装。）

> **codex review Medium 4（默认值陷阱）**：`current = value ?? resolveEntryProfile(...)` 时，若 parent 的 `value` state 初始 `undefined`，会出现"选择器显示 A、但请求发 `profile_id=undefined`"。**修法**：组件在挂载/profiles 就绪后，用 `useEffect` 把 resolved id **回吐给 parent**（`onChange(current)`），确保 parent state 与显示一致；**且**任务 4 各入口在真正发起 AI 请求时统一用 `resolveEntryProfile(entry, profiles, defaultId)` 取值（不直接依赖可能为 undefined 的 state）。手测须验证请求体确实带记忆的 profile_id。

- [ ] **步骤 3：`pnpm tsc` + Commit** `git commit -am "feat(ui): AiProviderSelector + per-entry memory helper"`

---

### 任务 3：settings/AI.tsx 重构为多配置管理页

**文件：**
- 修改：`frontend/src/pages/settings/AI.tsx`

- [ ] **步骤 1：改为 profile 列表管理**

- 列表渲染 `api.aiProfiles()`：每条显示 name / provider 类型 / 可用状态（CLI/ACP 灰显不可用）/ 是否默认。
- 操作：新增（复用现有 PRESETS 作模板，含 openai_compat / codex / **acp** 新预设，如 `{ label: 'Hermes (ACP)', provider: 'acp', launch_command: 'hermes acp' }`）、编辑、删除、设默认、测试（调后端 profile 测试端点，若后端提供）。
- 编辑时 api_key 不回填明文，留空=不改（对齐后端）。
- 保留旧单配置行为的**迁移友好**：页面加载先 `api.aiProfiles()`，为空时提示"新增第一条 AI 配置"。

- [ ] **步骤 2：`pnpm tsc` + 手工验证**：能增删改、设默认、切换后列表默认标记更新；acp/codex 配置在对应命令不可用时灰显标注。

- [ ] **步骤 3：Commit** `git commit -am "feat(ui): multi AI-config manager page"`

---

### 任务 4：4 个 AI 入口接入选择器

**文件（各入口渲染 `<AiProviderSelector entry="...">` 并把选中的 profileId 传给对应 api 调用）：**
- `pages/StockAnalysis.tsx` —— entry=`stock_analysis`，调用 stock analysis stream 时带 profileId。
- `components/financials/AiAnalysisDialog.tsx` —— entry=`financial_analysis`。
- `pages/Review.tsx` —— entry=`market_recap`。
- 策略 AI 构建触发处（strategy 页/组件，调 `strategyBuild` → `/api/strategies/build`）—— entry=`strategy_build`。

- [ ] **步骤 1：逐入口加选择器 + 透传**

每入口：本地 `const [profileId, setProfileId] = useState<string>()`；渲染 `<AiProviderSelector entry="stock_analysis" value={profileId} onChange={setProfileId} />`；触发 AI 时把 `resolveEntryProfile(...)` 或 `profileId` 作为 `profileId` 传给 api 调用。

- [ ] **步骤 2：`pnpm tsc` + 手工验证四入口**

配 ≥2 条 profile，在个股分析选 A、财务分析选 B，各自请求带对应 profile_id（可从后端日志/响应 model 验证）；切换只影响本入口；刷新后各入口沿用上次选择；只有 1 条配置时选择器不显示。

- [ ] **步骤 3：Commit** `git commit -am "feat(ui): AI provider selector on 4 AI entrypoints"`

---

### 任务 5：收尾

- [ ] `pnpm tsc --noEmit` 全绿；`pnpm build` 通过。
- [ ] 端到端手测：新建 openai_compat + codex（+ 有 hermes 时 acp）三条 → 设默认 → 4 入口分别选不同 → 各走各的 → 删一条被某入口记住的 → 该入口静默回落默认不报错。
- [ ] Commit（若有）。

---

## 自检（规格覆盖）

- ✅ 多配置管理页（任务 3）
- ✅ 功能级记忆 + 全局默认兜底（任务 2 helper + 任务 4 接入）
- ✅ 4 个有 UI 的入口切换（任务 4）
- ✅ CLI/ACP 不可用灰显（任务 2/3）
- ⏸️ agent per-thread 选择器 —— agent 对话 UI 不存在，延后（后端已支持 profile_id）
- 后端 profile CRUD / 分派 / ACP → 见 `2026-07-03-multi-ai-provider-backend.md`
