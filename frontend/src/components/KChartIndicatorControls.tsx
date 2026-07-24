import { useCallback, useState, type ReactNode } from 'react'

import {
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'
import { storage } from '@/lib/storage'

const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

export interface KChartIndicatorState {
  activeIndicators: string[]
  volumeCompare: VolumeCompareConfig
  toggleIndicator: (key: string) => void
  updateVolumeCompare: (patch: Partial<VolumeCompareConfig>) => void
}

export function useKChartIndicatorControls(): KChartIndicatorState {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(current => (
      current.includes(key)
        ? current.filter(item => item !== key)
        : [...current, key]
    ))
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(current => {
      const next = normalizeVolumeCompare({ ...current, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  return {
    activeIndicators,
    volumeCompare,
    toggleIndicator,
    updateVolumeCompare,
  }
}

export function KChartIndicatorControls({
  state,
  className = 'flex flex-wrap items-center gap-1.5 px-1 pb-0.5',
  children,
}: {
  state: KChartIndicatorState
  className?: string
  children?: ReactNode
}) {
  const {
    activeIndicators,
    volumeCompare,
    toggleIndicator,
    updateVolumeCompare,
  } = state

  return (
    <div className={className}>
      {SUB_CHARTS.map(indicator => (
        <button
          key={indicator.key}
          type="button"
          aria-pressed={activeIndicators.includes(indicator.key)}
          onClick={() => toggleIndicator(indicator.key)}
          className={`cursor-pointer rounded px-2 py-0.5 font-mono text-[10px] transition-colors ${
            activeIndicators.includes(indicator.key)
              ? 'bg-accent/20 text-accent'
              : 'bg-elevated text-muted hover:text-secondary'
          }`}
        >
          {indicator.label}
        </button>
      ))}
      {OVERLAY_INDICATORS.map(indicator => (
        <button
          key={indicator.key}
          type="button"
          aria-pressed={activeIndicators.includes(indicator.key)}
          onClick={() => toggleIndicator(indicator.key)}
          className={`cursor-pointer rounded px-2 py-0.5 font-mono text-[10px] transition-colors ${
            activeIndicators.includes(indicator.key)
              ? 'bg-accent/20 text-accent'
              : 'bg-elevated text-muted hover:text-secondary'
          }`}
        >
          {indicator.label}
        </button>
      ))}
      {activeIndicators.includes('vol') && (
        <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
          <span className="text-[10px] text-muted">量比</span>
          <button
            type="button"
            role="switch"
            aria-checked={volumeCompare.enabled}
            aria-label="开启量能对比"
            title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
            onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
            className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
              volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
            }`}
          >
            <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
              volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
            }`} />
          </button>
          <select
            aria-label="量能对比周期"
            value={volumeCompare.days}
            disabled={!volumeCompare.enabled}
            onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
            className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
          >
            {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
              <option key={days} value={days}>前{days}日均量</option>
            ))}
          </select>
        </div>
      )}
      {children}
    </div>
  )
}
