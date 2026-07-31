import { useEffect, useMemo, useState } from 'react'

import { EChartsCandlestick, SUB_CHARTS } from '@/components/EChartsCandlestick'
import {
  KChartIndicatorControls,
  useKChartIndicatorControls,
} from '@/components/KChartIndicatorControls'
import { StockDailyKChart } from '@/components/StockDailyKChart'
import { cn } from '@/lib/cn'

import {
  toChartBars,
  toChartMarkers,
  toHeadShouldersOverlays,
  toPriceLines,
  toSignalPriceLines,
} from './chartMappings'
import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowFreshnessState, DowTimeframe } from './types'
import { useDowMonitorDetail } from './useDowMonitor'

const TIMEFRAMES: Array<{ value: DowTimeframe; label: string }> = [
  { value: '5m', label: '5分钟' },
  { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' },
  { value: '60m', label: '60分钟' },
  { value: 'day', label: '日线' },
]

function freshnessLabel(freshness: DowFreshnessState | undefined, failed: boolean) {
  if (failed) return '详情连接失败 · 不可交易'
  if (freshness === 'STALE_DATA') return '数据延迟 · 不可交易'
  if (freshness === 'ANALYSIS_PAUSED') return '分析暂停 · 不可交易'
  if (freshness === 'LIVE') return '数据实时'
  return '状态不可用 · 不可交易'
}

function textValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

export function DowMonitorDetailPanel({
  symbol,
  initialTimeframe = '15m',
}: {
  symbol: string
  initialTimeframe?: DowTimeframe
}) {
  const [selectedTimeframe, setSelectedTimeframe] = useState(initialTimeframe)
  const [showLines, setShowLines] = useState(true)
  const [showHeadShoulders, setShowHeadShoulders] = useState(true)
  const indicators = useKChartIndicatorControls()
  const detailQuery = useDowMonitorDetail(symbol, selectedTimeframe, true)

  useEffect(() => {
    setSelectedTimeframe(initialTimeframe)
    setShowLines(true)
    setShowHeadShoulders(true)
  }, [initialTimeframe, symbol])

  const detail = detailQuery.data?.symbol === symbol
    && detailQuery.data.timeframe === selectedTimeframe
    ? detailQuery.data
    : undefined
  const chartBars = useMemo(() => toChartBars(detail?.chart?.bars), [detail?.chart?.bars])
  const markers = useMemo(
    () => toChartMarkers(
      detail?.chart?.turning?.signals,
      detail?.chart?.bars,
      detail?.chart?.signals,
    ),
    [detail?.chart?.bars, detail?.chart?.signals, detail?.chart?.turning?.signals],
  )
  const headShouldersOverlays = useMemo(
    () => toHeadShouldersOverlays(detail?.chart?.headShoulders, detail?.chart?.bars),
    [detail?.chart?.bars, detail?.chart?.headShoulders],
  )
  const detailPriceLines = useMemo(
    () => toPriceLines(detail?.chart?.lines, detail?.chart?.bars, detail?.chart?.longTerm),
    [detail?.chart?.bars, detail?.chart?.lines, detail?.chart?.longTerm],
  )
  const signalPriceLines = useMemo(() => toSignalPriceLines(markers), [markers])
  const subHeight = indicators.activeIndicators.reduce((height, key) => {
    const definition = SUB_CHARTS.find(item => item.key === key)
    return definition ? height + definition.height + 20 : height
  }, 0)
  const chartHeight = Math.max(460, 540 + subHeight)
  const blocked = detailQuery.isError || !detail || detail.freshness_state !== 'LIVE'
  const action = textValue(detail?.snapshot?.action)
  const shape = textValue(detail?.snapshot?.phase)

  return (
    <section
      role="region"
      aria-label={`${symbol} 详细走势`}
      data-testid="dow-detail-state"
      data-tradable={blocked ? 'false' : 'true'}
      className="mt-3 min-w-0 overflow-hidden rounded-card border border-border bg-surface"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 sm:px-4">
        <h2 className="font-mono text-sm font-semibold">{symbol}</h2>
        <span className={cn(
          'text-xs font-medium',
          blocked ? 'text-danger' : 'text-emerald-400',
        )}>
          {freshnessLabel(detail?.freshness_state, detailQuery.isError)}
        </span>
        {action && <span className="text-xs font-medium text-secondary">{action}</span>}
        {shape && <span className="text-xs text-muted">{shape}</span>}
        <span className="ml-auto font-mono text-[10px] text-muted">
          {formatServerTimestamp(detail?.source_timestamp) || '等待数据'}
        </span>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 sm:px-4">
        <div className="flex h-7 items-center overflow-hidden rounded-btn border border-border bg-base">
          {TIMEFRAMES.map(option => (
            <button
              key={option.value}
              type="button"
              aria-label={option.label}
              aria-pressed={selectedTimeframe === option.value}
              onClick={() => setSelectedTimeframe(option.value)}
              className={cn(
                'h-full px-2.5 text-xs font-medium transition-colors',
                selectedTimeframe === option.value
                  ? 'bg-accent/15 text-accent'
                  : 'text-muted hover:bg-elevated hover:text-secondary',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
        <KChartIndicatorControls
          state={indicators}
          className="flex flex-wrap items-center gap-1.5"
        />
        <div className="flex items-center gap-1.5 border-l border-border pl-2">
          <span className="text-[10px] text-muted">趋势线</span>
          <button
            type="button"
            role="switch"
            aria-label="显示趋势线和压力线"
            aria-checked={showLines}
            onClick={() => setShowLines(value => !value)}
            className={cn(
              'relative h-3.5 w-6 rounded-full transition-colors',
              showLines ? 'bg-accent' : 'bg-elevated',
            )}
          >
            <span className={cn(
              'absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform',
              showLines ? 'translate-x-3' : 'translate-x-0.5',
            )} />
          </button>
        </div>
        <div className="flex items-center gap-1.5 border-l border-border pl-2">
          <span className="text-[10px] text-muted">头肩形态</span>
          <button
            type="button"
            role="switch"
            aria-label="显示头肩形态"
            aria-checked={showHeadShoulders}
            onClick={() => setShowHeadShoulders(value => !value)}
            className={cn(
              'relative h-3.5 w-6 rounded-full transition-colors',
              showHeadShoulders ? 'bg-accent' : 'bg-elevated',
            )}
          >
            <span className={cn(
              'absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform',
              showHeadShoulders ? 'translate-x-3' : 'translate-x-0.5',
            )} />
          </button>
        </div>
      </div>

      <div className="min-h-[360px] overflow-auto px-2 py-2 sm:px-4">
        {detailQuery.isLoading && !detail && (
          <div className="py-12 text-center text-sm text-muted">
            正在加载 {TIMEFRAMES.find(item => item.value === selectedTimeframe)?.label} K线…
          </div>
        )}
        {detailQuery.isError && (
          <div role="alert" className="py-12 text-center text-sm text-danger">
            详情连接失败 · 当前状态不可交易
          </div>
        )}
        {!detailQuery.isLoading && !detailQuery.isError && !detail && (
          <div role="alert" className="py-12 text-center text-sm text-danger">
            详情数据不可用 · 当前状态不可交易
          </div>
        )}
        {detail && chartBars.length === 0 && (
          <div role="alert" className="py-12 text-center text-sm text-danger">
            K线数据格式异常 · 当前状态不可交易
          </div>
        )}
        {detail && chartBars.length > 0 && selectedTimeframe === 'day' && (
          <StockDailyKChart
            symbol={symbol}
            height={560}
            chartData={chartBars}
            markers={markers}
            priceLines={showLines ? detailPriceLines : []}
            showIndicatorControls={false}
            showLimitMarkers={false}
            showMarkerToggle={false}
            indicatorState={indicators}
          />
        )}
        {detail && chartBars.length > 0 && selectedTimeframe !== 'day' && (
          <EChartsCandlestick
            data={chartBars}
            markers={markers}
            headShouldersOverlays={showHeadShoulders ? headShouldersOverlays : []}
            priceLines={showLines ? signalPriceLines : []}
            height={chartHeight}
            visibleBars={320}
            activeIndicators={indicators.activeIndicators}
            volumeCompare={indicators.volumeCompare}
            showMA
            showInfoBar
          />
        )}
      </div>
    </section>
  )
}
