// @vitest-environment jsdom
import { act } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { InstallDialog, PackageActions, PackageInstaller } from './extension'
import type { PluginDataSourceItem } from '@/lib/api'

const mocks = vi.hoisted(() => ({ inspect: vi.fn(), install: vi.fn(), manage: vi.fn(), route: vi.fn(), matrix: vi.fn(), toast: vi.fn(), close: vi.fn(), done: vi.fn(), refresh: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { inspectSourcePackage: mocks.inspect, installSourcePackage: mocks.install, manageSourcePackage: mocks.manage, updateDataProviders: mocks.route, capabilityMatrix: mocks.matrix } }))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))
let root: Root
let qc: QueryClient
const node = document.createElement('div')
document.body.appendChild(node)
const plugin = { name: 'demo', display_name: '演示源', origin: 'external', version: '1.0.0', available: true, enabled: true, status: '已就绪' } as PluginDataSourceItem
const preview = { manifest: { name: 'demo', display_name: '演示源', version: '1.0.0', datasets: ['daily'] }, digest: 'hash', errors: [], installed_version: null }
async function render(element = <InstallDialog onClose={mocks.close} onInstalled={mocks.done} />) {
  root = createRoot(node)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={qc}>{element}</QueryClientProvider></MemoryRouter>))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}
const button = (label: string) => [...node.querySelectorAll('button')].find(el => el.textContent === label)!
async function click(el: HTMLElement) { await act(async () => el.click()) }
async function choose() {
  const input = node.querySelector('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['zip'], 'demo.zip')] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(fn => fn.mockReset())
  mocks.inspect.mockResolvedValue(preview)
  mocks.install.mockResolvedValue({ name: 'demo', message: '安装完成' })
  mocks.matrix.mockResolvedValue({ capabilities: [{ id: 'daily', label: '日K', field: 'daily_data_provider', current: 'tickflow', default: 'tickflow', pending: [], current_display: '原数据源', candidates: [{ name: 'demo' }] }] })
  mocks.route.mockResolvedValue({})
})
afterEach(() => { act(() => root?.unmount()); qc?.clear(); node.innerHTML = '' })

it('preview cannot execute install until the user confirms trust; successful install selects the card', async () => {
  await render(); await choose()
  expect(node.textContent).toContain('演示源')
  expect(mocks.install).not.toHaveBeenCalled()
  expect(button('确认安装').disabled).toBe(true)
  await click(node.querySelector('input[type="checkbox"]')!)
  await click(button('确认安装'))
  expect(mocks.install).toHaveBeenCalledWith(expect.any(File), 'hash')
  expect(mocks.done).toHaveBeenCalledWith('demo')
})
it('incompatible package is visible but cannot install', async () => {
  mocks.inspect.mockResolvedValue({ ...preview, errors: ['Python 版本不兼容'] })
  await render(); await choose(); await click(node.querySelector('input[type="checkbox"]')!)
  expect(node.textContent).toContain('Python 版本不兼容')
  expect(button('确认安装').disabled).toBe(true)
})
it('blocks escape and closing during installation; failure stays in the dialog', async () => {
  let reject!: (e: Error) => void
  mocks.install.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
  await render(); await choose(); await click(node.querySelector('input[type="checkbox"]')!); await click(button('确认安装'))
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(mocks.close).not.toHaveBeenCalled()
  await act(async () => reject(new Error('加载失败')))
  expect(node.querySelector('[role="alert"]')?.textContent).toBe('加载失败')
  expect(mocks.done).not.toHaveBeenCalled()
})
it('the data-source install entry points to the unified import center', async () => {
  await render(<PackageInstaller refresh={mocks.done} selectSource={vi.fn()} />)
  const entry = node.querySelector<HTMLAnchorElement>('a')!
  expect(entry.textContent).toContain('安装插件')
  expect(entry.getAttribute('href')).toBe('/settings?tab=import&kind=source')
})
it('only selected routing fields are changed', async () => {
  await render(<PackageActions plugin={plugin} refresh={mocks.done} />)
  await click(button('选择启用能力'))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  expect(button('确认切换所选能力').disabled).toBe(true)
  expect(node.textContent).toContain('原数据源 → 演示源')
  await click(node.querySelector('input[type="checkbox"]')!)
  await click(button('确认切换所选能力'))
  expect(mocks.route).toHaveBeenCalledWith({ daily_data_provider: 'demo' })
})
it('failed removal shows routing references without closing confirmation', async () => {
  mocks.manage.mockRejectedValue(new Error('请先切换五档数据源'))
  await render(<PackageActions plugin={plugin} refresh={mocks.done} />)
  await click(button('卸载插件')); await vi.waitFor(() => expect(button('确认卸载').disabled).toBe(false)); await click(button('确认卸载'))
  expect(node.textContent).toContain('请先切换五档数据源')
  expect(node.querySelector('[role="dialog"]')).not.toBeNull()
})

const amberPlugin: PluginDataSourceItem = {
  name: 'amber', display_name: 'Amber', origin: 'external', version: '1.0.0',
  enabled: true, available: true, datasets: ['realtime'], runtime: 'none',
  status: '已就绪', description: '', install_hint: '',
}
const capability = {
  id: 'realtime', label: '实时行情', field: 'realtime_data_provider',
  current: 'amber', default: 'tickflow',
  candidates: [{ name: 'tickflow', display: 'TickFlow' }],
  pending: [],
}

it('lists the affected capabilities before a single confirmed uninstall', async () => {
  mocks.matrix.mockResolvedValue({ capabilities: [capability] })
  mocks.manage.mockResolvedValue({ name: 'amber', message: '已安排卸载' })
  await render(<PackageActions plugin={amberPlugin} refresh={mocks.refresh} />)
  await click(button('卸载插件'))
  await vi.waitFor(() => expect(button('确认卸载').disabled).toBe(false))
  const dialog = node.querySelector('[role="dialog"]')!
  expect(dialog.textContent).toContain('实时行情')
  expect(dialog.textContent).toContain('TickFlow')
  expect(dialog.textContent).toContain('无需提前逐项切换')
  expect(mocks.manage).not.toHaveBeenCalled()
  await click(button('确认卸载'))
  expect(mocks.manage).toHaveBeenCalledWith('amber', 'remove', true)
  expect(mocks.refresh).toHaveBeenCalledOnce()
  expect(node.querySelector('[role="dialog"]')).toBeNull()
})

it('blocks confirmation until the routing check succeeds and supports retry', async () => {
  mocks.matrix.mockResolvedValue({ capabilities: [capability] })
  await render(<PackageActions plugin={amberPlugin} refresh={mocks.refresh} />)
  mocks.matrix.mockRejectedValue(new Error('network unavailable'))
  await click(button('卸载插件'))
  await vi.waitFor(() => expect(node.querySelector('[role="alert"]')?.textContent).toContain('无法读取'))
  expect(button('确认卸载').disabled).toBe(true)
  expect(mocks.manage).not.toHaveBeenCalled()
  mocks.matrix.mockResolvedValue({ capabilities: [] })
  await click(button('重新读取能力'))
  await vi.waitFor(() => expect(button('确认卸载').disabled).toBe(false))
  expect(node.querySelector('[role="dialog"]')?.textContent).toContain('可直接卸载')
})

it('explains when a default source cannot take over and cancellation makes no changes', async () => {
  mocks.matrix.mockResolvedValue({ capabilities: [{ ...capability, candidates: [] }] })
  await render(<PackageActions plugin={amberPlugin} refresh={mocks.refresh} />)
  await click(button('卸载插件'))
  await vi.waitFor(() => expect(node.textContent).toContain('相应能力将暂停取数'))
  await click(button('取消'))
  expect(mocks.manage).not.toHaveBeenCalled()
  expect(node.querySelector('[role="dialog"]')).toBeNull()
})

it('keeps a failed uninstall available for retry without reporting success', async () => {
  mocks.matrix.mockResolvedValue({ capabilities: [capability] })
  mocks.manage.mockRejectedValue(new Error('插件目录不可写'))
  await render(<PackageActions plugin={amberPlugin} refresh={mocks.refresh} />)
  await click(button('卸载插件'))
  await vi.waitFor(() => expect(button('确认卸载').disabled).toBe(false))
  await click(button('确认卸载'))
  expect(node.querySelector('[role="dialog"] [role="alert"]')?.textContent).toContain('插件目录不可写')
  expect(mocks.refresh).not.toHaveBeenCalled()
  expect(button('确认卸载').disabled).toBe(false)
})

it('shows the restart status instead of controls after scheduling uninstall', async () => {
  await render(<PackageActions plugin={{ ...amberPlugin, pending_remove: true, enabled: false, available: false }} refresh={mocks.refresh} />)
  expect(node.textContent).toContain('卸载已安排')
  expect(node.textContent).toContain('API Key 保留')
  expect(button('卸载插件')).toBeUndefined()
  expect(button('选择启用能力')).toBeUndefined()
  expect(mocks.matrix).not.toHaveBeenCalled()
})
