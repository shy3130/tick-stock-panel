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
const BUY_RED = '#EF4444'
const SELL_GREEN = '#22C55E'

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
  if (!isRecord(chart)) return []
  const barTimes = new Set(bars.map(bar => bar.timestamp))
  const turningSignals = validTurningSignals(chart, bars, barTimes)
  if (turningSignals.length > 0) return turningSignals
  if (!Array.isArray(chart.signals)) return []
  return chart.signals.filter((signal): signal is DowMonitorSignal => (
    isRecord(signal)
    && (signal.side === 'BUY' || signal.side === 'SELL' || signal.side === 'RISK')
    && isTimestamp(signal.barTime)
    && barTimes.has(signal.barTime)
    && isFiniteNumber(signal.price)
  ))
}

function validTurningSignals(
  chart: Record<string, unknown>,
  bars: DowMonitorBar[],
  barTimes: Set<string>,
): DowMonitorSignal[] {
  if (!isRecord(chart.turning) || !Array.isArray(chart.turning.signals)) return []
  const barByTime = new Map(bars.map(bar => [bar.timestamp, bar]))
  return chart.turning.signals.flatMap((signal): DowMonitorSignal[] => {
    if (
      !isRecord(signal)
      || (signal.side !== 'BUY' && signal.side !== 'SELL' && signal.side !== 'RISK')
      || (signal.stage !== 'TRIGGER' && signal.stage !== 'CONFIRMED')
      || !isStrictDoubleBreakSignal(signal)
      || !isTimestamp(signal.actionableTime)
      || !barTimes.has(signal.actionableTime)
      || !isFiniteNumber(signal.price)
    ) {
      return []
    }
    const bar = barByTime.get(signal.actionableTime)
    const volumeRatio = isFiniteNumber(bar?.vol_ratio_5d)
      ? bar.vol_ratio_5d
      : isFiniteNumber(bar?.vol_ma5) && isFiniteNumber(bar?.volume) && bar.vol_ma5 > 0
      ? bar.volume / bar.vol_ma5
      : null
    return [{
      side: signal.side,
      barIndex: isFiniteNumber(signal.actionableIndex) ? signal.actionableIndex : 0,
      barTime: signal.actionableTime,
      price: signal.price,
      reason: typeof signal.triggerPath === 'string' ? signal.triggerPath : '',
      confidence: typeof signal.stage === 'string' ? signal.stage : '',
      lineId: typeof signal.lineId === 'string' ? signal.lineId : null,
      firstCrossIndex: isFiniteNumber(signal.detectedIndex) ? signal.detectedIndex : null,
      firstCrossTime: isTimestamp(signal.detectedTime) ? signal.detectedTime : null,
      volumeRatio,
      pattern: Array.isArray(signal.reasonCodes) ? signal.reasonCodes.join('/') : null,
      evidence: [],
      stage: typeof signal.stage === 'string' ? signal.stage : null,
      triggerPath: typeof signal.triggerPath === 'string' ? signal.triggerPath : null,
      lineValue: isFiniteNumber(signal.lineValue) ? signal.lineValue : null,
      lineRole: typeof signal.lineRole === 'string' ? signal.lineRole : null,
      lineAnchorTimes: Array.isArray(signal.lineAnchorTimes)
        ? signal.lineAnchorTimes.filter((item): item is string => typeof item === 'string')
        : null,
      lineAnchorPrices: Array.isArray(signal.lineAnchorPrices)
        ? signal.lineAnchorPrices.filter((item): item is number => isFiniteNumber(item))
        : null,
      structurePivotId: typeof signal.structurePivotId === 'string' ? signal.structurePivotId : null,
      structurePivotPrice: isFiniteNumber(signal.structurePivotPrice) ? signal.structurePivotPrice : null,
      structurePivotTime: typeof signal.structurePivotTime === 'string' ? signal.structurePivotTime : null,
      reasonCodes: Array.isArray(signal.reasonCodes)
        ? signal.reasonCodes.filter((item): item is string => typeof item === 'string')
        : [],
    }]
  })
}

function isStrictDoubleBreakSignal(signal: Record<string, unknown>) {
  if (!isFiniteNumber(signal.lineValue) || !isFiniteNumber(signal.structurePivotPrice)) return false
  const reasonCodes = Array.isArray(signal.reasonCodes)
    ? signal.reasonCodes.filter((item): item is string => typeof item === 'string')
    : []
  if (signal.triggerPath === 'PRIMARY_STRUCTURE') {
    return reasonCodes.includes('PRIMARY_LINE_AND_STRUCTURE_BROKEN')
  }
  if (signal.triggerPath === 'DIRECT_STRUCTURE') {
    return reasonCodes.includes('LINE_AND_NEAREST_LEVEL_BROKEN')
      || reasonCodes.includes('LINE_AND_KEY_STRUCTURE_BROKEN')
  }
  if (signal.triggerPath === 'TWO_BAR_RETEST') {
    if (signal.side === 'BUY') {
      return reasonCodes.includes('FIRST_ACCEPTANCE_HIGH_BROKEN')
    }
    if (signal.side === 'SELL' || signal.side === 'RISK') {
      return reasonCodes.includes('FIRST_ACCEPTANCE_LOW_BROKEN')
    }
  }
  return false
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

function signalColor(side: string) {
  return side === 'BUY' ? BUY_RED : SELL_GREEN
}

function signalConclusion(side: string) {
  const trendText = side === 'BUY'
    ? '\u7a81\u7834\u4e0b\u964d\u8d8b\u52bf\u7ebf'
    : '\u8dcc\u7834\u4e0a\u6da8\u8d8b\u52bf\u7ebf'
  const levelText = side === 'BUY'
    ? '\u7a81\u7834\u524d\u9ad8/\u538b\u529b\u4f4d'
    : '\u8dcc\u7834\u524d\u4f4e/\u652f\u6491\u4f4d'
  if (side === 'BUY') {
    return `\u4e70\u70b9\uff1a${trendText} + ${levelText}`
  }
  return `\u5356\u70b9\uff1a${trendText} + ${levelText}`
}

function formatPrice(value: unknown) {
  return isFiniteNumber(value) ? value.toFixed(3) : '-'
}

function translateStage(value: unknown) {
  if (value === 'CONFIRMED') return '\u5df2\u786e\u8ba4'
  if (value === 'TRIGGER') return '\u89e6\u53d1'
  if (value === 'WARNING') return '\u9884\u8b66'
  return value ? String(value) : '-'
}

function translateTriggerPath(value: unknown) {
  if (value === 'PRIMARY_STRUCTURE') return '\u4e3b\u8d8b\u52bf\u7ebf+\u7ed3\u6784\u4f4d\u53cc\u7a81\u7834'
  if (value === 'DIRECT_STRUCTURE') return '\u8d8b\u52bf\u7ebf+\u5173\u952e\u4f4d\u76f4\u63a5\u53cc\u7a81\u7834'
  if (value === 'TWO_BAR_RETEST') return '\u4e8c\u6b21\u56de\u8e29\u786e\u8ba4'
  if (value === 'LINE_BREAK') return '\u8d8b\u52bf\u7ebf\u7a81\u7834'
  return value ? String(value) : '-'
}

function translateReasonCode(value: string) {
  if (value === 'PRIMARY_LINE_AND_STRUCTURE_BROKEN') return '\u4e3b\u8d8b\u52bf\u7ebf\u4e0e\u7ed3\u6784\u4f4d\u540c\u65f6\u7a81\u7834'
  if (value === 'LINE_AND_NEAREST_LEVEL_BROKEN') return '\u8d8b\u52bf\u7ebf\u4e0e\u9644\u8fd1\u5173\u952e\u4f4d\u540c\u65f6\u7a81\u7834'
  if (value === 'LINE_AND_KEY_STRUCTURE_BROKEN') return '\u8d8b\u52bf\u7ebf\u4e0e\u524d\u9ad8/\u524d\u4f4e\u540c\u65f6\u7a81\u7834'
  if (value === 'SECOND_CLOSE_ABOVE') return '\u7b2c\u4e8c\u6839K\u7ebf\u7ad9\u4e0a\u786e\u8ba4'
  if (value === 'SECOND_CLOSE_BELOW') return '\u7b2c\u4e8c\u6839K\u7ebf\u8dcc\u7834\u786e\u8ba4'
  if (value === 'HIGHER_SECOND_CLOSE') return '\u7b2c\u4e8c\u6839\u6536\u76d8\u66f4\u5f3a'
  if (value === 'LOWER_SECOND_CLOSE') return '\u7b2c\u4e8c\u6839\u6536\u76d8\u66f4\u5f31'
  if (value === 'FIRST_ACCEPTANCE_HIGH_BROKEN') return '\u7a81\u7834\u9996\u6b21\u627f\u63a5\u9ad8\u70b9'
  if (value === 'FIRST_ACCEPTANCE_LOW_BROKEN') return '\u8dcc\u7834\u9996\u6b21\u627f\u63a5\u4f4e\u70b9'
  return value.replaceAll('_', ' ').toLowerCase()
}

function translateReasonCodes(values: string[] | undefined) {
  if (!Array.isArray(values) || values.length === 0) return null
  return values.map(translateReasonCode).join('\uff1b')
}

function trendLineLabel(signal: DowMonitorSignal) {
  const role = signal.lineRole === 'ACCELERATION' ? '\u52a0\u901f' : '\u4e3b'
  const direction = signal.side === 'BUY' ? '\u4e0b\u964d' : '\u4e0a\u6da8'
  return `${role}${direction}\u8d8b\u52bf\u7ebf`
}

function keyLevelLabel(signal: DowMonitorSignal) {
  return signal.side === 'BUY' ? '\u524d\u9ad8/\u538b\u529b\u4f4d' : '\u524d\u4f4e/\u652f\u6491\u4f4d'
}

function formatSignalTime(value: string) {
  const parsed = new Date(value)
  if (!Number.isFinite(parsed.getTime())) return value
  const month = String(parsed.getMonth() + 1).padStart(2, '0')
  const day = String(parsed.getDate()).padStart(2, '0')
  const hour = String(parsed.getHours()).padStart(2, '0')
  const minute = String(parsed.getMinutes()).padStart(2, '0')
  return `${month}-${day} ${hour}:${minute}`
}

function formatAnchorPair(times: string[] | null | undefined, prices: number[] | null | undefined) {
  if (!Array.isArray(times) || times.length < 2) return null
  const firstPrice = Array.isArray(prices) && isFiniteNumber(prices[0]) ? ` ${prices[0].toFixed(3)}` : ''
  const secondPrice = Array.isArray(prices) && isFiniteNumber(prices[1]) ? ` ${prices[1].toFixed(3)}` : ''
  return `${formatSignalTime(times[0])}${firstPrice} / ${formatSignalTime(times[1])}${secondPrice}`
}

function formatLevelPoint(time: string | null | undefined, price: number | null | undefined) {
  if (!time) return null
  const priceText = isFiniteNumber(price) ? ` ${price.toFixed(3)}` : ''
  return `${formatSignalTime(time)}${priceText}`
}

function escapeHtml(value: unknown) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function signalEvidenceSummary(signal: DowMonitorSignal) {
  if (!Array.isArray(signal.evidence)) return null
  const parts = signal.evidence.flatMap(item => {
    if (!isRecord(item)) return []
    const code = typeof item.code === 'string' ? item.code : ''
    const strength = typeof item.strength === 'string' ? item.strength : ''
    const detector = typeof item.detector === 'string' ? item.detector : ''
    const text = [code, strength, detector].filter(Boolean).join('/')
    return text ? [text] : []
  })
  return parts.length > 0 ? parts.slice(0, 3).join(' | ') : null
}

function signalTooltipHtml(signal: DowMonitorSignal) {
  const trendLabel = trendLineLabel(signal)
  const levelLabel = keyLevelLabel(signal)
  const reasonCodesText = translateReasonCodes(signal.reasonCodes)
  const rows: Array<[string, string]> = [
    ['\u89e6\u53d1\u65f6\u95f4', formatSignalTime(signal.barTime)],
    ['\u89e6\u53d1\u4ef7', signal.price.toFixed(3)],
    [trendLabel, formatPrice(signal.lineValue)],
    [levelLabel, formatPrice(signal.structurePivotPrice)],
    ['\u5224\u65ad\u903b\u8f91', translateTriggerPath(signal.triggerPath || signal.reason)],
    ['\u786e\u8ba4\u72b6\u6001', translateStage(signal.stage || signal.confidence)],
  ]
  const lineAnchors = formatAnchorPair(signal.lineAnchorTimes, signal.lineAnchorPrices)
  if (lineAnchors) rows.splice(3, 0, ['\u8d8b\u52bf\u7ebfK\u4f4d', lineAnchors])
  const levelPoint = formatLevelPoint(signal.structurePivotTime, signal.structurePivotPrice)
  if (levelPoint) rows.splice(lineAnchors ? 5 : 4, 0, ['\u652f\u6491/\u538b\u529bK\u4f4d', levelPoint])
  if (signal.firstCrossTime) rows.push(['\u9996\u6b21\u7a81\u7834', formatSignalTime(signal.firstCrossTime)])
  if (isFiniteNumber(signal.volumeRatio)) rows.push(['\u91cf\u80fd', `${signal.volumeRatio.toFixed(2)}x`])
  if (reasonCodesText) rows.push(['\u5173\u952e\u8bc1\u636e', reasonCodesText])
  const evidence = signalEvidenceSummary(signal)
  if (evidence && !reasonCodesText) rows.push(['\u5173\u952e\u8bc1\u636e', evidence])
  return [
    `<div style="font-size:11px;font-weight:650;margin-bottom:5px;color:${signalColor(signal.side)}">${escapeHtml(signalConclusion(signal.side))}</div>`,
    ...rows.map(([label, value]) => (
      `<div style="min-width:220px;max-width:330px;white-space:normal;line-height:1.35;font-size:11px">`
      + `<span style="color:#9CA3AF">${escapeHtml(label)}\uff1a</span>`
      + `<b style="color:#E5E7EB;font-weight:500;font-size:11px">${escapeHtml(value)}</b>`
      + '</div>'
    )),
  ].join('')
}

export function buildDowMiniChartOption(
  chart: DowMonitorChart | unknown,
  colors = {
    border: '#353539',
    grid: 'rgba(255,255,255,0.06)',
  },
  options: { showLines?: boolean } = {},
): EChartsOption {
  const bars = validBars(chart)
  const backendLines = options.showLines === false ? [] : validLines(chart)
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
  const longTermAnchors = options.showLines === false ? null : completeLongTermAnchors(chart)
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
    tooltip: {
      show: true,
      trigger: 'item',
      confine: true,
      borderWidth: 1,
      backgroundColor: '#111217',
      borderColor: 'rgba(229, 231, 235, 0.18)',
      textStyle: {
        color: '#E5E7EB',
        fontSize: 12,
      },
      formatter: (params: unknown) => {
        if (!isRecord(params) || !isRecord(params.data)) return ''
        const signal = params.data.signal
        return isRecord(signal) ? signalTooltipHtml(signal as unknown as DowMonitorSignal) : ''
      },
    },
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
            value: signal.price,
            signal,
            itemStyle: {
              color: signalColor(signal.side),
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
  showLines = true,
}: {
  chart: DowMonitorChart
  testId?: string
  height?: number
  showLines?: boolean
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const instanceRef = useRef<ECharts | null>(null)
  const chartTheme = useChartTheme()
  const option = useMemo(
    () => buildDowMiniChartOption(chart, chartTheme, { showLines }),
    [chart, chartTheme, showLines],
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
