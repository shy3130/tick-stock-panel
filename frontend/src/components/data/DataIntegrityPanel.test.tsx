// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type IntegrityReport, type PipelineJob } from '@/lib/api'
import { DataIntegrityPanel } from './DataIntegrityPanel'

vi.mock('@/lib/api', () => ({ api: {
  dataIntegrity: vi.fn(), scanDataIntegrity: vi.fn(), repairDataIntegrity: vi.fn(),
} }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const onStart = vi.fn()
const report: IntegrityReport = {
  version: 1, id: 'a'.repeat(32), checked_at: '2026-10-30T12:00:00+08:00',
  start_date: '2026-09-30', end_date: '2026-10-29', lookback_days: 30,
  asset_types: ['stock'], status: 'issues', repairable_count: 1, uncertain_count: 0,
  no_data_assets: [], issues: [{ asset_type: 'stock', asset_label: '股票', date: '2026-10-29',
    kind: 'corrupt', kind_label: '文件损坏或为空', reason: '日K文件损坏或无法读取',
    repairable: true, symbols: [], symbol_count: 0 }],
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.scanDataIntegrity).mockResolvedValue({ job_id: 'scan-job' })
  vi.mocked(api.repairDataIntegrity).mockResolvedValue({ job_id: 'repair-job' })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(job?: PipelineJob) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <DataIntegrityPanel hasCap isRunning={job?.status === 'running'} job={job} defaultAssets={['stock']} onStart={onStart} />
  </QueryClientProvider>))
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
  expect(button).toBeTruthy()
  await act(async () => { button.click(); await new Promise(r => setTimeout(r, 10)) })
}

it('空报告可发起只读自检并接到已有任务进度', async () => {
  vi.mocked(api.dataIntegrity).mockResolvedValue({ report: null })
  await render()
  expect(host.textContent).toContain('第一次自检')
  await click('开始自检')
  expect(api.scanDataIntegrity).toHaveBeenCalledWith(30, ['stock'])
  expect(onStart).toHaveBeenCalledWith('scan-job')
})

it('显示具体问题, 确认后才修补', async () => {
  vi.mocked(api.dataIntegrity).mockResolvedValue({ report })
  await render()
  expect(host.textContent).toContain('2026-10-29')
  expect(host.textContent).toContain('文件损坏或为空')
  await click('修补已确认问题')
  expect(api.repairDataIntegrity).not.toHaveBeenCalled()
  expect(host.textContent).toContain('原文件先备份')
  await click('确认修补并复检')
  expect(api.repairDataIntegrity).toHaveBeenCalledWith(report.id)
  expect(onStart).toHaveBeenCalledWith('repair-job')
})

it('范围变化时不能拿旧报告修补', async () => {
  vi.mocked(api.dataIntegrity).mockResolvedValue({ report })
  await render()
  const select = host.querySelector('select')!
  await act(async () => {
    select.value = '7'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('修补已确认问题'))!
  expect(button.disabled).toBe(true)
  expect(host.textContent).toContain('请重新自检')
})

it('待核实日期不会显示已通过或提供自动修补', async () => {
  vi.mocked(api.dataIntegrity).mockResolvedValue({ report: {
    ...report, status: 'uncertain', repairable_count: 0, uncertain_count: 1,
    issues: [{ ...report.issues[0], kind: 'unconfirmed', kind_label: '缺失日期待核实', repairable: false }],
  } })
  await render()
  expect(host.textContent).toContain('尚不能确认是否开市')
  expect(host.textContent).not.toContain('未发现日K问题')
  expect(host.textContent).not.toContain('修补已确认问题')
})

it('自检任务失败时在弹窗中显示原因, 运行期间禁用重复操作', async () => {
  vi.mocked(api.dataIntegrity).mockResolvedValue({ report: null })
  await render()
  await click('开始自检')
  const job: PipelineJob = {
    id: 'scan-job', status: 'running', stage: 'integrity_check', progress: 10, stage_pct: 10,
    log: [], started_at: null, finished_at: null, duration_s: null, result: null, error: null,
  }
  await render(job)
  expect([...host.querySelectorAll('input, select, button')].every(el => (el as HTMLInputElement).disabled)).toBe(true)
  await render({ ...job, status: 'failed', error: '文件读取权限不足' })
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('文件读取权限不足')
})
