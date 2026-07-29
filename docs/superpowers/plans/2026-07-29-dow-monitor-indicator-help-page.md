# 趋势监控指标帮助页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在趋势监控顶部增加保留市场参数的“指标说明”入口，并提供结构化、可访问、与权威指标语义一致的独立帮助页。

**Architecture:** 新页面 `DowMonitorHelp` 是纯静态、懒加载的前端路由，不读取行情 API，也不建立 WebSocket。趋势监控使用当前 `market` 生成帮助链接，帮助页对查询参数做 `cn|hk|us` 白名单归一化并生成返回链接；内容和验收由现有 Dow Monitor 权威规格统一约束。

**Tech Stack:** React 19、TypeScript、React Router、Tailwind CSS、Lucide React、Vitest、Testing Library、pytest 规格合同。

## Global Constraints

- 路由固定为 `/dow-monitor/help?market=<cn|hk|us>`，非法或缺失市场回退到 `hk`。
- 入口只放在趋势监控 `PageHeader` 顶部操作区，不新增全局一级菜单。
- 页面不得调用行情 API、不得建立 WebSocket、不得生成或改变正式买卖信号。
- `实时`、`稳`、`--`、控制周期 15m → 30m → 缺失、确认 `0/2|1/2|2/2` 必须与权威规格一致。
- 窄屏速查表只能自身横向滚动，文档根节点不得产生横向溢出。
- 每个新需求的直接可执行证据仍只登记 `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`。
- 所有生产改动完成后更新 `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` 并发布新的唯一静态镜像。

---

### Task 1: 扩展权威规格和可执行合同

**Files:**

- Modify: `docs/specs/dow-monitor-list-websocket.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Modify: `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`
- Create: `docs/acceptance/dow-monitor-indicator-help.md`
- Create: `docs/reviews/dow-monitor-indicator-help.md`

**Interfaces:**

- Consumes: `USER-20260729-DOW-MONITOR-LIST-WEBSOCKET` 现有权威规格。
- Produces: 三个稳定需求 ID，以及唯一合同测试入口对页面行为测试的调用。

- [ ] **Step 1: 写失败的规格合同**

把合同中的需求集合扩展为：

```python
HELP_REQUIREMENTS = {
    "REQ-DOW-MONITOR-HELP-NAVIGATION-001",
    "REQ-DOW-MONITOR-HELP-CONTENT-001",
    "REQ-DOW-MONITOR-HELP-ACCESSIBILITY-001",
}
ALL_REQUIREMENTS = GROUP_REQUIREMENTS | HELP_REQUIREMENTS
```

合同的权威与追踪断言改为校验 `ALL_REQUIREMENTS`，行为命令增加：

```python
"src/pages/DowMonitorHelp.test.tsx",
```

- [ ] **Step 2: 运行合同并确认 RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
```

Expected: FAIL，因为三个帮助页需求尚未进入规格、追踪文件，测试文件也不存在。

- [ ] **Step 3: 扩展权威规格和索引**

在 `docs/specs/dow-monitor-list-websocket.md` 增加：

```markdown
### REQ-DOW-MONITOR-HELP-NAVIGATION-001

趋势监控页顶部必须提供“指标说明”链接，进入 `/dow-monitor/help` 并保留合法的
`cn|hk|us` 市场参数；帮助页必须提供保留同一市场的返回链接，非法或缺失参数回退 `hk`。

### REQ-DOW-MONITOR-HELP-CONTENT-001

帮助页必须解释快速决策顺序、四组指标、实时/稳定更新类型、缺失规则、组合场景、常见误区
和正式信号边界。页面不得把距离、ATR、盘口或实时动能表述为独立正式买卖信号。

### REQ-DOW-MONITOR-HELP-ACCESSIBILITY-001

帮助页必须使用语义标题和可键盘操作的锚点；桌面提供粘性目录，窄屏提供可滚动锚点；
速查表只允许自身横向滚动，文档整体不得横向溢出。
```

把三个 ID 加入 `docs/spec-index.yaml` 对应 specification 的 requirements。

- [ ] **Step 4: 增加追踪和待验收记录**

三个 `docs/traceability.yaml` 条目都必须：

```yaml
specification: USER-20260729-DOW-MONITOR-LIST-WEBSOCKET
tests:
  - {path: tests/spec_contracts/test_dow_monitor_list_websocket_contract.py, type: executable-test}
acceptance:
  - {path: docs/acceptance/dow-monitor-indicator-help.md, type: semantic-acceptance}
  - {path: docs/reviews/dow-monitor-indicator-help.md, type: independent-review}
```

实现路径分别登记：

- navigation：`frontend/src/pages/DowMonitor.tsx`、`frontend/src/pages/DowMonitorHelp.tsx`、`frontend/src/router.tsx`；
- content/accessibility：`frontend/src/pages/DowMonitorHelp.tsx`。

验收和复核文档此时明确写“实现中，尚未 PASS”，不得提前声明完成。

- [ ] **Step 5: 运行规格检查**

Run:

```powershell
python scripts/check_spec_compliance.py
```

Expected: 新增需求没有问题；只允许仓库已记录的过期 collection-monitor exception 和旧 detail-toggle 测试路径两项基线。

- [ ] **Step 6: 提交规格**

```powershell
git add docs/specs/dow-monitor-list-websocket.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_list_websocket_contract.py docs/acceptance/dow-monitor-indicator-help.md docs/reviews/dow-monitor-indicator-help.md
git commit -m "docs(dow-monitor): specify indicator help page"
```

---

### Task 2: 实现独立帮助页和可访问内容

**Files:**

- Create: `frontend/src/pages/DowMonitorHelp.tsx`
- Create: `frontend/src/pages/DowMonitorHelp.test.tsx`

**Interfaces:**

- Consumes: URL 查询参数 `market`。
- Produces: `export function DowMonitorHelp(): JSX.Element`，纯静态页面，不消费 API。

- [ ] **Step 1: 写帮助页失败测试**

测试用 `MemoryRouter initialEntries={['/dow-monitor/help?market=cn']}` 渲染页面，至少断言：

```tsx
expect(screen.getByRole('heading', { level: 1, name: '趋势监控指标说明' })).toBeInTheDocument()
expect(screen.getByRole('link', { name: '返回趋势监控' }))
  .toHaveAttribute('href', '/dow-monitor?market=cn')
for (const heading of ['快速决策路径', '趋势 / 位置', '动能 / 涨速', '量价 / 资金', '突破 / 风险', '典型组合场景', '指标速查表']) {
  expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
}
expect(screen.getAllByText('实时').length).toBeGreaterThan(0)
expect(screen.getAllByText('稳').length).toBeGreaterThan(0)
expect(screen.getByText(/0\/2、1\/2、2\/2/)).toBeInTheDocument()
```

另一个样例以 `market=invalid` 渲染并断言返回链接为 `/dow-monitor?market=hk`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: FAIL，因为 `DowMonitorHelp` 尚不存在。

- [ ] **Step 3: 实现市场归一化和页面外壳**

页面顶部使用：

```tsx
import { ArrowLeft, BookOpenCheck } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'

type HelpMarket = 'cn' | 'hk' | 'us'

function normalizeMarket(value: string | null): HelpMarket {
  return value === 'cn' || value === 'us' ? value : 'hk'
}

export function DowMonitorHelp() {
  const [searchParams] = useSearchParams()
  const market = normalizeMarket(searchParams.get('market'))
  return (
    <main className="min-h-full min-w-0 bg-base" data-testid="dow-monitor-help-page">
      <PageHeader
        title="趋势监控指标说明"
        subtitle="先看正式信号，再判断趋势、动能、量价与执行风险"
        right={(
          <Link to={`/dow-monitor?market=${market}`} aria-label="返回趋势监控">
            <ArrowLeft aria-hidden="true" />返回趋势监控
          </Link>
        )}
      />
      {/* 目录和正文 */}
    </main>
  )
}
```

- [ ] **Step 4: 实现七段结构化正文**

正文必须包含：

1. `#quick-start`：正式信号 → 趋势位置 → 动能量价 → 突破风险；
2. `#trend-position`：通道、控制、成本；
3. `#momentum-speed`：1m、5m、15m；
4. `#volume-funds`：量比、量速、主买、五档；
5. `#breakout-risk`：高、低、ATR14、确认；
6. `#scenarios`：向上突破候选、向下破位风险、假突破警惕、继续观察；
7. `#quick-reference`：指标速查表。

每个指标使用统一的可扫描行：

```tsx
<article>
  <div>
    <h3>ATR14</h3>
    <span>稳</span>
  </div>
  <p>15m 最近 14 个有效真实波幅相对最新完成收盘价的百分比。</p>
  <p><strong>怎么看：</strong>越大表示短线波动风险越高，不代表上涨或下跌。</p>
  <p><strong>避免误读：</strong>不能单独作为买卖方向。</p>
</article>
```

`1m`、`量速`、`五档`、`高`、`低`必须分别带可见 `实时`；控制、成本、5m、15m、量比、主买、ATR14、确认分别带 `稳`。

- [ ] **Step 5: 实现目录和窄屏局部滚动**

桌面目录使用 `lg:sticky lg:top-4`，移动锚点容器使用 `overflow-x-auto`。速查表必须包在：

```tsx
<div data-testid="indicator-reference-scroll" className="max-w-full overflow-x-auto">
  <table className="min-w-[760px]">...</table>
</div>
```

根节点保持 `min-w-0` 和 `overflow-x-clip`，标题锚点使用 `scroll-mt-20`。

- [ ] **Step 6: 运行页面测试**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: PASS。

- [ ] **Step 7: 提交帮助页**

```powershell
git add frontend/src/pages/DowMonitorHelp.tsx frontend/src/pages/DowMonitorHelp.test.tsx
git commit -m "feat(dow-monitor): add indicator help page"
```

---

### Task 3: 接入路由和趋势监控顶部入口

**Files:**

- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/pages/DowMonitor.tsx`
- Modify: `frontend/src/pages/DowMonitor.test.tsx`
- Modify: `frontend/src/pages/dow-monitor-route.test.tsx`

**Interfaces:**

- Consumes: `DowMonitorHelp` 懒加载页面和当前 `market` 状态。
- Produces: `/dow-monitor/help` 路由及 `指标说明` 顶部链接。

- [ ] **Step 1: 写路由和入口失败测试**

在 `DowMonitor.test.tsx` 当前 `market=hk` 基础上增加：

```tsx
expect(screen.getByRole('link', { name: '指标说明' }))
  .toHaveAttribute('href', '/dow-monitor/help?market=hk')
```

切换 A 股和美股后分别断言 `market=cn`、`market=us`。

在路由测试断言：

```tsx
expect(routePaths()).toContain('dow-monitor/help')
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitor.test.tsx src/pages/dow-monitor-route.test.tsx
```

Expected: FAIL，因为入口和路由尚不存在。

- [ ] **Step 3: 增加懒加载路由**

在 `router.tsx` 增加：

```tsx
const DowMonitorHelp = lazy(() => import('./pages/DowMonitorHelp').then(m => ({ default: m.DowMonitorHelp })))
```

子路由增加：

```tsx
{ path: 'dow-monitor/help', element: <DowMonitorHelp /> },
```

- [ ] **Step 4: 增加顶部入口**

`DowMonitor.tsx` 引入 `BookOpen` 和 `Link`，把 `PageHeader.right` 的表单外层改为一个横向操作容器：

```tsx
<div className="flex items-center gap-2">
  <Link
    to={`/dow-monitor/help?market=${market}`}
    aria-label="指标说明"
    className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-muted transition-colors hover:bg-elevated hover:text-foreground"
  >
    <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
    指标说明
  </Link>
  <form>{/* 现有添加股票表单保持不变 */}</form>
</div>
```

不得改变添加股票、候选列表或分页逻辑。

- [ ] **Step 5: 运行入口、路由和现有页面测试**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitorHelp.test.tsx src/pages/DowMonitor.test.tsx src/pages/dow-monitor-route.test.tsx
```

Expected: PASS。

- [ ] **Step 6: 提交路由和入口**

```powershell
git add frontend/src/router.tsx frontend/src/pages/DowMonitor.tsx frontend/src/pages/DowMonitor.test.tsx frontend/src/pages/dow-monitor-route.test.tsx
git commit -m "feat(dow-monitor): link indicator help from monitor"
```

---

### Task 4: 完整验证、语义验收、运行手册与发布

**Files:**

- Modify: `docs/acceptance/dow-monitor-indicator-help.md`
- Modify: `docs/reviews/dow-monitor-indicator-help.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`
- Create temporarily: `output/release/Dockerfile`

**Interfaces:**

- Consumes: Tasks 1–3 和 `frontend/dist`。
- Produces: 可复核验收证据和唯一生产静态镜像。

- [ ] **Step 1: 运行完整聚焦测试**

```powershell
pnpm --dir frontend exec vitest run `
  src/pages/DowMonitorHelp.test.tsx `
  src/pages/DowMonitor.test.tsx `
  src/pages/dow-monitor-route.test.tsx `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/lib/realtimeMarketData.test.ts

python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
pnpm --dir frontend build
python scripts/check_spec_compliance.py
```

Expected: 测试和构建 PASS；规格守卫只保留两项既有基线。

- [ ] **Step 2: 浏览器语义验收**

在已登录页面检查：

1. HK 趋势监控顶部“指标说明”链接为 `market=hk`；
2. A 股、美股切换后链接分别保留 `cn`、`us`；
3. 帮助页可直接打开并返回相同市场；
4. 七个正文区和所有指标存在；
5. 目录可键盘操作；
6. 390px 与 1800px 视口均无文档整体横向溢出，速查表自身可滚动；
7. 打开帮助页没有新增 Dow Monitor API/WebSocket 请求；
8. 回到趋势监控后正式信号和列表状态正常。

- [ ] **Step 3: 填写验收、独立复核和运行手册**

验收记录必须包含：

- 测试命令及实际数量；
- 构建资产名称和 SHA-256；
- 三市场链接与返回路径；
- 桌面/移动布局尺寸；
- 无新增 API/WebSocket 的网络证据；
- 内容逐项对照权威规格的结果。

独立复核表使用：

```markdown
| Requirement | Implementation | Executable test | Semantic evidence | Result |
```

每个结果只有在实现、测试和浏览器证据齐全时才能写 `PASS`。

- [ ] **Step 4: 提交验收文档**

```powershell
git add docs/acceptance/dow-monitor-indicator-help.md docs/reviews/dow-monitor-indicator-help.md
git commit -m "docs(dow-monitor): record indicator help acceptance"
```

- [ ] **Step 5: 构建并部署唯一镜像**

使用当前生产镜像作为 `BASE_IMAGE`，只复制新 `frontend/dist` 到 `/app/static`。候选 tag 必须包含本次源码 revision 和时间戳。切换前备份：

- 当前容器 inspect；
- 当前 compose 文件和项目名；
- `dow_monitor_symbols.json`；
- 回滚镜像 tag；
- 股票清单 SHA-256。

部署后验证：

- 容器使用新 tag，`running`、restart `0`；
- `/health` 成功；
- 股票清单 SHA-256 不变；
- 日志无 `ERROR|CRITICAL|Traceback`；
- 本地、容器、HTTP 的 `index.html` 和帮助页 chunk 哈希一致；
- 全新防缓存已登录标签页显示帮助入口和帮助页，控制台无候选包错误。

若任一检查失败，立即使用备份的 compose 项目和回滚镜像恢复，不声明完成。

- [ ] **Step 6: 更新最终发布证据**

把新镜像 tag、镜像 ID、回滚 tag、备份目录、静态哈希、生产浏览器结果补入验收、复核和运行手册；若文档发生变化，再提交：

```powershell
git add docs/acceptance/dow-monitor-indicator-help.md docs/reviews/dow-monitor-indicator-help.md
git commit -m "docs(dow-monitor): record indicator help release"
```

- [ ] **Step 7: 最终独立复核**

由未参与实现的复核者从三个需求 ID 逐项检查权威规格、实现、可执行测试、语义证据和生产状态。截图只能作为外观辅助，不能替代路由、参数、网络边界和内容准确性证据。
