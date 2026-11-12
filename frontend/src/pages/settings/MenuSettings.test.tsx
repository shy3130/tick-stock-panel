// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useQuery } from '@tanstack/react-query'
import { SettingsMenuSettingsPanel } from './MenuSettings'
import type { NavLayout } from '@/lib/navLayout'

vi.mock('@/extensions/registry', () => ({ getFrontendExtensionNavigation: () => [] }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => useQuery({ queryKey: QK.preferences, queryFn: api.preferences }) }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
let root: Root
let host: HTMLDivElement
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const prefs = { nav_order: ['/', '/watchlist', '/screener'], nav_hidden: [], nav_layout: null } as unknown as Preferences
  client.setQueryData(QK.preferences, prefs)
  vi.spyOn(api, 'preferences').mockResolvedValue(prefs)
  vi.spyOn(api, 'analysisMenus').mockResolvedValue({ items: [] })
  vi.spyOn(api, 'saveNavLayout').mockImplementation(async (nav_layout, nav_hidden) => ({ nav_layout, nav_hidden, nav_order: [] }))
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})
async function mount() {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}>
    <SettingsMenuSettingsPanel />
  </QueryClientProvider></MemoryRouter>))
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(x => x.textContent?.trim() === text)!
  expect(button).toBeTruthy()
  await act(async () => button.click())
}
async function fill(label: string, text: string) {
  const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function createGroup() {
  await fill('新分类名称', '交易工作台'); await click('新建分类')
  return host.querySelector<HTMLSelectElement>('select[aria-label="移动自选到"]')!.options[1].value
}
async function moveWatchlist(group: string) {
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="移动自选到"]')!
  await act(async () => { select.value = group; select.dispatchEvent(new Event('change', { bubbles: true })) })
}

it('pins home and atomically saves a new group, child move, and visibility', async () => {
  await mount()
  expect(host.querySelector<HTMLButtonElement>('button[aria-label="拖动看板"]')?.disabled).toBe(true)
  expect(host.querySelector('select[aria-label="移动看板到"]')).toBeNull()
  const group = await createGroup(); await moveWatchlist(group)
  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="隐藏数据"]')!.click())
  await click('保存布局')
  expect(api.saveNavLayout).toHaveBeenCalledOnce()
  const saved = vi.mocked(api.saveNavLayout).mock.calls[0][0]!
  expect(saved.items[0]).toEqual({ type: 'page', id: '/' })
  expect(saved.items.find(x => x.id === group)).toMatchObject({ label: '交易工作台', children: [{ type: 'page', id: '/watchlist' }] })
  expect(saved.items.filter(x => x.id === '/watchlist')).toHaveLength(0)
  expect(vi.mocked(api.saveNavLayout).mock.calls[0][1]).toEqual(['/data'])
})

it.each([false, true])('excludes fixed settings from editing and saving old layouts (nested: %s)', async nested => {
  const settings = { type: 'page' as const, id: '/settings' }
  const layout: NavLayout = { v: 1, items: [
    { type: 'page', id: '/' },
    { type: 'group', id: 'group:tools', label: '工具', children: [
      { type: 'page', id: '/watchlist' }, ...(nested ? [settings] : []),
    ] },
    ...(nested ? [] : [settings]),
  ] }
  client.setQueryData(QK.preferences, { nav_layout: layout, nav_hidden: ['/settings'], nav_order: [] })
  await mount()
  expect(host.querySelector('[data-nav-id="/settings"]')).toBeNull()
  expect(host.querySelector('select[aria-label="移动设置到"]')).toBeNull()
  await createGroup(); await click('保存布局')
  const [saved, hidden] = vi.mocked(api.saveNavLayout).mock.calls[0]
  expect(saved?.items.flatMap(node => node.type === 'group' ? node.children : [node]).map(node => node.id)).not.toContain('/settings')
  expect(hidden).not.toContain('/settings')
})

it('creates the category directly below home and focuses it instead of appending it offscreen', async () => {
  await mount()
  const group = await createGroup()
  expect([...host.querySelectorAll<HTMLElement>('[data-nav-id]')].slice(0, 2).map(x => x.dataset.navId))
    .toEqual(['/', group])
  expect(document.activeElement).toBe(host.querySelector('input[aria-label="分类名称：交易工作台"]'))
})

it('dissolves a group without deleting child pages', async () => {
  await mount(); const group = await createGroup(); await moveWatchlist(group)
  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="解散交易工作台"]')!.click())
  await click('保存布局')
  const saved = vi.mocked(api.saveNavLayout).mock.calls[0][0]!
  expect(saved.items.some(x => x.type === 'group')).toBe(false)
  expect(saved.items.some(x => x.id === '/watchlist')).toBe(true)
})

it('retains the draft after a failed save and permits retry', async () => {
  vi.mocked(api.saveNavLayout).mockRejectedValueOnce(new Error('保存失败，请重试'))
  await mount(); const group = await createGroup(); await moveWatchlist(group)
  await click('保存布局')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('保存失败')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="分类名称：交易工作台"]')?.value).toBe('交易工作台')
  await click('保存布局')
  expect(api.saveNavLayout).toHaveBeenCalledTimes(2)
})

it('preserves unavailable plugin references when saving unrelated changes', async () => {
  const layout: NavLayout = { v: 1, items: [{ type: 'page', id: '/' },
    { type: 'group', id: 'group:offline', label: '外部研究', children: [{ type: 'page', id: '/offline' }] }] }
  client.setQueryData(QK.preferences, { nav_layout: layout, nav_hidden: [], nav_order: [] })
  await mount()
  expect(host.textContent).toContain('页面暂不可用，归属已保留')
  await createGroup(); await click('保存布局')
  expect(vi.mocked(api.saveNavLayout).mock.calls[0][0]?.items).toContainEqual(layout.items[1])
})

it('creates groups on HTTP installations without crypto.randomUUID', async () => {
  vi.stubGlobal('crypto', {})
  await mount()
  expect(await createGroup()).toMatch(/^group:[a-zA-Z0-9_-]+$/)
})
