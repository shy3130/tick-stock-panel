// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({ settings: { data: {
  ai_provider: 'codex_cli', ai_configured: true, ai_codex_command: 'codex',
  ai_codex_model: '', ai_codex_reasoning_effort: '',
} } }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings }))
vi.mock('@/lib/sponsor-discount', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/sponsor-discount')>(),
  useSponsorModelItems: () => null,
}))

let root: Root
let client: QueryClient
let host: HTMLDivElement

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true))

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
