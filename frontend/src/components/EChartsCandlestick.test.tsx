import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { EChartsOption } from 'echarts'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  EChartsCandlestick,
  type OHLC,
} from './EChartsCandlestick'
import {
  KChartIndicatorControls,
  useKChartIndicatorControls,
} from './KChartIndicatorControls'

const chartMocks = vi.hoisted(() => ({
  dispatchAction: vi.fn(),
  dispose: vi.fn(),
  getOption: vi.fn(() => ({ dataZoom: [{ start: 0, end: 100 }] })),
  off: vi.fn(),
  on: vi.fn(),
  resize: vi.fn(),
  setOption: vi.fn(),
}))

vi.mock('echarts', () => ({
  init: () => chartMocks,
}))

const rows: OHLC[] = [
  {
    date: '2026-07-23T09:30:00+08:00',
    open: 10,
    high: 11,
    low: 9.5,
    close: 10.8,
    volume: 100,
    ma5: 10.1,
    ma10: 10,
    ma20: 9.9,
    ma60: 9.5,
    macd_dif: 0.3,
    macd_dea: 0.2,
    macd_hist: 0.2,
    rsi_6: 61,
    rsi_14: 58,
    rsi_24: 55,
    kdj_k: 68,
    kdj_d: 62,
    kdj_j: 80,
    boll_upper: 11.2,
    boll_lower: 9.1,
  },
  {
    date: '2026-07-23T09:35:00+08:00',
    open: 10.8,
    high: 11.4,
    low: 10.5,
    close: 11.2,
    volume: 120,
    ma5: 10.4,
    ma10: 10.2,
    ma20: 10,
    ma60: 9.6,
    macd_dif: 0.4,
    macd_dea: 0.25,
    macd_hist: 0.3,
    rsi_6: 65,
    rsi_14: 60,
    rsi_24: 57,
    kdj_k: 72,
    kdj_d: 65,
    kdj_j: 86,
    boll_upper: 11.5,
    boll_lower: 9.2,
  },
]

function Harness({
  data = rows,
  markers = [],
}: {
  data?: OHLC[]
  markers?: Parameters<typeof EChartsCandlestick>[0]['markers']
}) {
  const indicators = useKChartIndicatorControls()
  return (
    <>
      <KChartIndicatorControls state={indicators} />
      <EChartsCandlestick
        data={data}
        markers={markers}
        activeIndicators={indicators.activeIndicators}
        volumeCompare={indicators.volumeCompare}
      />
    </>
  )
}

function latestOption(): EChartsOption {
  return chartMocks.setOption.mock.calls.at(-1)?.[0] as EChartsOption
}

describe('ECharts candlestick shared controls', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      disconnect() {}
    })
  })

  it('renders nonempty existing MACD, RSI, KDJ, and BOLL series after control clicks', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    for (const label of ['MACD', 'RSI', 'KDJ', 'BOLL']) {
      await user.click(screen.getByRole('button', { name: label }))
    }

    const series = latestOption().series as Array<Record<string, unknown>>
    for (const name of ['DIF', 'DEA', 'MACD', 'RSI6', 'RSI14', 'RSI24', 'K', 'D', 'J', 'BOLL上', 'BOLL下']) {
      const values = series.find(item => item.name === name)?.data as unknown[] | undefined
      expect(values).toBeDefined()
      expect(values?.some(value => value !== '-' && value != null)).toBe(true)
    }
  })

  it('uses the exact BUY signal price and supplied green color initially', () => {
    render(
      <Harness markers={[{
        date: rows[1].date,
        kind: 'buy',
        price: 10.95,
        color: '#22C55E',
        label: '买',
      }]} />,
    )

    const series = latestOption().series as Array<Record<string, any>>
    const candle = series.find(item => item.name === 'K')
    expect(candle?.markPoint.data[0].coord).toEqual([rows[1].date, 10.95])
    expect(candle?.markPoint.data[0].itemStyle.color).toBe('#22C55E')
  })

  it('keeps the exact BUY signal price and supplied green color after compact zoom', () => {
    const compactRows = Array.from({ length: 61 }, (_, index): OHLC => ({
      ...rows[index % rows.length],
      date: new Date(Date.UTC(2026, 6, 23, 1, 30 + index * 5)).toISOString(),
    }))
    const marker = {
      date: compactRows[30].date,
      kind: 'buy' as const,
      price: 14.25,
      color: '#22C55E',
      label: '买',
    }
    render(<Harness data={compactRows} markers={[marker]} />)
    chartMocks.setOption.mockClear()

    const zoomRegistration = chartMocks.on.mock.calls.find(([event]) => event === 'dataZoom')
    expect(zoomRegistration).toBeDefined()
    const onDataZoom = zoomRegistration?.[1] as (() => void)
    onDataZoom()

    const series = latestOption().series as Array<Record<string, any>>
    const candle = series.find(item => item.name === 'K')
    expect(candle?.markPoint.data[0].coord).toEqual([marker.date, marker.price])
    expect(candle?.markPoint.data[0].itemStyle.color).toBe('#22C55E')
  })
})
