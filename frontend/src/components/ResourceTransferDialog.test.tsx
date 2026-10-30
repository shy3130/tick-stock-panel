// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ResourceTransferButton, ResourceTransferDialog } from './ResourceTransferDialog'
import type { ResourceKind, ResourceTransferEntry, ResourceTransferPreview } from '@/lib/api'

const mocks = vi.hoisted(() => ({ list: vi.fn(), preview: vi.fn(), inspect: vi.fn(), apply: vi.fn(), download: vi.fn(), close: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { resourcesList: mocks.list, resourcesExportPreview: mocks.preview, resourcesInspect: mocks.inspect, resourcesApply: mocks.apply, resourcesExport: mocks.download } }))

const entry = (key: string, label: string, deps: string[] = [], action: ResourceTransferEntry['action'] = 'create'): ResourceTransferEntry => ({
  key, kind: key.split(':')[0] as ResourceTransferEntry['kind'], id: key.split(':')[1], label,
  path: key, dependencies: deps, local_dependencies: [], issues: [], action,
})
const entries = [entry('strategy:custom_a', '策略甲', ['signal:shared', 'factor:uf_shared']), entry('strategy:custom_b', '策略乙'),
  entry('signal:shared', '突破信号', ['factor:uf_shared']), entry('factor:uf_shared', '动量因子')]
const preview = (overrides: Partial<ResourceTransferPreview> = {}): ResourceTransferPreview => ({ roots: ['strategy:custom_a', 'strategy:custom_b'], entries, revision: 'revision-1', counts: { strategy: 2, signal: 1, factor: 1 }, ...overrides })
const container = document.createElement('div')
document.body.appendChild(container)
let root: Root | undefined
let qc: QueryClient

async function render(initialMode: 'import' | 'export' = 'export', kind: ResourceKind = 'strategy') {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  root = createRoot(container)
  await act(async () => { root!.render(<QueryClientProvider client={qc}><ResourceTransferDialog initialMode={initialMode} kind={kind} onClose={mocks.close} /></QueryClientProvider>) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}

const button = (text: string) => [...document.querySelectorAll('button')].find(el => el.textContent?.trim() === text) as HTMLButtonElement
const checkbox = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
async function click(el: HTMLElement) { expect(el).toBeTruthy(); await act(async () => el.click()) }
async function pickFiles(files = [new File(['data'], 'resources.zip', { type: 'application/zip' })]) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: files })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(mock => mock.mockReset())
  mocks.list.mockResolvedValue({ items: entries.map(({ kind, id, label }) => ({ kind, id, label })) })
  mocks.preview.mockResolvedValue(preview())
  mocks.inspect.mockResolvedValue(preview())
  mocks.apply.mockResolvedValue({ imported: 4, reused: 0, counts: { strategy: 2, signal: 1, factor: 1 }, resources: entries.map(e => e.key), changed: entries.map(e => e.key) })
  mocks.download.mockResolvedValue(new Blob(['zip']))
})
afterEach(() => {
  act(() => root?.unmount())
  root = undefined
  qc?.clear()
  container.innerHTML = ''
})

it('strategy selection persists across searches and the ZIP preview contains signal and factor dependencies', async () => {
  await render()
  expect(button('预览导出').disabled).toBe(true)
  await click(button('全选筛选结果 (2)'))
  const search = document.querySelector<HTMLInputElement>('[aria-label="搜索资源"]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { setter.call(search, '甲'); search.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(checkbox('选择策略：策略乙')).toBeNull()
  expect(checkbox('选择信号：突破信号')).toBeNull()
  expect(document.querySelector('[aria-label="资源类型筛选"]')).toBeNull()
  await click(button('预览导出'))
  expect(mocks.preview).toHaveBeenCalledWith([{ kind: 'strategy', id: 'custom_a' }, { kind: 'strategy', id: 'custom_b' }])
  const plan = document.querySelector('[aria-label="资源依赖预览"]')!
  expect(plan.textContent).toContain('自动携带 2 项依赖')
  expect(plan.textContent).toContain('动量因子')
  expect(button('下载 ZIP 包')).toBeTruthy()
})

it('picking a package only inspects; importing one strategy automatically carries its dependencies', async () => {
  await render('import')
  await pickFiles()
  expect(mocks.apply).not.toHaveBeenCalled()
  expect(document.body.textContent).toContain('尚未写入本机')
  await click(checkbox('选择策略：策略乙'))
  expect(checkbox('选择因子：动量因子')).toBeNull()
  expect(document.querySelector('[aria-label="资源依赖预览"]')!.textContent).toContain('动量因子')
  await click(button('导入 3 项资源'))
  expect(mocks.apply).toHaveBeenCalledWith(expect.any(Array), ['strategy:custom_a'], [], 'revision-1')
  expect(document.body.textContent).toContain('资源已导入')
})

it('accepts a strategy, separate definitions and an ordinary ZIP together in the strategy dialog', async () => {
  await render('import')
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  expect(input.accept.split(',')).toEqual(expect.arrayContaining(['.py', '.json', '.zip']))
  const files = [new File(['code'], '策略.py'), new File(['factor'], '因子.json'), new File(['zip'], '依赖.zip')]
  await pickFiles(files)
  expect(mocks.inspect).toHaveBeenCalledWith(files)
  expect(mocks.apply).not.toHaveBeenCalled()
  await click(checkbox('选择策略：策略乙'))
  await click(button('导入 3 项资源'))
  expect(mocks.apply).toHaveBeenCalledWith(files, ['strategy:custom_a'], [], 'revision-1')
})

it('different content blocks import until each selected conflict is explicitly confirmed', async () => {
  const changed = entries.map(e => e.key === 'factor:uf_shared' ? { ...e, action: 'conflict' as const,
    comparison: { local: 'open', incoming: 'close', truncated: false }, referenced_by: ['本机策略'] } : e)
  mocks.inspect.mockResolvedValue(preview({ entries: changed }))
  await render('import')
  await pickFiles()
  expect(button('导入 4 项资源').disabled).toBe(true)
  expect(document.body.textContent).toContain('本机引用方：本机策略')
  expect(document.body.textContent).toContain('对比本机与包内内容')
  await click(checkbox('确认替换动量因子'))
  expect(button('导入 4 项资源').disabled).toBe(false)
  await click(button('导入 4 项资源'))
  expect(mocks.apply).toHaveBeenCalledWith(expect.any(Array), ['strategy:custom_a', 'strategy:custom_b'], ['factor:uf_shared'], 'revision-1')
})

it('deselecting an invalid independent resource allows the healthy resource to import', async () => {
  mocks.inspect.mockResolvedValue(preview({ entries: entries.map(e => e.key === 'strategy:custom_b' ? { ...e, issues: ['缺少因子'] } : e) }))
  await render('import')
  await pickFiles()
  expect(button('导入 4 项资源').disabled).toBe(true)
  await click(checkbox('选择策略：策略乙'))
  expect(button('导入 3 项资源').disabled).toBe(false)
  expect(document.body.textContent).not.toContain('所选资源尚不可用')
})

it('mode changes clear file previews, conflicts and selections', async () => {
  await render('import')
  await pickFiles()
  await click(document.querySelector<HTMLElement>('[role="tab"][aria-selected="false"]')!)
  expect(button('预览导出').disabled).toBe(true)
  await click(document.querySelector<HTMLElement>('[role="tab"][aria-selected="false"]')!)
  expect(document.body.textContent).toContain('选择文件，或将文件拖到这里')
  expect(document.body.textContent).not.toContain('resources.zip')
})

it('clearing selection also clears auto-selected dependencies', async () => {
  await render('import')
  await pickFiles()
  await click(button('清空选择'))
  expect(document.querySelector('[aria-label="资源依赖预览"]')!.textContent).not.toContain('动量因子')
  expect(button('导入所选资源').disabled).toBe(true)
})

it('a stale preview shows a recoverable error and re-inspection replaces its revision', async () => {
  mocks.apply.mockRejectedValueOnce(new Error('本机资源已变化，请重新预览后再导入'))
  await render('import')
  await pickFiles()
  await click(button('导入 4 项资源'))
  expect(document.body.textContent).toContain('本机资源已变化')
  mocks.inspect.mockResolvedValueOnce(preview({ revision: 'revision-2' }))
  await click(button('重新预览'))
  await click(button('导入 4 项资源'))
  expect(mocks.apply.mock.calls[1][3]).toBe('revision-2')
})

it('selection changes invalidate an export preview before downloading', async () => {
  await render()
  await click(button('全选筛选结果 (2)'))
  await click(button('预览导出'))
  await click(checkbox('选择策略：策略乙'))
  expect(button('下载 ZIP 包')).toBeUndefined()
  expect(button('预览导出')).toBeTruthy()
  expect(mocks.download).not.toHaveBeenCalled()
})

it('an export that changed after preview returns to preview instead of repeating a stale download', async () => {
  mocks.download.mockRejectedValueOnce(new Error('资源已变化，请重新预览后再下载'))
  await render()
  await click(button('全选筛选结果 (2)'))
  await click(button('预览导出'))
  await click(button('下载 ZIP 包'))
  expect(button('预览导出')).toBeTruthy()
  expect(button('下载 ZIP 包')).toBeUndefined()
  expect(document.body.textContent).toContain('重新预览后再下载')
})

it('imported AI drafts remain visibly identified and explain where to publish them', async () => {
  mocks.inspect.mockResolvedValue(preview({ entries: entries.map(e => e.key === 'strategy:custom_a' ? { ...e, draft: true } : e) }))
  mocks.apply.mockResolvedValue({ imported: 4, reused: 0, counts: { strategy: 2, signal: 1, factor: 1 }, resources: [], changed: [], drafts: ['ai_draft'] })
  await render('import')
  await pickFiles()
  expect(document.body.textContent).toContain('草稿')
  await click(button('导入 4 项资源'))
  expect(document.body.textContent).toContain('1 条策略保留为草稿')
  expect(document.body.textContent).toContain('发布后使用')
})

it.each([['factor', '动量因子'], ['signal', '突破信号']] as const)('the %s dialog only offers that page’s resource type for export', async (kind, label) => {
  await render('export', kind)
  expect(document.querySelector('[role="dialog"]')?.getAttribute('aria-label')).toBe(`${kind === 'factor' ? '因子' : '信号'}导入与导出`)
  expect(document.querySelectorAll('input[type="checkbox"]')).toHaveLength(1)
  expect(document.body.textContent).toContain(label)
  expect(document.body.textContent).not.toContain('策略甲')
})

it('the factor dialog extracts factors from a strategy ZIP without importing the strategy', async () => {
  await render('import', 'factor')
  await pickFiles()
  expect(checkbox('选择因子：动量因子').checked).toBe(true)
  expect(checkbox('选择策略：策略甲')).toBeNull()
  await click(button('导入 1 项资源'))
  expect(mocks.apply).toHaveBeenCalledWith(expect.any(Array), ['factor:uf_shared'], [], 'revision-1')
})

it('a package without the current page’s resource gives a clear empty state', async () => {
  mocks.inspect.mockResolvedValue(preview({ entries: [entry('factor:uf_shared', '动量因子')], roots: ['factor:uf_shared'] }))
  await render('import')
  await pickFiles()
  expect(document.body.textContent).toContain('文件中没有可导入的策略')
  expect(button('导入所选资源').disabled).toBe(true)
})

it('a failed export catalog does not hide the import file’s empty state after switching tabs', async () => {
  mocks.list.mockRejectedValueOnce(new Error('目录加载失败'))
  mocks.inspect.mockResolvedValue(preview({ entries: [entry('factor:uf_shared', '动量因子')], roots: ['factor:uf_shared'] }))
  await render()
  expect(document.body.textContent).toContain('资源列表加载失败')
  await click(document.querySelector<HTMLElement>('[role="tab"][aria-selected="false"]')!)
  await pickFiles()
  expect(document.body.textContent).toContain('文件中没有可导入的策略')
})

it('close and Escape cannot interrupt a pending file operation', async () => {
  let resolveInspect!: (value: ResourceTransferPreview) => void
  mocks.inspect.mockReturnValueOnce(new Promise(resolve => { resolveInspect = resolve }))
  await render('import')
  await pickFiles()
  await click(document.querySelector<HTMLButtonElement>('[aria-label="关闭"]')!)
  act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(mocks.close).not.toHaveBeenCalled()
  await act(async () => resolveInspect(preview()))
  await click(document.querySelector<HTMLButtonElement>('[aria-label="关闭"]')!)
  expect(mocks.close).toHaveBeenCalledTimes(1)
})

it('the single page icon opens the correct dialog and reopening discards the prior selection', async () => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(container)
  await act(async () => root!.render(<QueryClientProvider client={qc}><ResourceTransferButton kind="factor" /></QueryClientProvider>))
  const opener = container.querySelector<HTMLButtonElement>('[aria-label="因子导入与导出"]')!
  expect(container.querySelectorAll('button')).toHaveLength(1)
  await click(opener)
  expect(document.querySelector('[role="dialog"]')?.getAttribute('aria-label')).toBe('因子导入与导出')
  await click(document.querySelector<HTMLElement>('[role="tab"][aria-selected="false"]')!)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  await click(checkbox('选择因子：动量因子'))
  await click(button('取消'))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  await click(opener)
  expect(document.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('导入因子')
  expect(document.querySelector('[aria-label="选择资源文件"]')).toBeTruthy()
})
