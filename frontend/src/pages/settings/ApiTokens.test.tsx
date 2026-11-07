// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type ApiTokenRecord } from '@/lib/api'
import { SettingsApiTokensPanel } from './ApiTokens'

let root: Root
let host: HTMLDivElement
let client: QueryClient
let tokens: ApiTokenRecord[]

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request') }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  tokens = [
    { id: 'tok_first', name: '行情看板', scopes: ['read:market'], created_at: '2026-11-07T20:00:00', revoked: false },
    { id: 'tok_second', name: '另一个服务', scopes: ['read:analysis'], created_at: '2026-11-07T20:01:00', revoked: false },
  ]
  vi.spyOn(api, 'apiTokensList').mockImplementation(async () => ({ tokens }))
  vi.spyOn(api, 'apiTokenRevoke').mockImplementation(async id => {
    tokens = tokens.map(token => token.id === id ? { ...token, revoked: true } : token)
    return { status: 'revoked', id }
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(30) })
}

function button(label: string, container: ParentNode = host) {
  const match = [...container.querySelectorAll('button')].find(el => el.textContent?.trim() === label)
  expect(match).toBeTruthy()
  return match!
}

async function click(target: HTMLElement) {
  await act(async () => target.click())
  await tick()
}

async function open(index = 0) {
  await act(async () => root.render(<QueryClientProvider client={client}><SettingsApiTokensPanel /></QueryClientProvider>))
  await tick()
  const revoke = [...host.querySelectorAll('button')].filter(el => el.textContent?.trim() === '吊销')[index]
  revoke.focus()
  await click(revoke)
  const dialog = host.querySelector<HTMLElement>('[role="dialog"]')
  expect(dialog).not.toBeNull()
  return dialog!
}

it('opens confirmation for the selected Token without sending a revocation request', async () => {
  const dialog = await open(1)
  expect(dialog.textContent).toContain('另一个服务')
  expect(dialog.textContent).toContain('tok_second')
  expect(dialog.textContent).toContain('立即失效')
  expect(dialog.textContent).toContain('无法撤销')
  expect(document.activeElement).toBe(button('取消', dialog))
  expect(api.apiTokenRevoke).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})

it.each(['cancel', 'escape', 'backdrop'])('%s dismisses confirmation without revoking and restores focus', async method => {
  const dialog = await open()
  const trigger = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === '吊销')!
  if (method === 'cancel') await click(button('取消', dialog))
  if (method === 'escape') {
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    await tick()
  }
  if (method === 'backdrop') {
    await act(async () => {
      dialog.parentElement!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      dialog.parentElement!.click()
    })
    await tick()
  }
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(api.apiTokenRevoke).not.toHaveBeenCalled()
  expect(host.textContent).toContain('行情看板')
  expect(document.activeElement).toBe(trigger)
})

it('revokes only the selected Token after confirmation and refreshes the list', async () => {
  const dialog = await open(1)
  await click(button('确认吊销', dialog))
  expect(api.apiTokenRevoke).toHaveBeenCalledExactlyOnceWith('tok_second')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('行情看板')
  expect(host.textContent).not.toContain('另一个服务')
})

it('keeps confirmation open during the request, shows failures and allows retry', async () => {
  let reject!: (reason: Error) => void
  vi.mocked(api.apiTokenRevoke).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  const dialog = await open()
  await click(button('确认吊销', dialog))
  expect(button('吊销中…', dialog).disabled).toBe(true)
  expect(button('取消', dialog).disabled).toBe(true)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(host.querySelector('[role="dialog"]')).toBe(dialog)
  await click(button('吊销中…', dialog))
  expect(api.apiTokenRevoke).toHaveBeenCalledTimes(1)

  await act(async () => reject(new Error('网络连接失败')))
  await tick()
  expect(dialog.querySelector('[role="alert"]')?.textContent).toContain('网络连接失败')
  expect(host.textContent).toContain('行情看板')
  expect(button('确认吊销', dialog).disabled).toBe(false)

  await click(button('确认吊销', dialog))
  expect(api.apiTokenRevoke).toHaveBeenCalledTimes(2)
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).not.toContain('行情看板')
  expect(host.textContent).toContain('另一个服务')
})
