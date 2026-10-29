// @vitest-environment jsdom
// 回归: "扩展 official.signals 暂时不可用" — 条件右值为数字 (如迁移的残月信号
// right=0.6, 后端 isinstance(int,float) 一直合法) 时, 前端 c.right.startsWith
// 直接 TypeError, 整页渲染崩、错误边界兜底。用真实接口数据结构 + ExtensionBoundary
// 复刻页面挂载路径, 断言边界不再触发、条件正常显示。
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Signals } from '@/custom/signals/extension'
import { ConditionEditor } from '@/components/signals/ConditionEditor'
import { ExtensionBoundary } from '@/extensions/ExtensionBoundary'

const fixtures = vi.hoisted(() => ({
  options: {
    fields: [
      { key: 'close', label: '前复权收盘价' },
      { key: 'volume', label: '成交量' },
      { key: 'ma5', label: 'MA5' },
      { key: 'ma20', label: 'MA20' },
    ],
    groups: [{ label: '价格', fields: ['close', 'ma5', 'ma20'] }],
    maxDays: 60,
    operators: ['>', '>=', '<', '<=', '==', '!='],
    stringFields: ['symbol'],
    stringOperators: ['contains', '==', '!='],
  },
}))

vi.mock('@/lib/api', () => ({
  api: {
    customSignalsList: async () => ({
      signals: [
        {
          id: 'canyue_long_enter', name: '残月入场', kind: 'entry', timeframe: 'daily',
          conditions: [{ left: 'change_pct', op: '>', right: 0.6, leftDays: 0, rightDays: 0 }], // 数字右值 (真实迁移数据形态)
          column: 'signal_canyue_long_enter',
        },
        {
          id: 'ma_golden_5_20', name: '均线金叉', kind: 'both', timeframe: 'daily', enabled: true,
          conditions: [
            { left: 'ma5', op: '>', right: 'field:ma20', leftDays: 0, rightDays: 0 },
            { left: 'ma5', op: '>', right: 'field:ma5', leftDays: 0, rightDays: 1 },
          ],
          column: 'signal_ma_golden_5_20',
        },
      ],
    }),
    customSignalsOptions: async () => fixtures.options,
    strategyAiStatus: async () => ({ configured: false }),
    customSignalDelete: async () => ({ ok: true }),
  },
}))

let root: Root | null = null
let container: HTMLElement | null = null

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  root?.unmount()
  container?.remove()
})

function render(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root!.render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/signals']}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

it('信号库页: 数字右值条件不再触发错误边界, 正常渲染', async () => {
  render(
    <ExtensionBoundary extensionId="official.signals">
      <Signals />
    </ExtensionBoundary>,
  )
  await new Promise(r => setTimeout(r, 120))
  expect(container!.textContent).not.toContain('暂时不可用')
  expect(container!.textContent).toContain('信号库')
  // 数字右值条件正常显示 (不再因 .startsWith 崩溃)
  expect(container!.textContent).toContain('0.6')
})

it('条件编辑器: 数字右值不抛错, 常量输入正常挂载', async () => {
  render(
    <ConditionEditor
      conditions={[{ left: 'change_pct', op: '>', right: 0.6, leftDays: 0, rightDays: 0 }]}
      onChange={() => {}}
      options={fixtures.options as any}
    />,
  )
  await new Promise(r => setTimeout(r, 50))
  const input = container!.querySelector('input[type="number"]') as HTMLInputElement
  expect(input).not.toBeNull()
  expect(input.value).toBe('0.6')
})
