// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { SettingsState } from '@/lib/api'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({ settings: { data: {} as Partial<SettingsState> } }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings }))
vi.mock('@/lib/sponsor-discount', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/sponsor-discount')>(),
  useSponsorModelItems: () => null,
}))

let root: Root
let client: QueryClient
let host: HTMLDivElement

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  mocks.settings.data = {
    ai_provider: 'codex_cli', ai_configured: true, ai_codex_command: 'codex',
    ai_codex_model: '', ai_codex_reasoning_effort: '',
  }
})

afterEach(() => {
  act(() => root?.unmount())
  client?.clear()
  host?.remove()
  vi.unstubAllGlobals()
})

async function setup() {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={client}><MemoryRouter><SettingsAIPanel /></MemoryRouter></QueryClientProvider>,
  ))
}

async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === label)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
}

it('both test buttons show the complete routing error and clear it after a successful retry', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Method Not Allowed' }), { status: 405 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, model: 'codex_cli', response: 'OK' })))
  vi.stubGlobal('fetch', fetcher)
  await setup()
  await click('测试连接')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('前后端版本一致')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('POST /api/settings/ai/test')
  expect(host.querySelector('[role=status]')?.textContent).toContain('测试失败')
  await click('测试')
  expect(host.querySelector('[role=alert]')).toBeNull()
  expect(host.querySelector('[role=status]')?.textContent).toContain('连通成功')
  expect(fetcher).toHaveBeenCalledTimes(2)
  for (const [url, init] of fetcher.mock.calls) {
    expect(url).toBe('/api/settings/ai/test')
    expect(JSON.parse(init.body)).toEqual({ provider: 'codex_cli', model: '' })
  }
})

it('shows the full CLI login guidance without replacing it with a deployment error', async () => {
  const error = 'Codex CLI 调用失败: 401 Unauthorized\n请执行 codex login'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: false, error }))))
  await setup()
  await click('测试')
  expect(host.querySelector('[role=alert]')?.textContent).toBe(error)
})

it('edits a saved OpenAI configuration under Custom without an OpenAI preset card', async () => {
  mocks.settings.data = {
    ai_provider: 'openai',
    ai_protocol: 'responses',
    ai_base_url: 'https://api.openai.com/v1',
    ai_model: 'saved-openai-model',
    ai_reasoning_effort: 'xhigh',
    has_ai_key: true,
    ai_configured: true,
  }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true })))
  vi.stubGlobal('fetch', fetcher)
  await setup()

  const cards = [...host.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')]
  expect(cards.some(card => card.textContent?.startsWith('OpenAI'))).toBe(false)
  const [custom, modelflare] = cards
  expect(custom.textContent).toContain('自定义')
  expect(custom.getAttribute('aria-pressed')).toBe('true')
  await act(async () => modelflare.click())
  await act(async () => custom.click())

  await click('保存配置')
  expect(fetcher).toHaveBeenCalledTimes(1)
  const [url, init] = fetcher.mock.calls[0]
  expect(url).toBe('/api/settings/ai')
  expect(JSON.parse(init.body)).toMatchObject({
    provider: 'openai', protocol: 'responses', base_url: 'https://api.openai.com/v1',
    model: 'saved-openai-model', reasoning_effort: 'xhigh',
  })
  expect(JSON.parse(init.body)).not.toHaveProperty('api_key')
})

it('places Modelflare after Custom and tests its Chat Completions preset with its own key', async () => {
  mocks.settings.data = {
    ai_provider: 'openai_compat',
    ai_protocol: 'responses',
    ai_base_url: 'https://llm.runninghub.ai/v1',
    ai_model: 'saved-model',
    has_ai_key: true,
    ai_configured: true,
  }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, model: 'gpt-5.6-sol' })))
  vi.stubGlobal('fetch', fetcher)
  await setup()

  const [custom, modelflare, runningHub] = host.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')
  expect(custom.textContent).toContain('自定义')
  expect(modelflare.textContent).toContain('Modelflare')
  expect(modelflare.textContent).toContain('优惠')
  expect(runningHub.textContent).toContain('RunningHub')
  expect(runningHub.getAttribute('aria-pressed')).toBe('true')
  await act(async () => modelflare.click())

  expect(host.textContent).toContain('US$20 解锁 GPT 0.015x')
  expect(host.textContent).toContain('US$50 解锁 Claude 0.1x')
  expect(host.textContent).toContain('65% / 75% / 85%')
  expect(host.querySelector('code')?.textContent).toBe('f1cc942c2f674f568106bb244f0f1ac0')
  expect(host.textContent).toContain('每位用户限用一次')
  expect(host.querySelector('[aria-label="获取模型列表"]')).toBeNull()
  expect(host.querySelector<HTMLInputElement>('input[placeholder^="https://"]')?.value).toBe('https://modelflare.dev/v1')
  expect(host.querySelector<HTMLInputElement>('input[placeholder="gpt-5.6-sol"]')?.value).toBe('gpt-5.6-sol')
  expect([...host.querySelectorAll('button[aria-pressed="true"]')].some(
    button => button.textContent === 'Chat Completions',
  )).toBe(true)
  await click('测试')
  await click('测试连接')
  await click('保存配置')
  expect(fetcher).not.toHaveBeenCalled()

  const keyInput = host.querySelector<HTMLInputElement>('input[type="password"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(keyInput, 'test-modelflare-key')
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('测试连接')

  expect(fetcher).toHaveBeenCalledTimes(1)
  const [url, init] = fetcher.mock.calls[0]
  expect(url).toBe('/api/settings/ai/test')
  expect(JSON.parse(init.body)).toEqual({
    provider: 'openai_compat', protocol: 'chat_completions', base_url: 'https://modelflare.dev/v1',
    model: 'gpt-5.6-sol', api_key: 'test-modelflare-key',
  })
  expect(host.querySelector('[role=status]')?.textContent).toContain('连通成功')

  await act(async () => runningHub.click())
  expect(host.querySelector('[aria-label="获取模型列表"]')).not.toBeNull()
  expect(keyInput.value).toBe('')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '测试连接')?.disabled).toBe(false)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('offers the x6.ai preset and keeps an existing Kimi configuration available under Custom', async () => {
  mocks.settings.data = {
    ai_provider: 'openai_compat',
    ai_protocol: 'responses',
    ai_base_url: 'https://api.moonshot.cn/v1',
    ai_model: 'saved-kimi-model',
    has_ai_key: true,
    ai_configured: true,
  }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true })))
  vi.stubGlobal('fetch', fetcher)
  await setup()

  const cards = [...host.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')]
  const x6 = cards.find(card => card.textContent?.startsWith('x6.ai'))
  const baseUrl = host.querySelector<HTMLInputElement>('input[placeholder^="https://"]')!
  expect(cards.some(card => card.textContent?.startsWith('Kimi'))).toBe(false)
  expect(cards[0].textContent).toContain('自定义')
  expect(cards[0].getAttribute('aria-pressed')).toBe('true')
  expect(baseUrl.value).toBe('https://api.moonshot.cn/v1')
  expect(x6).toBeDefined()
  await act(async () => x6!.click())

  expect(baseUrl.value).toBe('https://api.x6.ai/v1')
  expect(host.querySelector('a[href="https://console.x6.ai/register?code=tuSkEebA"]')?.textContent).toContain('8 折、9 折')
  await click('测试连接')
  expect(fetcher).not.toHaveBeenCalled()

  const keyInput = host.querySelector<HTMLInputElement>('input[type="password"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(keyInput, 'test-x6-key')
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('测试连接')

  expect(fetcher).toHaveBeenCalledTimes(1)
  const [url, init] = fetcher.mock.calls[0]
  expect(url).toBe('/api/settings/ai/test')
  expect(JSON.parse(init.body)).toEqual({
    provider: 'openai_compat', protocol: 'chat_completions', base_url: 'https://api.x6.ai/v1',
    model: 'openai/gpt-6.2-sol-discount', api_key: 'test-x6-key',
  })
})

it('offers the OpenDesign Go preset with prefilled Chat Completions endpoint and default model', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, model: 'deepseek-v4.1-flash' })))
  vi.stubGlobal('fetch', fetcher)
  await setup()
  expect(host.querySelector('h2')?.textContent).toBe('AI 设置')

  const cards = [...host.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')]
  const openDesign = cards.find(card => card.textContent?.startsWith('OpenDesign Go'))
  expect(openDesign).toBeDefined()
  await act(async () => openDesign!.click())

  expect(host.querySelector<HTMLInputElement>('input[placeholder^="https://"]')?.value).toBe('https://amr-link.open-design.ai/v1')
  expect(host.querySelector<HTMLInputElement>('input[placeholder="gpt-5.6-sol"]')?.value).toBe('deepseek-v4.1-flash')
  expect(host.querySelector('a[href="https://go.open-design.ai/"]')?.textContent).toContain('go.open-design.ai')
  expect([...host.querySelectorAll('button[aria-pressed="true"]')].some(
    button => button.textContent === 'Chat Completions',
  )).toBe(true)

  const keyInput = host.querySelector<HTMLInputElement>('input[type="password"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(keyInput, 'test-opendesign-key')
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('测试连接')

  expect(fetcher).toHaveBeenCalledTimes(1)
  const [url, init] = fetcher.mock.calls[0]
  expect(url).toBe('/api/settings/ai/test')
  expect(JSON.parse(init.body)).toEqual({
    provider: 'openai_compat', protocol: 'chat_completions', base_url: 'https://amr-link.open-design.ai/v1',
    model: 'deepseek-v4.1-flash', api_key: 'test-opendesign-key',
  })
})
