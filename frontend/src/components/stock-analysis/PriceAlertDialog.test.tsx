// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type MonitorRule } from '@/lib/api'
import { buildMonitorPriceLines } from '@/lib/price-alerts'
import { PriceAlertDialog } from './PriceAlertDialog'

vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: { webhook_default_channels: [] } }) }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
const rule = { id: 'neighbor', name: '相邻价位', enabled: true, type: 'price', asset_type: 'etf', scope: 'symbols',
  symbols: ['159558.SZ'], conditions: [{ field: 'close', op: '>=', value: 0.828 }], cooldown_seconds: 3600 } as MonitorRule

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'monitorRulesList').mockResolvedValue({ rules: [rule] })
  vi.spyOn(api, 'stockAnalysisLevels').mockResolvedValue({ close: 0.800, levels: {} } as Awaited<ReturnType<typeof api.stockAnalysisLevels>>)
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function render(assetType: 'stock' | 'etf' = 'etf') {
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter>
    <PriceAlertDialog symbol={assetType === 'etf' ? '159558.SZ' : '600000.SH'} name="测试" assetType={assetType}
      initialTarget={0.827} initialCurrentPrice={0.800} onClose={() => {}} />
  </MemoryRouter></QueryClientProvider>))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}

it('ETF 千分位目标不被截断, 相邻报价不误判重复, 保存仍为 ETF 规则', async () => {
  const save = vi.spyOn(api, 'monitorRuleSave').mockResolvedValue({ ok: true, rule })
  await render()
  const input = host.querySelector<HTMLInputElement>('input[type="number"]')!
  expect(input.value).toBe('0.827')
  expect(Number(input.step)).toBe(0.001)
  expect(host.textContent).toContain('0.800')
  expect(host.textContent).not.toContain('相同方向和价格的提醒已存在')
  expect(api.stockAnalysisLevels).not.toHaveBeenCalled()
  const create = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('创建提醒'))!
  expect(create.disabled).toBe(false)
  await act(async () => create.click())
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ asset_type: 'etf',
    conditions: [{ field: 'close', op: '>=', value: 0.827 }], message: expect.stringContaining('0.827') }))
})

it('已有 ETF 价格提醒显示三位, 股票仍为分位输入', async () => {
  await render()
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.startsWith('已有提醒'))!.click())
  expect(host.textContent).toContain('0.828')
  await render('stock')
  const input = host.querySelector<HTMLInputElement>('input[type="number"]')
  // 新建页与已有页状态独立于资产精度; 返回新建页确认股票输入步长。
  if (!input) await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '新建提醒')!.click())
  expect(Number(host.querySelector<HTMLInputElement>('input[type="number"]')!.step)).toBe(0.01)
})

it('图中提醒标签使用规则资产类型并保留原始目标价格', () => {
  expect(buildMonitorPriceLines([rule], '159558.SZ')[0]).toMatchObject({ value: 0.828, label: '上穿 0.828' })
  expect(buildMonitorPriceLines([{ ...rule, asset_type: 'stock' }], '159558.SZ')[0]).toMatchObject({ value: 0.828, label: '上穿 0.83' })
})
