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
  manage: vi.fn(), loadErrors: vi.fn(), stageEngine: vi.fn(), candidates: vi.fn(), switchEngine: vi.fn(),
  uninstallPro: vi.fn(),
  licenses: vi.fn(), identity: vi.fn(), importLease: vi.fn(),
  chartImport: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: {
  importCenterInspect: mocks.inspect, installExtensionPackage: mocks.install,
  configBackupApply: mocks.backup, resourcesApply: mocks.resources, installSourcePackage: mocks.source,
  extensionPackages: mocks.packages, extPagesList: mocks.pages, dataSources: mocks.sources,
  extPagesSourceNamespaces: mocks.namespaces,
  capabilityMatrix: mocks.matrix,
  manageExtensionPackage: mocks.manage,
  stageEngineRelease: mocks.stageEngine, engineReleaseCandidates: mocks.candidates,
  switchEngineRelease: mocks.switchEngine,
  uninstallProDynamic: mocks.uninstallPro,
  engineLicenses: mocks.licenses, createEngineIdentity: mocks.identity, importEngineLicense: mocks.importLease,
  importChartIndicators: mocks.chartImport,
} }))
vi.mock('@/custom/source-packages/extension', () => ({ PackageActions: () => null }))
vi.mock('./DataSources', () => ({ PluginKeyConfig: ({ plugin }: { plugin: { name: string } }) => <div data-key-config={plugin.name}>API Key configuration</div> }))
vi.mock('@/extensions/registry', () => ({ getFrontendExtensionLoadErrors: mocks.loadErrors }))

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

it('stores a signed engine candidate and shows its pending status without requesting code execution', async () => {
  const preview = { manifest: { id: 'demo.price', version: '1.0.0', issuer: 'demo', kind: 'backtest-extension',
    license_kind: 'commercial', provides: [{ id: 'demo.dynamic' }] }, kid: 'vendor-release', digest: 'fixture-digest',
    errors: [], revision: 'review-revision', state: 'inspected' }
  mocks.inspect.mockResolvedValue({ kind: 'engine', preview })
  mocks.stageEngine.mockResolvedValue({ ...preview, state: 'staged' })
  mocks.candidates.mockResolvedValue([{ id: 'demo.price', version: '1.0.0', digest: 'fixture-digest', kid: 'vendor-release', state: 'staged' }])
  await render()
  await pick('engine.zip')
  expect(node.textContent).toContain('已验证的发行签名')
  expect(node.textContent).toContain('商业制品，运行时另行授权')
  expect(node.querySelector('input[type="checkbox"]')).toBeNull()
  await click(button('保存候选包'))
  expect(mocks.stageEngine).toHaveBeenCalledWith(expect.any(File), 'fixture-digest', 'review-revision')
  expect(mocks.install).not.toHaveBeenCalled()
  expect(node.textContent).toContain('引擎候选包已保存')
  await click(button('查看已安装扩展'))
  await vi.waitFor(() => expect(node.querySelector('[data-extension-detail]')?.textContent).toContain('fixture-digest'))
  expect(node.querySelector('[data-extension-detail]')?.textContent).toContain('候选版本')
})

it('disables candidate staging on a declared runtime mismatch', async () => {
  mocks.inspect.mockResolvedValue({ kind: 'engine', preview: { manifest: { id: 'demo.price', version: '1.0.0',
    issuer: 'demo', license_kind: 'commercial', provides: [] }, kid: 'root', errors: ['ABI 不兼容'] } })
  await render()
  await pick()
  expect(button('保存候选包').disabled).toBe(true)
  expect(node.textContent).toContain('ABI 不兼容')
})

it('previews chart definitions and requires confirmation of an ID conflict', async () => {
  const definition = { id: 'user_ma', name: '自定义均线', syntax: 'tdx', source: '均线:MA(C,5);' }
  const packageData = { format: 'tick-chart-indicators', version: 1, definitions: [definition] }
  mocks.inspect.mockResolvedValue({ kind: 'chart_indicators', preview: {
    package: packageData, revision: 'charts-1', entries: [{ id: 'user_ma', name: '自定义均线', action: 'conflict' }],
  } })
  const library = { library: { definitions: [definition] }, revision: 'charts-2' }
  mocks.chartImport.mockRejectedValueOnce(new Error('指标暂时无法保存')).mockResolvedValueOnce(library)
  await render()
  await pick('indicators.json')
  expect(button('确认导入').disabled).toBe(true)
  expect(mocks.chartImport).not.toHaveBeenCalled()
  await click(node.querySelector('[aria-label="确认替换自定义均线"]')!)
  await click(button('确认导入'))
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('指标暂时无法保存')
  await click(button('确认导入'))
  expect(mocks.chartImport).toHaveBeenLastCalledWith(packageData, ['user_ma'], ['user_ma'], 'charts-1')
  expect(qc.getQueryData(['chart-indicators'])).toEqual(library)
  expect(node.textContent).toContain('图表指标已导入')
})

it('shows the declared research capability id for an installed data package', async () => {
  mocks.candidates.mockResolvedValue([{ id: 'desk.feed', version: '1', digest: 'active-feed',
    kid: 'publisher', state: 'active', provider_ids: ['desk.raw-bars'] }])
  await render('/settings?tab=import&view=installed&selected=engine%3Aactive-feed')
  await vi.waitFor(() => expect(node.textContent).toContain('研究数据源 desk.raw-bars'))
  expect(button('彻底卸载')).toBeUndefined()
})

it('asks before uninstalling Pro and refreshes its installed and price-view status after confirmation', async () => {
  const id = 'tivelo.pro.dynamic-adjustment'
  const version = { id, version: '1.0', digest: 'active-pro', kid: 'publisher',
    state: 'active', generation: 2 }
  const previous = { ...version, version: '0.9', digest: 'older-pro', state: 'previous' }
  mocks.candidates.mockResolvedValue([version, previous])
  mocks.uninstallPro.mockRejectedValueOnce(new Error('研究任务正在使用 Tivelo Pro'))
    .mockImplementationOnce(async () => {
      mocks.candidates.mockResolvedValue([])
      return { id, removed: 2 }
    })
  await render('/settings?tab=import&view=installed&selected=engine%3Aactive-pro')
  await vi.waitFor(() => expect(button('彻底卸载')).toBeTruthy())
  await click(button('彻底卸载'))
  expect(node.querySelector('[role="dialog"]')?.textContent).toContain('所有版本')
  expect(mocks.uninstallPro).not.toHaveBeenCalled()
  await click(button('取消'))
  expect(mocks.uninstallPro).not.toHaveBeenCalled()
  expect(node.querySelector('[role="dialog"]')).toBeNull()
  await click(button('彻底卸载'))
  await click(button('确认卸载'))
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('研究任务正在使用 Tivelo Pro')
  expect(node.textContent).toContain('active-pro')
  await click(button('彻底卸载'))
  await click(button('确认卸载'))
  expect(mocks.uninstallPro).toHaveBeenLastCalledWith({ generation: 2, digests: ['active-pro', 'older-pro'] })
  await vi.waitFor(() => {
    expect(node.textContent).toContain('Tivelo Pro 已卸载')
    expect(node.textContent).not.toContain('active-pro')
  })
})

it('switches the displayed candidate with its generation and exposes rollback for the active version', async () => {
  const candidate = { id: 'demo.views', version: '2', digest: 'new', kid: 'publisher', state: 'staged', generation: 2 }
  mocks.candidates.mockResolvedValue([candidate])
  mocks.switchEngine.mockImplementation(async () => {
    mocks.candidates.mockResolvedValue([{ ...candidate, state: 'active', generation: 3, rollback_digest: 'old' }])
    return { id: 'demo.views', active_digest: 'new', generation: 3 }
  })
  await render('/settings?tab=import&view=installed&selected=engine%3Anew')
  await vi.waitFor(() => expect(button('验证并启用版本')).toBeTruthy())
  expect(mocks.switchEngine).not.toHaveBeenCalled()
  await click(button('验证并启用版本'))
  expect(mocks.switchEngine).toHaveBeenCalledWith('new', 'activate', 2)
  await vi.waitFor(() => expect(button('回退至上一个版本')).toBeTruthy())
  mocks.switchEngine.mockRejectedValue(new Error('自检期间活动版本发生变化'))
  await click(button('回退至上一个版本'))
  expect(mocks.switchEngine).toHaveBeenLastCalledWith('new', 'rollback', 3)
  expect(node.textContent).toContain('自检期间活动版本发生变化')
  expect(button('停用引擎扩展')).toBeTruthy()
})

it('registers the device and imports a lease for the selected active commercial digest', async () => {
  const identity = { public_key: 'installation-public-key', sha256: 'hash', storage: 'private_file' }
  const summary = { license_policy: 'subscription', lease_expires_at: '2027-01-01T00:00:00Z' }
  mocks.candidates.mockResolvedValue([{ id: 'demo.views', version: '1', digest: 'active', kid: 'vendor',
    state: 'active', generation: 1, license_kind: 'commercial' }])
  mocks.licenses.mockResolvedValue({ identity: null, leases: [] })
  mocks.identity.mockImplementation(async () => {
    mocks.licenses.mockResolvedValue({ identity, leases: [] })
    return identity
  })
  mocks.importLease.mockRejectedValueOnce(new Error('本机使用凭证校验失败'))
    .mockImplementationOnce(async () => {
      const lease = { extension_id: 'demo.views', revision: 1, summary }
      mocks.licenses.mockResolvedValue({ identity, leases: [lease] })
      return lease
    })
  await render('/settings?tab=import&view=installed&selected=engine%3Aactive')
  await vi.waitFor(() => expect(button('生成本机安装身份')).toBeTruthy())
  expect(mocks.identity).not.toHaveBeenCalled()
  await click(button('生成本机安装身份'))
  await vi.waitFor(() => expect(node.querySelector<HTMLInputElement>('input[readonly]')?.value).toBe(identity.public_key))
  const input = node.querySelector<HTMLInputElement>('[aria-label="商业授权"] input[type="file"]')!
  expect(input.hidden).toBe(false)
  const pickerLabel = input.labels?.[0]
  expect(pickerLabel?.textContent?.trim()).toBe('选择凭证文件')
  expect(input.getAttribute('accept')).toBe('.lic,.json,application/json')
  expect(document.getElementById(input.getAttribute('aria-describedby') ?? '')?.textContent).toContain('.lic')
  const openPicker = vi.fn()
  input.addEventListener('click', openPicker)
  await click(pickerLabel!)
  expect(openPicker).toHaveBeenCalledOnce()
  expect(mocks.importLease).not.toHaveBeenCalled()
  const file = new File(['signed'], 'lease.lic')
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('本机使用凭证校验失败')
  expect(input.value).toBe('')
  expect(input.disabled).toBe(false)
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  await vi.waitFor(() => expect(node.textContent).toContain('已登记订阅凭证'))
  expect(node.querySelector('[role="alert"]')).toBeNull()
  expect(mocks.importLease).toHaveBeenCalledTimes(2)
  expect(mocks.importLease).toHaveBeenCalledWith('active', file, 0)
})

it('imports a single dropped credential using the existing device authorization', async () => {
  const identity = { public_key: 'device', sha256: 'hash', storage: 'private_file' }
  mocks.candidates.mockResolvedValue([{ id: 'demo.views', version: '1', digest: 'active', kid: 'vendor',
    state: 'active', generation: 1, license_kind: 'commercial' }])
  mocks.licenses.mockResolvedValue({ identity, leases: [] })
  mocks.importLease.mockResolvedValue({})
  await render('/settings?tab=import&view=installed&selected=engine%3Aactive')
  const area = () => node.querySelector('[aria-label="拖入使用凭证"]')!
  await vi.waitFor(() => expect(area()).toBeTruthy())
  const file = new File(['isolated drop fixture'], 'device.lic')
  function drop(files: File[]) {
    const event = new Event('drop', { bubbles: true, cancelable: true })
    Object.defineProperty(event, 'dataTransfer', { value: { files } })
    return act(async () => area().dispatchEvent(event))
  }
  await drop([file, file])
  expect(mocks.importLease).not.toHaveBeenCalled()
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('请一次拖入一个')
  await drop([file])
  expect(mocks.importLease).toHaveBeenCalledExactlyOnceWith('active', file, 0)
  expect(node.querySelector('[role="alert"]')).toBeNull()
})

it('submits pasted credential contents only after confirmation and clears a successful draft', async () => {
  const identity = { public_key: 'device', sha256: 'hash', storage: 'private_file' }
  mocks.candidates.mockResolvedValue([{ id: 'demo.views', version: '1', digest: 'active', kid: 'vendor',
    state: 'active', generation: 1, license_kind: 'commercial' }])
  mocks.licenses.mockResolvedValue({ identity, leases: [] })
  mocks.importLease.mockRejectedValueOnce(new Error('本机使用凭证校验失败')).mockResolvedValueOnce({})
  await render('/settings?tab=import&view=installed&selected=engine%3Aactive')
  const summary = () => Array.from(node.querySelectorAll('summary')).find(item => item.textContent === '粘贴凭证 JSON')!
  await vi.waitFor(() => expect(summary()).toBeTruthy())
  await click(summary())
  const field = node.querySelector<HTMLTextAreaElement>('textarea[aria-label="凭证 JSON"]')!
  const confirm = button('核验并导入')
  expect(confirm.disabled).toBe(true)
  const contents = ' { "signed": "isolated", "signature": "test" }\n'
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(field, contents)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(mocks.importLease).not.toHaveBeenCalled()
  await click(confirm)
  expect(node.querySelector('[role="alert"]')?.textContent).toContain('本机使用凭证校验失败')
  expect(field.value).toBe(contents)
  await click(confirm)
  expect(mocks.importLease).toHaveBeenCalledTimes(2)
  const [digest, submitted, revision] = mocks.importLease.mock.calls[1] as [string, File, number]
  expect([digest, submitted.name, revision]).toEqual(['active', 'tivelo-license.json', 0])
  const sent = await new Promise(resolve => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.readAsText(submitted)
  })
  expect(sent).toBe(contents)
  expect(field.value).toBe('')
})

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
  mocks.candidates.mockResolvedValue([])
  mocks.pages.mockResolvedValue([])
  mocks.sources.mockResolvedValue({ plugins: [] })
  mocks.namespaces.mockResolvedValue([])
  mocks.matrix.mockResolvedValue({ capabilities: [] })
  mocks.manage.mockResolvedValue({ id: 'demo.backend', message: '变更已保存，重启后生效' })
  mocks.loadErrors.mockReturnValue([])
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

it('previews a data source Markdown description before installation', async () => {
  mocks.inspect.mockResolvedValue({ kind: 'source', preview: {
    manifest: { name: 'demo', display_name: '演示数据源', version: '1.0.0', datasets: ['daily'],
      description: '## 接入说明\n使用 **个人 Key**，价格为 `不复权`。' },
    digest: 'digest', revision: 'revision', files: ['plugin.yaml', 'provider.py'], errors: [], installed_version: null,
  } })
  await render(); await pick('source.zip')
  expect(node.querySelector('strong')?.textContent).toBe('个人 Key')
  expect(node.querySelector('code')?.textContent).toBe('不复权')
  expect(mocks.source).not.toHaveBeenCalled()
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

it('opens the first extension by default and shows its full detail', async () => {
  mocks.packages.mockResolvedValue([{ id: 'demo.backend', name: '研究服务', version: '1.0.0',
    enabled: true, status: '已启用', revision: 'r' }])
  await render('/settings?tab=import&view=installed')
  await vi.waitFor(() => {
    expect(node.querySelector('button[aria-pressed="true"]')?.textContent).toBe('已安装扩展')
    expect(button('停用扩展')).toBeTruthy()
  })
  expect(node.querySelector('nav[aria-label="已安装扩展列表"] button[aria-pressed="true"]')?.textContent).toContain('研究服务')
})

it.each([
  { enabled: false, pending_remove: false },
  { enabled: false, pending_remove: true },
  { enabled: undefined, pending_remove: true },
])('hides credential editing for an inactive source %j', async (state) => {
  const source = { name: 'amber', display_name: 'Amber', origin: 'external', version: '1.0.0',
    datasets: ['realtime'], ...state, api_key_env: 'AMBER_API_KEY',
    available: false, status: '待重启卸载', description: '' }
  mocks.sources.mockResolvedValue({ plugins: [source] })
  await render('/settings?tab=import&view=installed&selected=source%3Aamber')
  await vi.waitFor(() => expect(node.querySelector('[data-extension-detail] h3')?.textContent).toContain('Amber'))
  expect(node.querySelector('[data-key-config]')).toBeNull()
})

it('shows Markdown instructions and custom links even for a disabled source without a Key form', async () => {
  mocks.sources.mockResolvedValue({ plugins: [{
    name: 'demo', display_name: '演示数据源', origin: 'external', version: '1.0.0',
    datasets: ['daily'], enabled: false, available: false, status: '已停用',
    description: '## 接入说明\n使用 **个人账户** 和 `不复权` 数据。',
    usage_notes: ['阅读 [详细说明](https://example.com/docs)。'],
    homepage: 'https://example.com', homepage_label: '访问官方网站',
    key_manage_url: 'https://example.com/account', key_manage_label: '获取个人 Key',
  }] })
  await render('/settings?tab=import&view=installed&selected=source%3Ademo')
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(node.querySelector('[data-extension-detail] strong')?.textContent).toBe('个人账户')
  })
  const detail = node.querySelector('[data-extension-detail]')!
  expect(detail.querySelector('code')?.textContent).toBe('不复权')
  expect([...detail.querySelectorAll('a')].map(link => link.textContent)).toEqual(['详细说明', '访问官方网站', '获取个人 Key'])
  expect(node.querySelector('[data-key-config]')).toBeNull()
})

it('associates runtime loading errors with their page instead of showing it as enabled', async () => {
  mocks.pages.mockResolvedValue([{ id: 'abnormal', label: '异动监控', enabled: true }])
  mocks.loadErrors.mockReturnValue([{ source: 'runtime:abnormal', error: '扩展 ID 重复: official.abnormal' }])
  await render('/settings?tab=import&view=installed&selected=page%3Aabnormal')
  await vi.waitFor(() => {
    const entry = node.querySelector('nav[aria-label="已安装扩展列表"] button')
    expect(entry?.textContent).toContain('加载失败')
    expect(entry?.textContent).not.toContain('已启用')
  })
  expect(button('停用页面')?.disabled).toBe(false)
  expect(node.querySelector('[data-extension-detail]')?.textContent).toContain('扩展 ID 重复: official.abnormal')
})
