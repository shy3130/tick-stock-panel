// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyBuilderDialog, buildMinuteTemplate } from './StrategyBuilderDialog'
import { BUILTIN_DEFAULT_BASIC_FILTER } from './DefaultStrategyParamsDialog'
import { storage } from '@/lib/storage'
import { api } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  friendlyStreamError: (message: string) => message,
  api: {
    strategyAiStatus: vi.fn(),
    strategyBuildStream: vi.fn(),
    strategyAiIterate: vi.fn(),
  },
}))

let root: Root
let container: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  vi.resetAllMocks()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  storage.strategyDraft.set({ name: '工具迭代', description: '', direction: 'long',
    rules: '一分钟上涨2%', code: '', step: 1, strategyId: '', source: 'ai' })
  vi.mocked(api.strategyAiStatus).mockResolvedValue({ configured: true, has_key: false,
    has_model: true, provider: 'codex_cli', supports_tools: true })
  vi.mocked(api.strategyAiIterate).mockResolvedValue({ draft_strategy_id: 'ai_tools',
    final_code: buildMinuteTemplate(BUILTIN_DEFAULT_BASIC_FILTER), final_meta: {}, rounds: [] })
  vi.mocked(api.strategyBuildStream).mockImplementation(async function* () {
    yield { type: 'result', valid: true, code: buildMinuteTemplate(BUILTIN_DEFAULT_BASIC_FILTER), meta: {}, error: null }
  })
})

afterEach(() => { act(() => root.unmount()); container.remove() })

async function render(open = true) {
  await act(async () => { root.render(<StrategyBuilderDialog open={open} onClose={() => {}} />) })
}

function button(text: string) {
  const element = [...container.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  expect(element).toBeTruthy()
  return element!
}

it('allows Codex iteration when the backend advertises tool support', async () => {
  await render()
  await act(async () => button('AI 迭代（').click())
  await act(async () => button('AI 迭代生成').click())
  expect(api.strategyAiIterate).toHaveBeenCalledOnce()
  expect(api.strategyBuildStream).not.toHaveBeenCalled()
  expect(container.textContent).not.toContain('不支持工具调用迭代')
})

it('shows the backtest failure beside its iteration and keeps a zero-trade result readable', async () => {
  vi.mocked(api.strategyAiIterate).mockResolvedValue({
    draft_strategy_id: 'ai_failed',
    final_code: buildMinuteTemplate(BUILTIN_DEFAULT_BASIC_FILTER), final_meta: {},
    rounds: [
      { round: 1, stats: null, backtest_error: '策略引用了不存在的信号 signal_missing', change_summary: '代码未变化，迭代停止' },
      { round: 2, stats: { total_return: 0, n_trades: 0, win_rate: 0 }, backtest_error: null, change_summary: '最终版回测' },
    ],
  })
  await render()
  await act(async () => button('AI 迭代（').click())
  await act(async () => button('AI 迭代生成').click())
  expect(container.textContent).toContain('策略引用了不存在的信号 signal_missing')
  const rows = container.querySelectorAll('tbody tr')
  expect(rows[0].textContent).toContain('回测失败')
  expect(rows[1].textContent).toContain('0.00%')
})

it('explains an unsupported adapter and keeps ordinary generation available', async () => {
  vi.mocked(api.strategyAiStatus).mockResolvedValue({ configured: true, has_key: true,
    has_model: true, provider: 'unknown', supports_tools: false })
  await render()
  expect(button('AI 迭代（').disabled).toBe(true)
  expect(container.textContent).toContain('普通生成')
  await act(async () => button('AI 生成策略').click())
  expect(api.strategyBuildStream).toHaveBeenCalledOnce()
  expect(api.strategyAiIterate).not.toHaveBeenCalled()
})

it('refreshes capability status after closing and reopening', async () => {
  vi.mocked(api.strategyAiStatus).mockResolvedValueOnce({ configured: true, has_key: true,
    has_model: true, provider: 'unknown', supports_tools: false })
  await render()
  expect(button('AI 迭代（').disabled).toBe(true)
  await render(false)
  await render()
  expect(api.strategyAiStatus).toHaveBeenCalledTimes(2)
  expect(button('AI 迭代（').disabled).toBe(false)
})

it('does not advertise Codex iteration against an older backend without capability metadata', async () => {
  vi.mocked(api.strategyAiStatus).mockResolvedValueOnce({ configured: true, has_key: false,
    has_model: true, provider: 'codex_cli' })
  await render()
  expect(button('AI 迭代（').disabled).toBe(true)
})
