// @vitest-environment jsdom
// 「默认基础参数」: 对话框编辑保存写 localStorage; 纯函数把默认参数渲染进
// 新建策略模板的 META.basic_filter 段 (Python 字面量)。
// 「一键应用到全部策略」: 二次确认后逐策略 PATCH basic_filter 并存为默认配置。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DefaultStrategyParamsDialog, BUILTIN_DEFAULT_BASIC_FILTER, loadDefaultBasicFilter } from './DefaultStrategyParamsDialog'
import { buildCustomTemplate, renderBasicFilterPy } from './StrategyBuilderDialog'
import { storage } from '@/lib/storage'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const mockPatchConfig = vi.fn(async (_id: string, _overrides: Record<string, unknown>) => ({ ok: true }))
vi.mock('@/lib/api', () => ({
  api: {
    screenerStrategies: vi.fn(async () => ({
      presets: [
        { id: 'strategy_a', timeframes: ['1d'] },
        { id: 'strategy_b', timeframes: ['1m'] },
      ],
    })),
    strategyPatchConfig: (id: string, ov: Record<string, unknown>) => mockPatchConfig(id, ov),
  },
}))

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

// 渲染前预取策略列表进缓存: query 同步命中, 避免 jsdom 下异步解析时序抖动
async function render(props: { show: boolean; onClose?: () => void }) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  await queryClient.fetchQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
  })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={queryClient}>
        <DefaultStrategyParamsDialog show={props.show} onClose={props.onClose ?? (() => {})} />
      </QueryClientProvider>,
    )
  })
}

beforeEach(() => {
  localStorage.clear()
  mockPatchConfig.mockClear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('renders nothing when show=false', async () => {
  await render({ show: false })

  expect(container.textContent).toBe('')
})

it('shows the basic filter fields when open', async () => {
  await render({ show: true })

  expect(container.textContent).toContain('默认基础参数')
  expect(container.textContent).toContain('价格')
  expect(container.textContent).toContain('流通市值')
  expect(container.textContent).toContain('板块')
  expect(container.textContent).toContain('排除')
})

it('save persists to localStorage and closes', async () => {
  const onClose = vi.fn()
  await render({ show: true, onClose })

  // 改价格下限: 第一对 min/max 输入 (RangeField 价格)
  // React 受控 input 需经原生 value setter + input 事件触发 onChange
  const input = container.querySelector('input[type="number"]') as HTMLInputElement
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(input, '3')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  const saveBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!
  act(() => { saveBtn.click() })

  const saved = storage.defaultStrategyBasicFilter.get(null)
  expect(saved?.price_min).toBe(3)
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(loadDefaultBasicFilter().price_min).toBe(3)
})

it('reset restores builtin defaults', async () => {
  storage.defaultStrategyBasicFilter.set({ ...BUILTIN_DEFAULT_BASIC_FILTER, price_min: 100 })
  await render({ show: true })

  const resetBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('恢复默认'))!
  act(() => { resetBtn.click() })

  expect((container.querySelector('input[type="number"]') as HTMLInputElement).value).toBe('5')
})

// ===== 模板注入 (纯函数) =====

it('renderBasicFilterPy emits Python literals for the template META', () => {
  const py = renderBasicFilterPy({
    ...BUILTIN_DEFAULT_BASIC_FILTER,
    price_min: 3, exclude_st: false, float_cap_max: null,
  })

  expect(py).toContain('"price_min": 3, "price_max": 200,')
  expect(py).toContain('"exclude_st": False,')
  expect(py).toContain('"float_cap_max": None,')
  expect(py).toContain('"boards": ["沪主板", "深主板", "创业板", "科创板"],')
})

it('buildCustomTemplate embeds custom defaults into the code template', () => {
  const code = buildCustomTemplate({ ...BUILTIN_DEFAULT_BASIC_FILTER, price_min: 3, turnover_min: 2 })

  expect(code).toContain('"basic_filter": {')
  expect(code).toContain('"price_min": 3,')
  expect(code).toContain('"turnover_min": 2,')
  expect(code).toContain('def filter(df: pl.DataFrame, params: dict) -> pl.Expr:')
})

// ===== 一键应用到全部策略 =====


it('apply button shows strategy count after list loads', async () => {
  await render({ show: true })

  const btn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('一键应用'))!
  expect(btn.textContent).toContain('2')
  // 未点击确认前不发起任何 PATCH
  expect(mockPatchConfig).not.toHaveBeenCalled()
})

it('apply requires confirm, then patches every strategy with current values', async () => {
  await render({ show: true })

  // 第一步: 点「一键应用」→ 出现二次确认, 尚未请求
  const applyBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('一键应用'))!
  await act(async () => { applyBtn.click() })
  expect(mockPatchConfig).not.toHaveBeenCalled()
  expect(container.textContent).toContain('确认覆盖')

  // 第二步: 确认 → 逐策略 PATCH, 值为当前表单值 (改过 price_min=3)
  const input = container.querySelector('input[type="number"]') as HTMLInputElement
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    nativeSetter.call(input, '3')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  const confirmBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('确认覆盖'))!
  await act(async () => { confirmBtn.click() })
  await act(async () => {})

  expect(mockPatchConfig).toHaveBeenCalledTimes(2)
  const ids = mockPatchConfig.mock.calls.map(c => c[0])
  expect(ids).toEqual(['strategy_a', 'strategy_b'])
  for (const call of mockPatchConfig.mock.calls) {
    expect(call[1]).toEqual({ basic_filter: { ...BUILTIN_DEFAULT_BASIC_FILTER, price_min: 3 } })
  }
  // 应用即所见: 同时存为默认配置
  expect(storage.defaultStrategyBasicFilter.get(null)?.price_min).toBe(3)
})
