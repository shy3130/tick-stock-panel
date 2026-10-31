// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type MinuteGapRow, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MinuteSyncConfig } from './MinuteSyncConfig'

vi.mock('@/lib/api', () => ({ api: {
  preferences: vi.fn(), updateMinuteSync: vi.fn(), syncMinute: vi.fn(),
  clearMinute: vi.fn(), minuteGaps: vi.fn(), syncMinuteSingle: vi.fn(), repairMinuteGaps: vi.fn(),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const onJobStart = vi.fn()
const rows: MinuteGapRow[] = Array.from({ length: 24 }, (_, i) => ({
  symbol: `${String(i + 1).padStart(6, '0')}.SZ`, date: '2026-10-20',
  actual_bars: 171, expected_bars: 240, missing_bars: 69,
  first_time: '09:30', last_time: '13:49',
}))
const report = {
  status: 'ok', scanned_dates: 10, scanned_symbols: 5580, complete_dates: [],
  inactive_symbol_days: 0,
  gaps: rows, summary_by_date: [{ date: '2026-10-20', gap_symbols: 24, missing_bars: 1656, expected_bars: 240 }],
  truncated: false,
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const prefs = { minute_sync_enabled: false, minute_sync_days: 5, minute_sync_segment_days: 20 } as Preferences
  vi.mocked(api.preferences).mockResolvedValue(prefs)
  vi.mocked(api.updateMinuteSync).mockResolvedValue(prefs)
  vi.mocked(api.minuteGaps).mockResolvedValue(report)
  vi.mocked(api.syncMinute).mockResolvedValue({ job_id: 'minute-job', status: 'started' })
  vi.mocked(api.repairMinuteGaps).mockResolvedValue({ job_id: 'repair-job', status: 'started' })
})

it('零成交残留单独说明, 不提供无效补拉按钮', async () => {
  vi.mocked(api.minuteGaps).mockResolvedValue({ ...report, gaps: [], summary_by_date: [], inactive_symbol_days: 2 })
  await render()
  await click('数据体检')
  expect(host.textContent).toContain('未发现分钟数据缺口')
  expect(host.textContent).toContain('另有 2 组整日零成交记录')
  expect(host.textContent).not.toContain('一键补齐')
  expect(api.repairMinuteGaps).not.toHaveBeenCalled()
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(hasCap = true) {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}>
    <MinuteSyncConfig hasCap={hasCap} onJobStart={onJobStart} />
  </QueryClientProvider></MemoryRouter>))
  await settle()
}
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
  expect(button).toBeTruthy()
  await act(async () => button.click())
  await settle()
}
async function filter(value: string) {
  const input = host.querySelector<HTMLInputElement>('input[placeholder="按代码 / 日期过滤"]')!
  expect(input).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('同步设置和体检分开展示, 切换页签不丢失筛选条件', async () => {
  await render()
  const panels = host.querySelectorAll<HTMLElement>('[role="tabpanel"]')
  expect(panels).toHaveLength(2)
  expect(panels[0].hidden).toBe(false)
  expect(panels[1].hidden).toBe(true)
  await click('数据体检')
  expect(panels[0].hidden).toBe(true)
  expect(panels[1].hidden).toBe(false)
  expect(panels[1].textContent).toContain('000001.SZ')
  await filter('000001')
  await click('同步与获取')
  await click('数据体检')
  expect(host.querySelector<HTMLInputElement>('input')?.value).toBe('000001')
})

it('页签支持方向键切换并把键盘焦点移到选中项', async () => {
  await render()
  const tabs = host.querySelectorAll<HTMLButtonElement>('[role="tab"]')
  await act(async () => {
    tabs[0].focus()
    tabs[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  })
  expect(tabs[1].getAttribute('aria-selected')).toBe('true')
  expect(document.activeElement).toBe(tabs[1])
  await act(async () => tabs[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })))
  expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  expect(document.activeElement).toBe(tabs[0])
})

it('无匹配记录时保留搜索框和真实缺口状态, 可清除筛选', async () => {
  await render()
  await click('数据体检')
  await filter('不存在')
  expect(host.querySelector('input')).not.toBeNull()
  expect(host.textContent).toContain('没有匹配的缺口记录')
  expect(host.textContent).toContain('24 条缺口')
  expect(host.textContent).not.toContain('未发现分钟数据缺口')
  await filter('')
  expect(host.textContent).toContain('000001.SZ')
})

it('分页限制单页记录, 刷新后记录减少仍能显示有效页', async () => {
  await render()
  await click('数据体检')
  expect(host.textContent).toContain('000010.SZ')
  expect(host.textContent).not.toContain('000011.SZ')
  await click('下一页')
  await click('下一页')
  expect(host.textContent).toContain('000024.SZ')
  await act(async () => { client.setQueryData(QK.minuteGaps, { ...report, gaps: rows.slice(0, 1) }) })
  await settle()
  expect(host.textContent).toContain('000001.SZ')
})

it('手动获取仍使用分段天数并通知任务, 清空必须经过二次确认', async () => {
  await render()
  await click('单次获取 20 天')
  expect(api.syncMinute).toHaveBeenCalledWith(20, true)
  expect(onJobStart).toHaveBeenCalledWith('minute-job')
  await click('清空分钟K数据')
  expect(api.clearMinute).not.toHaveBeenCalled()
  expect(host.textContent).toContain('确认清空分钟K数据？')
  await click('取消')
  expect(api.clearMinute).not.toHaveBeenCalled()
})

it('无分钟能力时禁用拉取和补齐, 仍能查看本地体检结果', async () => {
  await render(false)
  expect([...host.querySelectorAll('button')].find(b => b.textContent?.includes('单次获取'))?.disabled).toBe(true)
  await click('数据体检')
  const repairs = [...host.querySelectorAll('button')].filter(b => b.textContent?.includes('尝试补齐'))
  expect(repairs.length).toBeGreaterThan(0)
  expect(repairs.every(b => b.disabled)).toBe(true)
  expect([...host.querySelectorAll('button')].find(b => b.textContent?.includes('一键补齐'))?.disabled).toBe(true)
})

it('一键补齐覆盖当前筛选的全部分页, 同一标的只提交一次', async () => {
  const repeated = rows.map((r, i) => ({ ...r, symbol: rows[i % 12].symbol, date: i < 12 ? '2026-10-20' : '2026-10-21' }))
  vi.mocked(api.minuteGaps).mockResolvedValue({ ...report, gaps: repeated })
  await render()
  await click('数据体检')
  await click('一键补齐')
  expect(api.repairMinuteGaps).toHaveBeenCalledTimes(1)
  expect(api.repairMinuteGaps).toHaveBeenCalledWith(rows.slice(0, 12).map(r => r.symbol))
  expect(api.syncMinuteSingle).not.toHaveBeenCalled()
  expect(onJobStart).toHaveBeenCalledWith('repair-job')
})

it('一键补齐遵守代码筛选, 无匹配项时不能发起任务', async () => {
  await render()
  await click('数据体检')
  await filter('不存在')
  expect([...host.querySelectorAll('button')].find(b => b.textContent?.includes('一键补齐'))?.disabled).toBe(true)
  await filter('000001')
  await click('一键补齐')
  expect(api.repairMinuteGaps).toHaveBeenCalledWith(['000001.SZ'])
})

it('批量启动期间禁用重复操作, 失败后显示原因并恢复按钮', async () => {
  let reject!: (error: Error) => void
  vi.mocked(api.repairMinuteGaps).mockImplementation(() => new Promise((_, fail) => { reject = fail }))
  await render()
  await click('数据体检')
  await click('一键补齐')
  const repairs = [...host.querySelectorAll('button')].filter(b => b.textContent?.includes('尝试补齐'))
  expect(repairs.every(b => b.disabled)).toBe(true)
  await act(async () => reject(new Error('已有数据任务正在运行')))
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('已有数据任务正在运行')
  expect([...host.querySelectorAll('button')].find(b => b.textContent?.includes('一键补齐'))?.disabled).toBe(false)
  expect(onJobStart).not.toHaveBeenCalled()
})

it('体检加载时显示扫描进度', async () => {
  vi.mocked(api.minuteGaps).mockImplementationOnce(() => new Promise(() => {}))
  await render()
  await click('数据体检')
  expect(host.textContent).toContain('正在扫描分钟分区')
})

it('体检覆盖失败和空结果状态', async () => {
  vi.mocked(api.minuteGaps).mockResolvedValueOnce({ ...report, gaps: [], summary_by_date: [] })
  await render()
  await click('数据体检')
  expect(host.textContent).toContain('未发现分钟数据缺口')
  vi.mocked(api.minuteGaps).mockRejectedValueOnce(new Error('无法读取分钟分区'))
  await click('重新体检')
  expect(host.textContent).toContain('体检失败')
  expect(host.textContent).toContain('无法读取分钟分区')
})
