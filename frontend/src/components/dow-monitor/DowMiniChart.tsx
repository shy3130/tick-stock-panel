import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { useEffect, useMemo, useRef } from 'react'

import { useChartTheme } from '@/lib/theme'

import type {
  DowMonitorBar,
  DowMonitorChart,
  DowMonitorLine,
  DowMonitorSignal,
  DowSignalSide,
} from './types'

const CANDLE_UP = '#C74040'
const CANDLE_DOWN = '#2D9B65'
const SUPPORT_BLUE = '#3B82F6'
const RESISTANCE_MAGENTA = '#D946EF'
const LONG_TERM_AMBER = '#F59E0B'
const BUY_GREEN = '#22C55E'
const SELL_RED = '#EF4444'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && Number.isFinite(Date.parse(value))
}

function validBars(chart: unknown): DowMonitorBar[] {
  if (!isRecord(chart) || !Array.isArray(chart.bars)) return []
  return chart.bars.filter((bar): bar is DowMonitorBar => (
    isRecord(bar)
    && isFiniteNumber(bar.index)
    && isTimestamp(bar.timestamp)
    && isFiniteNumber(bar.open)
    && isFiniteNumber(bar.high)
    && isFiniteNumber(bar.low)
    && isFiniteNumber(bar.close)
    && isFiniteNumber(bar.volume)
  ))
}

function validLines(chart: unknown): DowMonitorLine[] {
  if (!isRecord(chart) || !Array.isArray(chart.lines)) return []
  return chart.lines.filter((line): line is DowMonitorLine => (
    isRecord(line)
    && typeof line.id === 'string'
    && line.id.length > 0
    && (line.side === 'SUPPORT' || line.side === 'RESISTANCE')
    && (line.role === 'MAIN' || line.role === 'ACCELERATION')
    && Array.isArray(line.anchorTimes)
    && line.anchorTimes.length >= 2
    && isTimestamp(line.anchorTimes[0])
    && isTimestamp(line.anchorTimes[1])
    && Array.isArray(line.anchorPrices)
    && line.anchorPrices.length >= 2
    && isFiniteNumber(line.anchorPrices[0])
    && isFiniteNumber(line.anchorPrices[1])
  ))
}

function validSignals(chart: unknown, bars: DowMonitorBar[]): DowMonitorSignal[] {
  if (!isRecord(chart) || !Array.isArray(chart.signals)) return []
  const barTimes = new Set(bars.map(bar => bar.timestamp))
  return chart.signals.filter((signal): signal is DowMonitorSignal => (
    isRecord(signal)
    && (signal.side === 'BUY' || signal.side === 'SELL' || signal.side === 'RISK')
    && isTimestamp(signal.barTime)
    && barTimes.has(signal.barTime)
    && isFiniteNumber(signal.price)
  ))
}

function completeLongTermAnchors(chart: unknown) {
  if (!isRecord(chart) || !isRecord(chart.longTerm)) return null
  const longTerm = chart.longTerm
  if (
    !isTimestamp(longTerm.first_anchor_time)
    || !isTimestamp(longTerm.second_anchor_time)
    || !isFiniteNumber(longTerm.first_anchor_price)
    || !isFiniteNumber(longTerm.second_anchor_price)
  ) {
    return null
  }
  return [
    [longTerm.first_anchor_time, longTerm.first_anchor_price],
    [longTerm.second_anchor_time, longTerm.second_anchor_price],
  ]
}

export function getLatestValidDowSignalSide(chart: unknown): DowSignalSide | null {
  const bars = validBars(chart)
  const side = validSignals(chart, bars).at(-1)?.side
  return side === 'BUY' || side === 'SELL' || side === 'RISK' ? side : null
}

export function buildDowMiniChartOption(
  chart: DowMonitorChart | unknown,
  colors = {
    border: '#353539',
    grid: 'rgba(255,255,255,0.06)',
  },
): EChartsOption {
  const bars = validBars(chart)
  const backendLines = validLines(chart)
  const backendSignals = validSignals(chart, bars)
  const lineSeries: Array<Record<string, unknown>> = backendLines.map(line => {
    const acceleration = line.role === 'ACCELERATION'
    const resistance = line.side === 'RESISTANCE'
    return {
      id: line.id,
      name: `${line.side} ${line.role}`,
      type: 'line',
      data: [
        [line.anchorTimes[0], line.anchorPrices[0]],
        [line.anchorTimes[1], line.anchorPrices[1]],
      ],
      showSymbol: false,
      silent: true,
      connectNulls: true,
      lineStyle: {
        color: resistance ? RESISTANCE_MAGENTA : SUPPORT_BLUE,
        type: acceleration ? 'dashed' : 'solid',
        width: acceleration ? 1.5 : 2,
      },
      z: acceleration ? 4 : 5,
    }
  })
  const longTermAnchors = completeLongTermAnchors(chart)
  if (longTermAnchors) {
    lineSeries.push({
      id: 'long-term',
      name: '长期趋势',
      type: 'line',
      data: longTermAnchors,
      showSymbol: false,
      silent: true,
      connectNulls: true,
      lineStyle: {
        color: LONG_TERM_AMBER,
        type: 'solid',
        width: 2,
      },
      z: 3,
    })
  }

  return {
    animation: false,
    grid: { top: 4, right: 4, bottom: 4, left: 4, containLabel: false },
    xAxis: {
      type: 'category',
      data: bars.map(bar => bar.timestamp),
      boundaryGap: true,
      axisLine: { show: false, lineStyle: { color: colors.border } },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: true, lineStyle: { color: colors.grid } },
    },
    tooltip: { show: false },
    series: [
      {
        id: 'candles',
        type: 'candlestick',
        data: bars.map(bar => [bar.open, bar.close, bar.low, bar.high]),
        itemStyle: {
          color: CANDLE_UP,
          color0: CANDLE_DOWN,
          borderColor: CANDLE_UP,
          borderColor0: CANDLE_DOWN,
        },
        markPoint: {
          symbol: 'circle',
          symbolSize: 7,
          label: { show: false },
          data: backendSignals.map(signal => ({
            name: signal.side,
            coord: [signal.barTime, signal.price],
            itemStyle: {
              color: signal.side === 'BUY' ? BUY_GREEN : SELL_RED,
            },
          })),
        },
        z: 2,
      },
      ...lineSeries,
    ],
  }
}

export function DowMiniChart({
  chart,
  testId,
  height = 96,
}: {
  chart: DowMonitorChart
  testId?: string
  height?: number
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const instanceRef = useRef<ECharts | null>(null)
  const chartTheme = useChartTheme()
  const option = useMemo(
    () => buildDowMiniChartOption(chart, chartTheme),
    [chart, chartTheme],
  )

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const instance = echarts.init(container, undefined, { renderer: 'canvas' })
    instanceRef.current = instance
    const observer = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(() => instance.resize())
    observer?.observe(container)
    return () => {
      observer?.disconnect()
      instance.dispose()
      instanceRef.current = null
    }
  }, [])

  useEffect(() => {
    instanceRef.current?.setOption(option, true)
  }, [option])

  return (
    <div
      ref={containerRef}
      data-testid={testId}
      aria-label="迷你K线"
      className="w-full"
      style={{ height }}
    />
  )
}
