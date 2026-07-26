import { Maximize2, Minimize2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { EChartsCandlestick, SUB_CHARTS } from '@/components/EChartsCandlestick'
import {
  KChartIndicatorControls,
  useKChartIndicatorControls,
} from '@/components/KChartIndicatorControls'
import { Modal } from '@/components/Modal'
import { StockDailyKChart } from '@/components/StockDailyKChart'
import { cn } from '@/lib/cn'

import { formatServerTimestamp } from './formatServerTimestamp'
import { toChartBars, toChartMarkers, toPriceLines, toSignalPriceLines } from './chartMappings'
import type { DowFreshnessState, DowTimeframe } from './types'
import { useDowMonitorDetail } from './useDowMonitor'

const TIMEFRAMES: Array<{ value: DowTimeframe; label: string }> = [
  { value: '5m', label: '5分' },
  { value: '15m', label: '15分' },
  { value: '30m', label: '30分' },
  { value: '60m', label: '60分' },
  { value: 'day', label: '日K' },
]

function freshnessLabel(freshness: DowFreshnessState | undefined, disconnected: boolean) {
  if (disconnected) return '详情连接失败 · 不可交易'
  if (freshness === 'STALE_DATA') return '数据延迟 · 不可交易'
  if (freshness === 'ANALYSIS_PAUSED') return '分析暂停 · 不可交易'
  if (freshness === 'LIVE') return '数据实时'
  return '状态不可用 · 不可交易'
}

function textValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

export function DowMonitorDetailDialog({
  symbol,
  timeframe,
  open,
  onClose,
}: {
  symbol: string
  timeframe: DowTimeframe
  open: boolean
  onClose: () => void
}) {
  const [selectedTimeframe, setSelectedTimeframe] = useState(timeframe)
  const [fullscreen, setFullscreen] = useState(false)
  const indicators = useKChartIndicatorControls()
  const detailQuery = useDowMonitorDetail(symbol, selectedTimeframe, open)

  useEffect(() => {
    if (!open) return
    setSelectedTimeframe(timeframe)
    setFullscreen(false)
  }, [open, symbol, timeframe])

  const detail = detailQuery.data?.symbol === symbol
    && detailQuery.data.timeframe === selectedTimeframe
    ? detailQuery.data
    : undefined
  const chartBars = useMemo(() => toChartBars(detail?.chart?.bars), [detail?.chart?.bars])
  const markers = useMemo(
    () => toChartMarkers(detail?.chart?.turning?.signals, detail?.chart?.bars, detail?.chart?.signals),
    [detail?.chart?.bars, detail?.chart?.signals, detail?.chart?.turning?.signals],
  )
  const detailPriceLines = useMemo(
    () => toPriceLines(detail?.chart?.lines, detail?.chart?.bars, detail?.chart?.longTerm),
    [detail?.chart?.bars, detail?.chart?.lines, detail?.chart?.longTerm],
  )
  const signalPriceLines = useMemo(
    () => toSignalPriceLines(markers),
    [markers],
  )
  const subHeight = indicators.activeIndicators.reduce((height, key) => {
    const definition = SUB_CHARTS.find(item => item.key === key)
    return definition ? height + definition.height + 20 : height
  }, 0)
  const chartHeight = Math.max(520, 620 + subHeight)
  const blocked = detailQuery.isError || !detail || detail.freshness_state !== 'LIVE'
  const sourceTime = formatServerTimestamp(detail?.source_timestamp)
  const updatedTime = formatServerTimestamp(detail?.updated_at)
  const action = textValue(detail?.snapshot?.action)
  const shape = textValue(detail?.snapshot?.phase)
  const completion = textValue(detail?.snapshot?.bar_completion)

  if (!open) return null

  return (
    <Modal
      onClose={onClose}
      ariaLabel={`${symbol} 完整K线`}
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-2 backdrop-blur-sm sm:p-4"
      panelClassName={cn(
        'flex max-h-[96vh] flex-col overflow-hidden border border-border bg-surface shadow-2xl',
        fullscreen
          ? 'h-[calc(100vh-1rem)] w-[calc(100vw-1rem)] rounded-card'
          : 'h-[92vh] w-[96vw] max-w-[1600px] rounded-card',
      )}
    >
      <div
        data-testid="dow-detail-state"
        data-tradable={blocked ? 'false' : 'true'}
        className="flex min-h-0 flex-1 flex-col"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-border px-3 py-2 sm:px-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h2 className="font-mono text-sm font-semibold">{symbol}</h2>
              <span className={cn(
                'text-xs font-medium',
                blocked ? 'text-danger' : 'text-emerald-400',
              )}>
                {freshnessLabel(detail?.freshness_state, detailQuery.isError)}
              </span>
              {action && <span className="text-xs font-medium text-secondary">{action}</span>}
              {shape && <span className="text-xs text-muted">{shape}</span>}
              {completion && <span className="font-mono text-[10px] text-muted">{completion}</span>}
            </div>
            <div className="mt-0.5 flex flex-wrap gap-x-3 font-mono text-[10px] text-muted">
              {sourceTime && <span>源 {sourceTime}</span>}
              {updatedTime && <span>更新 {updatedTime}</span>}
            </div>
          </div>
          <button
            type="button"
            aria-label={fullscreen ? '退出全屏' : '全屏查看'}
            onClick={() => setFullscreen(value => !value)}
            className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-secondary"
          >
            {fullscreen
              ? <Minimize2 className="h-4 w-4" />
              : <Maximize2 className="h-4 w-4" />}
          </button>
          <button
            type="button"
            aria-label="关闭完整K线"
            onClick={onClose}
            className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-secondary"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2 sm:px-4">
          <div className="flex h-7 items-center overflow-hidden rounded-btn border border-border bg-base">
            {TIMEFRAMES.map(option => (
              <button
                key={option.value}
                type="button"
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
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-2 py-2 sm:px-4">
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
              height={620}
              chartData={chartBars}
              markers={markers}
              priceLines={detailPriceLines}
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
              priceLines={signalPriceLines}
              height={chartHeight}
              visibleBars={320}
              activeIndicators={indicators.activeIndicators}
              volumeCompare={indicators.volumeCompare}
              showMA
              showInfoBar
            />
          )}
        </div>
      </div>
    </Modal>
  )
}
