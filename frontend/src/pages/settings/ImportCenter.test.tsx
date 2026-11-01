// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsImportCenterPanel } from './ImportCenter'

const mocks = vi.hoisted(() => ({
  inspect: vi.fn(), install: vi.fn(), backup: vi.fn(), resources: vi.fn(), source: vi.fn(),
  packages: vi.fn(), pages: vi.fn(), sources: vi.fn(), namespaces: vi.fn(), matrix: vi.fn(),
  manage: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: {
  importCenterInspect: mocks.inspect, installExtensionPackage: mocks.install,
  configBackupApply: mocks.backup, resourcesApply: mocks.resources, installSourcePackage: mocks.source,
  extensionPackages: mocks.packages, extPagesList: mocks.pages, dataSources: mocks.sources,
  extPagesSourceNamespaces: mocks.namespaces,
  capabilityMatrix: mocks.matrix,
  manageExtensionPackage: mocks.manage,
} }))
vi.mock('@/custom/source-packages/extension', () => ({ PackageActions: () => null }))
vi.mock('./DataSources', () => ({ PluginKeyConfig: () => null }))
vi.mock('@/extensions/registry', () => ({ getFrontendExtensionLoadErrors: () => [] }))

const extension = {
  kind: 'extension',
  preview: {
    manifest: { id: 'demo.backend', display_name: '研究服务', version: '1.0.0', backend: 'backend.extension', author: '作者声明' },
    digest: 'digest', revision: 'revision', files: ['extension.yaml', 'backend/extension.py'], errors: [], installed_version: null,
  },
}
const backup = { kind: 'backup', preview: { created_at: '2026-11-01', app_version: '0.3.25', has_secrets: true, total_files: 3, conflict_files: 0, out_of_scope_files: 0,
  categories: [{ key: 'watchlist', label: '自选股', count: 1, conflicts: 0 }, { key: 'secrets', label: '密钥', count: 1, conflicts: 0 }, { key: 'auth', label: '访问凭证', count: 1, conflicts: 0 }] } }
let root: Root
let qc: QueryClient
const node = document.createElement('div')
document.body.appendChild(node)
const button = (text: string) => [...node.querySelectorAll('button')].find(el => el.textContent?.trim() === text)!
async function click(el: HTMLElement) { expect(el).toBeTruthy(); await act(async () => el.click()) }
async function render(url = '/settings?tab=import') {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  root = createRoot(node)
  await act(async () => root.render(<MemoryRouter initialEntries={[url]}><QueryClientProvider client={qc}><SettingsImportCenterPanel /></QueryClientProvider></MemoryRouter>))
}
async function pick(filename = 'extension.zip') {
  const input = node.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['data'], filename)] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(fn => fn.mockReset())
  mocks.inspect.mockResolvedValue(extension)
  mocks.install.mockResolvedValue({ id: 'demo.backend', message: '扩展包已保存，重启主程序后生效' })
  mocks.backup.mockResolvedValue({ categories: [{ key: 'watchlist', label: '自选股', imported: 1, skipped_conflict: 0, failed: [] }], skipped_unselected: 2, out_of_scope_files: 0 })
  mocks.packages.mockResolvedValue([])
  mocks.pages.mockResolvedValue([])
  mocks.sources.mockResolvedValue({ plugins: [] })
  mocks.namespaces.mockResolvedValue([])
  mocks.matrix.mockResolvedValue({ capabilities: [] })
  mocks.manage.mockResolvedValue({ id: 'demo.backend', message: '变更已保存，重启后生效' })
})
afterEach(() => { act(() => root?.unmount()); qc?.clear(); node.innerHTML = '' })

it('one file entry only previews; backend installation requires explicit trust and retains the digest/revision', async () => {
  await render(); await pick()
  expect(mocks.install).not.toHaveBeenCalled()
  expect(button('确认安装').disabled).toBe(true)
  await click(node.querySelector<HTMLInputElement>('[aria-label="信任代码来源"]')!)
  await click(button('确认安装'))
  expect(mocks.install).toHaveBeenCalledWith(expect.any(File), 'digest', 'revision')
  expect(node.textContent).toContain('重启主程序后生效')
})

it('compatibility errors block a trusted package and a failed request preserves its review', async () => {
  mocks.inspect.mockResolvedValue({ ...extension, preview: { ...extension.preview, errors: ['需要更高主程序版本'] } })
  await render(); await pick(); await click(node.querySelector<HTMLInputElement>('[aria-label="信任代码来源"]')!)
  expect(button('确认安装').disabled).toBe(true)
  expect(node.textContent).toContain('需要更高主程序版本')
})

it('each backup operation starts without keys/passwords and applies only explicitly selected categories', async () => {
  mocks.inspect.mockResolvedValue(backup)
  await render(); await pick('backup.zip')
  const secret = () => node.querySelector<HTMLInputElement>('[aria-label="恢复密钥"]')!
  const auth = () => node.querySelector<HTMLInputElement>('[aria-label="恢复访问凭证"]')!
  expect(secret().checked).toBe(false); expect(auth().checked).toBe(false)
  await click(secret()); await click(auth())
  await click(button('重新选择文件')); await pick('other-backup.zip')
  expect(secret().checked).toBe(false); expect(auth().checked).toBe(false)
  await click(node.querySelector<HTMLInputElement>('[aria-label="信任备份来源"]')!)
  await click(button('恢复所选配置'))
  expect(mocks.backup).toHaveBeenCalledWith(expect.any(File), false, ['watchlist'])
})

it('strategy code needs trust and every selected dependency conflict needs its own replacement confirmation', async () => {
  mocks.inspect.mockResolvedValue({ kind: 'resources', preview: { roots: ['strategy:s'], revision: 'r', counts: { strategy: 1, signal: 1, factor: 0 }, entries: [
    { key: 'strategy:s', kind: 'strategy', id: 's', label: '策略', action: 'create', dependencies: ['signal:x'], issues: [], local_dependencies: [], path: 's.py' },
    { key: 'signal:x', kind: 'signal', id: 'x', label: '信号', action: 'conflict', dependencies: [], issues: [], local_dependencies: [], path: 'x.json' },
  ] } })
  mocks.resources.mockResolvedValue({ imported: 2, reused: 0, counts: { strategy: 1, signal: 1 }, drafts: [] })
  await render(); await pick('strategy.zip')
  await click(node.querySelector<HTMLInputElement>('[aria-label="信任代码来源"]')!)
  expect(button('确认导入').disabled).toBe(true)
  await click(node.querySelector<HTMLInputElement>('[aria-label="确认替换信号"]')!)
  await click(button('确认导入'))
  expect(mocks.resources).toHaveBeenCalledWith(expect.any(Array), ['strategy:s'], ['signal:x'], 'r')
})

it('install failure leaves the file and review available for retry', async () => {
  mocks.install.mockRejectedValue(new Error('权限不足，请检查目录'))
  await render(); await pick(); await click(node.querySelector<HTMLInputElement>('[aria-label="信任代码来源"]')!)
  await click(button('确认安装'))
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('权限不足')
  expect(button('确认安装').disabled).toBe(false)
  expect(node.textContent).toContain('研究服务')
})

it('update review identifies both the active version and the staged version to replace', async () => {
  mocks.inspect.mockResolvedValue({ ...extension, preview: { ...extension.preview, installed_version: '0.8.0', pending_version: '0.9.0' } })
  await render(); await pick()
  expect(node.textContent).toContain('本机 0.8.0 → 目标 1.0.0')
  expect(node.textContent).toContain('已有待生效版本 0.9.0')
  expect(node.textContent).toContain('用 1.0.0 替换该暂存版本')
})

it('failed version and rollback target are explicit; management uses the displayed state revision', async () => {
  mocks.packages.mockResolvedValue([{ id: 'demo.backend', name: '研究服务', version: '2.0.0', previous_version: '1.0.0', runtime_version: '2.0.0',
    failed_version: '3.0.0', enabled: true, status: '启动失败 · 保留原版', error: '失败', revision: 'state-2' }])
  await render('/settings?tab=import&view=installed&selected=extension%3Ademo.backend')
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(node.textContent).toContain('版本 3.0.0 启动失败；当前仍运行 2.0.0')
  })
  await click(button('回退到 1.0.0'))
  expect(node.textContent).toContain('确认回退：2.0.0 → 1.0.0')
  await click(button('确认'))
  expect(mocks.manage).toHaveBeenCalledWith('demo.backend', 'rollback', 'state-2')
})

it('factor-page context extracts factors from a strategy-root package without selecting the strategy', async () => {
  mocks.inspect.mockResolvedValue({ kind: 'resources', preview: { roots: ['strategy:s'], revision: 'r', counts: { strategy: 1, factor: 1 }, entries: [
    { key: 'strategy:s', kind: 'strategy', id: 's', label: '策略', action: 'create', dependencies: ['factor:f'], issues: [], local_dependencies: [], path: 's.py' },
    { key: 'factor:f', kind: 'factor', id: 'f', label: '因子', action: 'create', dependencies: [], issues: [], local_dependencies: [], path: 'f.json' },
  ] } })
  mocks.resources.mockResolvedValue({ imported: 1, reused: 0, counts: { factor: 1 } })
  await render('/settings?tab=import&kind=factor'); await pick('strategy.zip')
  await click(button('确认导入'))
  expect(mocks.resources).toHaveBeenCalledWith(expect.any(Array), ['factor:f'], [], 'r')
})
