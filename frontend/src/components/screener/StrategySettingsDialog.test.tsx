// @vitest-environment jsdom
// 策略设置弹窗 (分区导航版) 交互回归:
// - 左侧分区导航: 默认「策略条件」页, 点击切换页面显隐
// - 「未保存的改动」提示: 编辑出现 (导航圆点 + 底部文案), 改回原值即消失
// - 保存 payload: 名称/基础参数/评分/叠加条件按后端契约组装
// - 分钟策略: 叠加条件区显示「不支持」文案
// - 叠加策略: 导航只剩「子策略与权重」
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategySettingsDialog } from './StrategySettingsDialog'
import type { StrategyDetail } from '@/lib/api'

const mocks = vi.hoisted(() => ({
  saveConfig: vi.fn(async (_id: string, _payload: Record<string, unknown>) => ({ ok: true })),
  detail: null as unknown,
}))

vi.mock('@/lib/api', () => ({
  api: {
    strategyGet: vi.fn(async () => mocks.detail),
    strategySaveConfig: mocks.saveConfig,
    strategyResetConfig: vi.fn(async () => ({ ok: true })),
    strategyDelete: vi.fn(async () => ({ ok: true })),
    strategyGetSource: vi.fn(async () => ({ code: '', source: 'custom' })),
    customSignalsOptions: vi.fn(async () => ({ fields: [{ key: 'close', label: '收盘价' }], groups: [], stringFields: [] })),
    customSignalsList: vi.fn(async () => ({ signals: [] })),
    factorColumns: vi.fn(async () => ({ columns: [] })),
    screenerStrategies: vi.fn(async () => ({ presets: [] })),
  },
}))

function makeDetail(overrides: Partial<StrategyDetail> = {}): StrategyDetail {
  return {
    id: 'test_strategy',
    name: '测试策略',
    description: '测试描述',
    tags: [],
    source: 'custom',
    execution_backend: 'polars_expr',
    asset_types: ['stock'],
    timeframes: ['1d'],
    version: '1',
    basic_filter: { price_min: 5, price_max: 200, boards: ['沪主板'], exclude_st: true, enabled: true },
    params: [{ id: 'min_limit_ups', label: '最少涨停次数', type: 'int', default: 3 }],
    params_defaults: { min_limit_ups: 3 },
    scoring: { momentum_20d: 1 },
    scoring_directions: { momentum_20d: 'high' },
    entry_signals: ['signal_ma_cross'],
    exit_signals: [],
    overlay_filter: [],
    minute_exit_trigger_supported_signals: [],
    stop_loss: -0.08,
    take_profit: null,
    trailing_stop: null,
    trailing_take_profit_activate: null,
    trailing_take_profit_drawdown: null,
    max_hold_days: 25,
    order_by: 'score',
    descending: true,
    limit: 50,
    ...overrides,
  } as StrategyDetail
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

async function render(props: { onClose?: () => void } = {}) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <StrategySettingsDialog strategyId="test_strategy" onClose={props.onClose ?? (() => {})} />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
  // 冲掉 strategyGet / 字段选项等异步链
  await act(async () => {})
}

const findButton = (text: string) =>
  [...container.querySelectorAll('button')].find(b => b.textContent?.includes(text))!

// 分区页判隐: 页说明段落 → 内层容器 → 页 wrapper
function pageHidden(introText: string) {
  const p = [...container.querySelectorAll('p')].find(el => el.textContent?.includes(introText))
  if (!p) throw new Error(`page intro not found: ${introText}`)
  return p.parentElement!.parentElement!.className.includes('hidden')
}

function setNumberInput(input: HTMLInputElement, v: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    setter.call(input, v)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  mocks.saveConfig.mockClear()
  mocks.detail = makeDetail()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('renders section nav with 策略条件 page active by default', async () => {
  await render()

  expect(container.textContent).toContain('策略条件')
  expect(container.textContent).toContain('策略权重')
  expect(container.textContent).toContain('纪律与触发')
  expect(container.textContent).toContain('基础参数')
  expect(container.textContent).toContain('叠加条件')
  // 默认页可见, 其他分区隐藏 (常驻挂载避免编辑草稿丢失)
  expect(pageHidden('决定哪些股票进入候选')).toBe(false)
  expect(pageHidden('对通过筛选的候选按因子加权打分排序')).toBe(true)
  expect(pageHidden('控制买卖时点')).toBe(true)
})

it('switches pages through the nav rail', async () => {
  await render()

  act(() => { findButton('策略权重').click() })
  expect(pageHidden('对通过筛选的候选按因子加权打分排序')).toBe(false)
  expect(pageHidden('决定哪些股票进入候选')).toBe(true)

  act(() => { findButton('纪律与触发').click() })
  expect(pageHidden('控制买卖时点')).toBe(false)
  expect(pageHidden('对通过筛选的候选按因子加权打分排序')).toBe(true)
})

it('shows unsaved-change hint on edit and clears it when reverted', async () => {
  await render()
  act(() => { findButton('纪律与触发').click() })

  const stopLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '止损')!
  const stopInput = stopLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement
  // 百分比口径显示: 存储 -0.08 → 显示 8
  expect(stopInput.value).toBe('8')
  expect(container.textContent).not.toContain('有未保存的改动')

  setNumberInput(stopInput, '9')
  expect(container.textContent).toContain('有未保存的改动')
  const navBtn = [...container.querySelectorAll('nav button')].find(b => b.textContent?.includes('纪律与触发'))!
  expect(navBtn.querySelector('.bg-amber-400')).toBeTruthy()

  setNumberInput(stopInput, '8')
  expect(container.textContent).not.toContain('有未保存的改动')
  expect(navBtn.querySelector('.bg-amber-400')).toBeFalsy()
})

it('trading page exposes full risk fields and save carries them', async () => {
  await render()
  act(() => { findButton('纪律与触发').click() })

  // 与回测抽屉同构的 6 项纪律字段
  for (const label of ['止损', '止盈', '移动止损', '回撤止盈启动', '回撤止盈回撤', '最长持仓']) {
    expect([...container.querySelectorAll('span')].some(s => s.textContent === label)).toBe(true)
  }

  // 填止盈 15% 与回撤止盈 20/5 → 保存 payload 携带对应小数
  const tpLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '止盈')!
  setNumberInput(tpLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement, '15')
  const taLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '回撤止盈启动')!
  setNumberInput(taLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement, '20')
  const tdLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '回撤止盈回撤')!
  setNumberInput(tdLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement, '5')

  await act(async () => { findButton('保存设置').click() })
  const [, payload] = mocks.saveConfig.mock.calls[0]
  expect(payload).toMatchObject({
    stop_loss: -0.08,
    take_profit: 0.15,
    trailing_take_profit_activate: 0.2,
    trailing_take_profit_drawdown: 0.05,
    max_hold_days: 25,
  })
})

it('drawdown clamps to activate value (与回测抽屉同联动)', async () => {
  await render()
  act(() => { findButton('纪律与触发').click() })

  const taLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '回撤止盈启动')!
  const tdLabel = [...container.querySelectorAll('span')].find(s => s.textContent === '回撤止盈回撤')!
  const tdInput = tdLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement
  // 先设回撤 30, 再设启动 20 → 回撤被钳到 20
  setNumberInput(tdInput, '30')
  setNumberInput(taLabel.parentElement!.querySelector('input[type="number"]') as HTMLInputElement, '20')
  expect(tdInput.value).toBe('20')
})

it('save submits assembled payload and closes', async () => {
  const onClose = vi.fn()
  await render({ onClose })

  await act(async () => { findButton('保存设置').click() })

  expect(mocks.saveConfig).toHaveBeenCalledTimes(1)
  const [id, payload] = mocks.saveConfig.mock.calls[0]
  expect(id).toBe('test_strategy')
  expect(payload).toMatchObject({
    name: '测试策略',
    stop_loss: -0.08,
    max_hold_days: 25,
    entry_signals: ['signal_ma_cross'],
    exit_signals: [],
    display_limit: null,
    overlay_filter: [],
  })
  expect(payload.basic_filter).toMatchObject({ price_min: 5, enabled: true })
  expect(payload.scoring).toEqual({ momentum_20d: 1 })
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('minute strategy shows overlay unsupported message', async () => {
  mocks.detail = makeDetail({ execution_backend: 'minute_filter' })
  await render()

  expect(container.textContent).toContain('不支持叠加条件')
})

it('overlay conditions expose 最新/前N日 offset and save carries it', async () => {
  await render()

  // 叠加条件区添加一条 (默认 left=close, right='0' 常量) → 左侧出现「最新」按钮
  act(() => { findButton('添加条件').click() })
  const latest = [...container.querySelectorAll('button')].find(b => b.textContent === '最新')
  expect(latest).toBeTruthy()

  // 切为「前1日」(与信号库同一控件): 出现 前[1]日 输入
  act(() => { latest!.click() })
  expect([...container.querySelectorAll('span')].some(s => s.textContent === '前')).toBe(true)

  await act(async () => { findButton('保存设置').click() })
  const [, payload] = mocks.saveConfig.mock.calls[0]
  expect((payload.overlay_filter as any[])[0]).toMatchObject({
    left: 'close', op: '>', right: '0', leftDays: 1, rightDays: 0,
  })
})

it('the last overlay condition is deletable back to 不叠加', async () => {
  mocks.detail = makeDetail({
    overlay_filter: [{ left: 'ext_ext_rq_ths_rank', op: '<=', right: '1000', leftDays: 0, rightDays: 0 }],
  })
  await render()

  // 仅一条时删除钮仍可用 (默认空 = 不叠加)
  const remove = container.querySelector<HTMLButtonElement>('button[aria-label="删除条件"]')!
  expect(remove).toBeTruthy()
  act(() => { remove.click() })
  expect(container.textContent).toContain('无条件 — 不叠加过滤')

  await act(async () => { findButton('保存设置').click() })
  const [, payload] = mocks.saveConfig.mock.calls[0]
  expect(payload.overlay_filter).toEqual([])
})

it('composite strategy only exposes the children section', async () => {
  mocks.detail = makeDetail({
    source: 'composite',
    execution_backend: 'composite',
    composite_children: [{ id: 'child_a', name: '子策略A', source: 'custom', weight: 1 }],
  })
  await render()

  expect(container.textContent).toContain('子策略与权重')
  expect(container.textContent).not.toContain('策略条件')
  expect(pageHidden('叠加策略按权重融合子策略')).toBe(false)
  expect(container.textContent).toContain('子策略A')
})
