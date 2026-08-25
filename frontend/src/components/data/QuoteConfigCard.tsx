import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Activity, Settings } from 'lucide-react'
import { Skeleton } from './Skeleton'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import { resolveQuoteDataState, quoteDataStateText, quoteSnapshotText } from '@/lib/api'

export function QuoteConfigCard({ enabled, running, isTrading, lastFetchMs, intervalS, intervalMin, intervalMax, loading, onToggle, toggling, showIntervalEdit, onShowIntervalEdit, onIntervalChange }: {
  enabled: boolean
  running: boolean
  isTrading: boolean
  lastFetchMs: number | null
  intervalS: number
  intervalMin: number
  intervalMax: number
  loading: boolean
  onToggle: (enabled: boolean) => void
  toggling: boolean
  showIntervalEdit: boolean
  onShowIntervalEdit: () => void
  onIntervalChange: (v: number) => void
}) {
  // 共享缓存 (QK.quoteStatus), 不产生额外请求; 取数据健康状态而非线程存活
  const { data: quoteStatus } = useQuoteStatus()
  const dataState = resolveQuoteDataState(quoteStatus)
  const snapshotText = quoteSnapshotText(quoteStatus?.source_as_of)
  const isReady = enabled && dataState === 'ready'
  const isLive = isReady && !snapshotText

  const statusColor = isLive
    ? 'bg-accent shadow-[0_0_6px_rgba(61,214,140,0.5)]'
    : enabled && dataState === 'error'
      ? 'bg-danger/70'
      : enabled && (running || (dataState !== null && dataState !== 'disabled'))
        ? 'bg-warning/60'
        : 'bg-muted'

  const statusText = !enabled
    ? '已关闭'
    : isLive
      ? '行情运行中'
      : isReady
        ? '轮询中，本地快照可用'
        : !isTrading
          ? '非交易时段'
          : dataState
            ? quoteDataStateText(dataState)
            : running
              ? '轮询中'
              : '已停止'

  const lastFetchTime = lastFetchMs
    ? new Date(lastFetchMs).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : null

  return (
    <div className="rounded-card border border-border bg-surface p-4 relative">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-secondary" />
          <h3 className="text-sm font-medium text-foreground">实时行情</h3>
        </div>
        <button
          onClick={() => onToggle(!enabled)}
          disabled={toggling}
          className={`relative inline-flex h-4 w-7 items-center rounded-full shrink-0 transition-colors duration-200 ${
            enabled
              ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]'
              : 'bg-elevated'
          } ${toggling ? 'opacity-50' : 'cursor-pointer'}`}
        >
          <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            enabled ? 'translate-x-[14px]' : 'translate-x-0.5'
          }`} />
        </button>
      </div>

      {loading ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between"><Skeleton w="w-8" /><Skeleton w="w-16" /></div>
          <div className="flex items-center justify-between"><Skeleton w="w-12" /><Skeleton w="w-20" /></div>
          <div className="flex items-center justify-between"><Skeleton w="w-10" /><Skeleton w="w-14" /></div>
          <div className="flex items-center justify-between"><Skeleton w="w-12" /><Skeleton w="w-12" /></div>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">状态</span>
            <div className="flex items-center gap-1.5">
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${statusColor} ${isLive ? 'animate-pulse' : ''}`} />
              <span className="font-mono text-secondary">{statusText}</span>
            </div>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">交易时段</span>
            <span className={`font-mono ${isTrading ? 'text-accent' : 'text-muted'}`}>{isTrading ? '交易中' : '休市'}</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <div className="flex items-center gap-1">
              <span className="text-muted">轮询间隔</span>
              <button
                onClick={() => onShowIntervalEdit()}
                className={`p-0.5 rounded hover:bg-elevated transition-colors ${showIntervalEdit ? 'text-accent' : 'text-secondary'}`}
                title="设置轮询间隔"
              >
                <Settings className="h-3 w-3" />
              </button>
            </div>
            <span className="font-mono text-secondary">{intervalS}s</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">最后获取</span>
            <span className="font-mono text-secondary">{lastFetchTime ?? '—'}</span>
          </div>
          {snapshotText && (
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-muted">数据来源</span>
              <span className="font-mono text-warning/80">{snapshotText}</span>
            </div>
          )}
        </div>
      )}

      <AnimatePresence>
        {showIntervalEdit && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <IntervalEditor
              min={intervalMin}
              max={intervalMax}
              value={intervalS}
              onChange={onIntervalChange}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function IntervalEditor({ min, max, value, onChange }: {
  min: number; max: number; value: number; onChange: (v: number) => void
}) {
  const [draft, setDraft] = useState(value)
  const clamped = Math.max(min, Math.min(max, draft))
  const step = min < 1 ? 0.1 : min < 3 ? 0.5 : 1
  const presets = min <= 3 ? [3, 5, 10, 30, 60] : [5, 10, 15, 30, 60]

  return (
    <div className="mt-2 pt-2 border-t border-border/50">
      <div className="text-[10px] text-muted mb-1.5">
        轮询间隔 <span className="text-muted/60">({min}s ~ {max}s)</span>
      </div>
      <div className="flex flex-wrap gap-1 mb-2">
        {presets.map(p => (
          <button
            key={p}
            onClick={() => { setDraft(p); onChange(p) }}
            className={`px-1.5 py-0.5 rounded text-[10px] font-mono transition-colors ${
              Math.abs(clamped - p) < 0.01
                ? 'bg-accent/15 text-accent border border-accent/30'
                : 'bg-elevated text-secondary hover:text-foreground border border-transparent'
            }`}
          >
            {p}s
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="range"
          min={min} max={max} step={step}
          value={clamped}
          onChange={e => { const v = parseFloat(e.target.value); setDraft(v); onChange(v) }}
          className="flex-1 h-1 accent-accent cursor-pointer"
        />
        <span className="text-[10px] font-mono text-foreground w-8 text-right">
          {clamped < 1 ? clamped.toFixed(1) : clamped.toFixed(0)}s
        </span>
      </div>
    </div>
  )
}
