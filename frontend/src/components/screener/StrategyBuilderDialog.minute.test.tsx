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
    strategyAiStatus: vi.fn(async () => ({ configured: true })),
    strategyBuildStream: vi.fn(),
    strategyAiIterate: vi.fn(),
  },
}))

const minuteCode = buildMinuteTemplate(BUILTIN_DEFAULT_BASIC_FILTER)
let root: Root
let container: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  vi.clearAllMocks()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  vi.mocked(api.strategyBuildStream).mockImplementation(async function* () {
    yield { type: 'result', valid: true, code: minuteCode, meta: {}, error: null }
  })
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

async function render(open = true) {
  await act(async () => {
    root.render(<StrategyBuilderDialog open={open} onClose={() => {}} />)
  })
}

async function click(text: string) {
  const button = [...container.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  expect(button, text).toBeTruthy()
  await act(async () => button!.click())
}

function input(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  act(() => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function seed(code = '', step = 1) {
  storage.strategyDraft.set({ name: '分钟测试', description: '', direction: 'long',
    rules: '一分钟上涨2%且成交量超过前20分钟均量3倍', code, step, strategyId: '', source: 'ai' })
}

it('sends the minute backend for generation and subsequent edits', async () => {
  seed()
  await render()
  await click('一分钟策略')
  expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain('一分钟策略')
  await click('AI 生成策略')
  expect(api.strategyBuildStream).toHaveBeenCalledWith(1, expect.objectContaining({ execution_backend: 'minute_filter' }))

  const instruction = container.querySelector('input[placeholder^="调整策略逻辑"]') as HTMLInputElement
  input(instruction, '涨幅改成3%')
  await click('AI 修改')
  expect(api.strategyBuildStream).toHaveBeenLastCalledWith(2, expect.objectContaining({
    execution_backend: 'minute_filter', instruction: '涨幅改成3%',
  }))
})

it('passes the selected backend through automatic iteration', async () => {
  seed()
  vi.mocked(api.strategyAiIterate).mockResolvedValue({
    draft_strategy_id: 'ai_minute', final_code: minuteCode, final_meta: {}, rounds: [],
  })
  await render()
  await click('一分钟策略')
  await click('AI 迭代（')
  await click('AI 迭代生成')
  expect(api.strategyAiIterate).toHaveBeenCalledWith(expect.objectContaining({ execution_backend: 'minute_filter' }))
})

it('switches unchanged templates while preserving edited custom code', async () => {
  await render()
  await click('自定义编写')
  await click('一分钟策略')
  const editor = container.querySelector('textarea') as HTMLTextAreaElement
  expect(editor.value).toBe(minuteCode)
  input(editor, minuteCode + '\n# 用户规则\n')
  await click('矩阵原生')
  expect(editor.value).toContain('# 用户规则')
  await click('一分钟策略')
  await click('使用模板')
  expect(editor.value).toBe(minuteCode)
  await click('Polars 表达式')
  expect(editor.value).toContain('def filter(')
})

it('restores old minute drafts without a backend field', async () => {
  seed(minuteCode)
  await render()
  expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain('一分钟策略')
  await render(false)
  await render()
  await click('重新生成')
  expect(api.strategyBuildStream).toHaveBeenCalledWith(1, expect.objectContaining({ execution_backend: 'minute_filter' }))
})

it('shows a failed generation and keeps the chosen backend for retry', async () => {
  seed()
  vi.mocked(api.strategyBuildStream).mockImplementation(async function* () {
    yield { type: 'error', message: '分钟生成失败' }
  })
  await render()
  await click('一分钟策略')
  await click('AI 生成策略')
  expect(container.textContent).toContain('分钟生成失败')
  expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain('一分钟策略')
  expect(storage.strategyDraft.get(null)?.rules).toContain('2%')
})

it('keeps manual minute authoring available when AI is not configured', async () => {
  vi.mocked(api.strategyAiStatus).mockResolvedValueOnce({ configured: false, has_key: false, has_model: false })
  await render()
  expect(container.textContent).toContain('AI API Key 未配置')
  await click('自定义编写')
  await click('一分钟策略')
  expect((container.querySelector('textarea') as HTMLTextAreaElement).value).toBe(minuteCode)
})
