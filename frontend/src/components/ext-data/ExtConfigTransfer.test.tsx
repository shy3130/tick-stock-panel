// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type ExtDataConfigPreview } from '@/lib/api'
import { ExtConfigImportDialog } from './ExtConfigTransfer'

vi.mock('@/lib/api', () => ({ api: {
  extDataConfigInspect: vi.fn(), extDataConfigImport: vi.fn(), extDataConfigExport: vi.fn(),
} }))

const preview: ExtDataConfigPreview = {
  id: 'auction', label: '集合竞价', mode: 'timeseries', field_count: 2,
  fields: [{ name: 'symbol', dtype: 'string', label: '代码' }, { name: 'price', dtype: 'float', label: '价格' }],
  conflict: false, requires_key: true, has_pull: true, warnings: [],
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
let close: ReturnType<typeof vi.fn>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => setTimeout(fn, 0))
  vi.stubGlobal('cancelAnimationFrame', clearTimeout)
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  close = vi.fn()
  vi.mocked(api.extDataConfigInspect).mockResolvedValue(preview)
  vi.mocked(api.extDataConfigImport).mockResolvedValue({ ...preview, created_at: '', updated_at: '' })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); host.remove(); vi.unstubAllGlobals()
})

async function mount() {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtConfigImportDialog onClose={close} />
  </QueryClientProvider>))
}

async function selectFile(file = new File(['zip'], 'auction.zip', { type: 'application/zip' })) {
  const input = host.querySelector('input[type=file]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  return file
}

async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === text)!
  expect(button).toBeTruthy()
  await act(async () => button.click())
}

it('previews a single configuration, imports it, and explains recipient key setup', async () => {
  await mount()
  const file = await selectFile()
  expect(host.textContent).toContain('集合竞价')
  expect(host.textContent).toContain('API Key')
  await click('导入配置')
  expect(api.extDataConfigImport).toHaveBeenCalledWith(file, 'auction')
  expect(host.textContent).toContain('导入成功')
  expect(host.textContent).toContain('自己的 API Key')
  expect(host.textContent).toContain('定时拉取')
})

it('suggests a new ID on conflict and never offers silent overwrite', async () => {
  vi.mocked(api.extDataConfigInspect).mockResolvedValue({ ...preview, conflict: true })
  await mount(); await selectFile()
  const input = host.querySelector<HTMLInputElement>('input[name=config_id]')!
  expect(input.value).toBe('auction_copy')
  expect(host.textContent).toContain('已有同名配置')
  await click('导入配置')
  expect(api.extDataConfigImport).toHaveBeenCalledWith(expect.any(File), 'auction_copy')
})

it('shows parsing and import failures without reporting success', async () => {
  vi.mocked(api.extDataConfigInspect).mockRejectedValueOnce(new Error('不是配置包'))
  await mount(); await selectFile()
  expect(host.querySelector('[role=alert]')?.textContent).toContain('不是配置包')
  await selectFile()
  vi.mocked(api.extDataConfigImport).mockRejectedValueOnce(new Error('标识已被其他请求创建'))
  await click('导入配置')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('标识已被其他请求创建')
  expect(host.textContent).not.toContain('导入成功')
})

it('protects an active import from Escape and duplicate submissions', async () => {
  let finish!: (value: Awaited<ReturnType<typeof api.extDataConfigImport>>) => void
  vi.mocked(api.extDataConfigImport).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  await mount(); await selectFile(); await click('导入配置')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLButtonElement>('button[aria-label=关闭]')?.disabled).toBe(true)
  expect(api.extDataConfigImport).toHaveBeenCalledTimes(1)
  await act(async () => finish({ ...preview, created_at: '', updated_at: '' }))
})
