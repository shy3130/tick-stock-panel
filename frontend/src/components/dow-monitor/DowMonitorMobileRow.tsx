import { Pause, Play, Trash2 } from 'lucide-react'
import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'

import { cn } from '@/lib/cn'

import { DowMonitorSparkline } from './DowMonitorSparkline'
import { KeyInterpretationCell } from './KeyInterpretationCell'
import type { KeyInterpretation } from './keyInterpretation'
import type { MonitorRowPresentation } from './monitorListPresentation'
import type { DowMonitorOverviewSymbol } from './types'


function numberText(value: number | null): string {
  return value == null ? '--' : value.toFixed(2)
}

function percentText(value: number | null): string {
  if (value == null) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function stop(event: MouseEvent<HTMLButtonElement>) {
  event.stopPropagation()
}

export function DowMonitorMobileRow({
  item,
  row,
  interpretation,
  selected,
  pendingToggle,
  pendingRemoval,
  auxiliaryAction,
  onSelect,
  onToggle,
  onRemove,
}: {
  item: DowMonitorOverviewSymbol
  row: MonitorRowPresentation
  interpretation: KeyInterpretation
  selected: boolean
  pendingToggle: boolean
  pendingRemoval: boolean
  auxiliaryAction?: ReactNode
  onSelect: (symbol: string) => void
  onToggle: (symbol: string, enabled: boolean) => void
  onRemove: (symbol: string) => void
}) {
  const selectFromKeyboard = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    onSelect(item.symbol)
  }
  const positive = (row.changePct ?? 0) >= 0

  return (
    <article
      data-testid={`dow-monitor-mobile-${item.symbol}`}
      aria-selected={selected}
      tabIndex={0}
      onClick={() => onSelect(item.symbol)}
      onKeyDown={selectFromKeyboard}
      className={cn(
        'cursor-pointer border-b border-border/70 px-3 py-3 outline-none last:border-b-0 focus-visible:bg-elevated',
        selected && 'bg-accent/8 shadow-[inset_3px_0_0_0_rgb(var(--color-accent))]',
        !item.enabled && 'opacity-55',
      )}
    >
      <div className="grid grid-cols-[minmax(0,1fr)_auto_88px] items-center gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-foreground">
            {item.name || item.symbol}
          </div>
          <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
        </div>
        <div className="whitespace-nowrap text-right font-mono">
          <div className="text-sm font-semibold text-foreground">
            {numberText(row.price)}
          </div>
          <div className={positive ? 'text-danger' : 'text-emerald-400'}>
            {percentText(row.changePct)}
          </div>
        </div>
        <DowMonitorSparkline symbol={item.symbol} values={row.sparkline} />
      </div>
      <div className="mt-2 min-w-0">
        <KeyInterpretationCell
          interpretation={interpretation}
          compact
          testId="key-interpretation-mobile"
        />
      </div>
      <div className="mt-2 flex min-w-0 items-center justify-between gap-2">
        <div className="min-w-0 flex-1">{auxiliaryAction}</div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            aria-label={`${item.enabled ? '暂停监控' : '恢复监控'} ${item.symbol}`}
            disabled={pendingToggle}
            onClick={(event) => {
              stop(event)
              onToggle(item.symbol, !item.enabled)
            }}
            className="rounded-btn p-1.5 text-muted hover:bg-base hover:text-secondary disabled:opacity-40"
          >
            {item.enabled
              ? <Pause className="h-4 w-4" />
              : <Play className="h-4 w-4" />}
          </button>
          <button
            type="button"
            aria-label={`移除 ${item.symbol}`}
            disabled={pendingRemoval}
            onClick={(event) => {
              stop(event)
              onRemove(item.symbol)
            }}
            className="rounded-btn p-1.5 text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-40"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>
    </article>
  )
}
