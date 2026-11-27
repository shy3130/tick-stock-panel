// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ExtDataConfig, ExtDataRowsResult } from '@/lib/api'
import { AUCTION_SCAN_CONFIG_ID, AuctionScanCard } from './AuctionScanCard'

const mocks = vi.hoisted(() => ({
  extDataList: vi.fn(),
  extDataRows: vi.fn(),
  extDataPullConfig: vi.fn(),
  extDataPullRun: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  api: {
    extDataList: mocks.extDataList,
    extDataRows: mocks.extDataRows,
    extDataPullConfig: mocks.extDataPullConfig,
    extDataPullRun: mocks.extDataPullRun,
  },
}))

function makeConfig(overrides: Partial<ExtDataConfig> = {}): ExtDataConfig {
  return {
    id: AUCTION_SCAN_CONFIG_ID,
    label: '9:25 全市场竞价扫描',
    mode: 'timeseries',
    fields: [],
    created_at: '2026-04-14T00:00:00Z',
    updated_at: '2026-04-14T00:00:00Z',
    running: false,
    pull: {
      url: '',
      collector: 'auction_scan.market_925',
      method: 'GET',
      response_path: '',
      schedule_minutes: 1440,
      enabled: false,
      schedule_time: '09:25',
      schedule_period: 'daily',
      schedule_trading_only: true,
      time_window_start: '09:25',
      time_window_end: '09:30',
      timeout_seconds: 60,
    },
    ...overrides,
  }
}

function makeRows(overrides: Partial<ExtDataRowsResult> = {}): ExtDataRowsResult {
  return {
    id: AUCTION_SCAN_CONFIG_ID,
    label: '9:25 全市场竞价扫描',
    mode: 'timeseries',
    date: '2026-04-15',
    dates: ['2026-04-15', '2026-04-14'],
    total: 1,
    limit: 500,
    fields: [],
    rows: [
      {
        symbol: '000001.SZ',
        trade_date: '2026-04-15',
        name: '平安银行',
        open_gap_pct: 0.055,
        auction_vol_ratio: 12.4,
        auction_price: 10.55,
        prev_close: 10.0,
        auction_volume: 1240,
        collected_at: '2026-04-15T09:25:03+08:00',
      },
    ],
    ...overrides,
  }
}

const container = document.createElement('div')
document.body.appendChild(container)
let root: Root
let qc: QueryClient

async function flush() {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 15))
    })
  }
}

async function waitForText(expected: string) {
  for (let i = 0; i < 30; i += 1) {
    if (container.textContent?.includes(expected)) return
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 15))
    })
  }
}

async function renderCard(onOpenStock = vi.fn()) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <AuctionScanCard onOpenStock={onOpenStock} />
      </QueryClientProvider>,
    )
  })
  return onOpenStock
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(m => m.mockReset())
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  qc.clear()
  container.innerHTML = ''
})

it('未启用态展示待启用引导，点击开启发送 collector 启用配置', async () => {
  mocks.extDataList.mockResolvedValue({ items: [makeConfig()] })
  mocks.extDataPullConfig.mockResolvedValue({ status: 'ok', pull: {} })

  await renderCard()
  await waitForText('待采集任务启用')

  expect(container.textContent).toContain('待采集任务启用')
  expect(container.textContent).toContain('开启 9:25 自动扫描')
  expect(mocks.extDataRows).not.toHaveBeenCalled()

  const enableBtn = [...container.querySelectorAll('button')].find(b =>
    b.textContent?.includes('开启 9:25 自动扫描'),
  )
  expect(enableBtn).toBeTruthy()
  await act(async () => {
    enableBtn!.click()
  })
  await flush()

  expect(mocks.extDataPullConfig).toHaveBeenCalledWith(
    AUCTION_SCAN_CONFIG_ID,
    expect.objectContaining({
      collector: 'auction_scan.market_925',
      enabled: true,
      schedule_time: '09:25',
      time_window_end: '09:30',
      schedule_trading_only: true,
    }),
  )
})

it('采集中状态显示正在扫描提示', async () => {
  mocks.extDataList.mockResolvedValue({
    items: [makeConfig({
      running: true,
      pull: { ...makeConfig().pull!, enabled: true },
    })],
  })
  mocks.extDataRows.mockResolvedValue(makeRows({ date: null, dates: [], rows: [], total: 0 }))

  await renderCard()
  await waitForText('采集中')

  expect(container.textContent).toContain('采集中')
  expect(container.textContent).toContain('正在扫描全市场竞价快照')
})

it('今日无命中时明确区分空命中态并展示累计天数', async () => {
  mocks.extDataList.mockResolvedValue({
    items: [makeConfig({
      date_range: ['2026-04-14', '2026-04-15'],
      pull: { ...makeConfig().pull!, enabled: true, last_status: 'success', last_rows: 0 },
    })],
  })
  mocks.extDataRows.mockResolvedValue(makeRows({
    date: '2026-04-15',
    dates: ['2026-04-15', '2026-04-14'],
    total: 0,
    rows: [],
  }))

  await renderCard()
  await waitForText('当日无符合条件标的')

  expect(container.textContent).toContain('已累计 2 个交易日')
  expect(container.textContent).toContain('2026-04-15 竞价扫描已完成，当日无符合条件标的')
})

it('今日有命中展示列表、支持点开个股详情与切换历史交易日', async () => {
  mocks.extDataList.mockResolvedValue({
    items: [makeConfig({
      date_range: ['2026-04-14', '2026-04-15'],
      pull: { ...makeConfig().pull!, enabled: true, last_status: 'success', last_rows: 2 },
    })],
  })
  mocks.extDataRows.mockImplementation((_id: string, opts?: { date?: string }) => {
    if (opts?.date === '2026-04-14') {
      return Promise.resolve(makeRows({
        date: '2026-04-14',
        rows: [
          {
            symbol: '600519.SH',
            trade_date: '2026-04-14',
            name: '贵州茅台',
            open_gap_pct: 0.062,
            auction_vol_ratio: 15.0,
            auction_price: 1600,
            prev_close: 1506.59,
            auction_volume: 500,
            collected_at: '2026-04-14T09:25:02+08:00',
          },
        ],
      }))
    }
    return Promise.resolve(makeRows({
      total: 2,
      rows: [
        ...makeRows().rows,
        {
          symbol: '300750.SZ',
          trade_date: '2026-04-15',
          name: '宁德时代',
          open_gap_pct: 0.071,
          auction_vol_ratio: 14.2,
          auction_price: 214.2,
          prev_close: 200.0,
          auction_volume: 2000,
          collected_at: '2026-04-15T09:25:03+08:00',
        },
      ],
    }))
  })

  const onOpenStock = await renderCard()
  await waitForText('平安银行')

  expect(container.textContent).toContain('平安银行')
  expect(container.textContent).toContain('宁德时代')
  expect(container.textContent).toContain('创')
  expect(container.textContent).toContain('+5.50%')
  expect(container.textContent).toContain('12.4x')

  const row = container.querySelector('tbody tr') as HTMLTableRowElement
  expect(row).toBeTruthy()
  act(() => row.click())
  expect(onOpenStock).toHaveBeenCalledWith(
    '000001.SZ',
    '平安银行',
    [
      { symbol: '000001.SZ', name: '平安银行' },
      { symbol: '300750.SZ', name: '宁德时代' },
    ],
  )

  const select = container.querySelector('select[aria-label="选择历史交易日"]') as HTMLSelectElement
  expect([...select.options].map(o => o.value)).toEqual(['2026-04-15', '2026-04-14'])
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!
    setter.call(select, '2026-04-14')
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await waitForText('贵州茅台')

  expect(container.textContent).toContain('贵州茅台')
  expect(container.textContent).toContain('+6.20%')
})

it('部分批次失败同时展示告警与已保留命中列表', async () => {
  mocks.extDataList.mockResolvedValue({
    items: [makeConfig({
      date_range: ['2026-04-15', '2026-04-15'],
      pull: {
        ...makeConfig().pull!,
        enabled: true,
        last_status: 'partial',
        last_message: '扫描 2500 只 | 部分失败: 第3批超时',
        last_rows: 1,
      },
    })],
  })
  mocks.extDataRows.mockResolvedValue(makeRows())

  await renderCard()
  await waitForText('平安银行')

  expect(container.textContent).toContain('部分批次失败，已保留已成功结果')
  expect(container.textContent).toContain('第3批超时')
  expect(container.textContent).toContain('平安银行')
})

it('历史读取失败展示错误提示而非伪装空态', async () => {
  mocks.extDataList.mockResolvedValue({
    items: [makeConfig({
      date_range: ['2026-04-15', '2026-04-15'],
      pull: { ...makeConfig().pull!, enabled: true, last_status: 'success', last_rows: 1 },
    })],
  })
  mocks.extDataRows.mockRejectedValue(new Error('parquet 读取异常'))

  await renderCard()
  await waitForText('历史记录读取失败')

  expect(container.textContent).toContain('历史记录读取失败')
  expect(container.textContent).toContain('parquet 读取异常')
  expect(container.textContent).not.toContain('当日无符合条件标的')
})
