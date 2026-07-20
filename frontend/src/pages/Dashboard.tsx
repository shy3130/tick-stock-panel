import { useState, useEffect, useRef, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Activity, ArrowDownRight, ArrowUpRight, BarChart3, BellRing, Database, Flame, Info, LineChart, Loader2, Play, RefreshCw, Sparkles, Timer, X } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { PageHeader } from '@/components/PageHeader'
import { Skeleton } from '@/components/data/Skeleton'
import { Modal } from '@/components/Modal'
import { api, type MarketSnapshotRow, type OverviewDimensionRankItem, type OverviewMarket, type AlertEvent } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { useDataStatus, useCapabilities, useSettings } from '@/lib/useSharedQueries'
import { SealedBadge } from '@/components/SealedBadge'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { STAGE_LABELS } from '@/components/data/ActiveJobCard'
import { cn } from '@/lib/cn'
import { cnSignal } from '@/lib/signals'
import { getNavIconMeta } from '@/lib/navRegistry'
import { boardTag } from '@/components/stock-table/primitives'

function n(v: number | null | undefined) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function scoreColor(v: number) {
  // A 股惯例: 强势=红, 弱式=绿
  if (v >= 70) return 'hsl(var(--emotion-hot))'
  if (v >= 55) return 'hsl(var(--emotion-warm))'
  if (v >= 45) return 'hsl(var(--emotion-neutral))'
  if (v >= 30) return 'hsl(var(--emotion-cool))'
  return 'hsl(var(--emotion-cold))'
}

function fmtPrice(v: number | null | undefined, digits = 2) {
  const x = n(v)
  return x == null ? '—' : x.toFixed(digits)
}

function fmtIndexPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${x.toFixed(2)}%`
}

function fmtStockPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(2)}%`
}

function pctClass(v: number | null | undefined) {
  const x = n(v)
  if (x == null || x === 0) return 'text-muted'
  return x > 0 ? 'text-bull' : 'text-bear'
}

function quoteAge(ms?: number | null) {
  if (ms == null) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${s % 60}s`
}

function compactCount(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  if (x >= 1000) return `${(x / 1000).toFixed(1)}k`
  return x.toFixed(0)
}

const PANEL_CLS = 'overflow-hidden rounded-card border border-border bg-surface/90'

function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="flex min-h-10 items-center justify-between gap-3 border-b border-border/70 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-btn bg-accent/10 text-accent">
          <Icon className="h-3.5 w-3.5" />
        </span>
        <h2 className="truncate text-xs font-semibold text-foreground">{title}</h2>
      </div>
      {hint && <div className="shrink-0 font-mono text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

function DashboardPanel({
  icon,
  title,
  hint,
  children,
  className,
  bodyClassName,
}: {
  icon: typeof Activity
  title: string
  hint?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={cn(PANEL_CLS, className)}>
      <SectionTitle icon={icon} title={title} hint={hint} />
      <div className={cn('p-3', bodyClassName)}>{children}</div>
    </section>
  )
}

// 看板监控中心小组件 — 显示前 10 条触发记录 + 更多按钮
const _SOURCE_BADGE: Record<string, string> = {
  strategy: 'bg-amber-400/10 text-amber-400',
  signal: 'bg-accent/10 text-accent',
  price: 'bg-emerald-400/10 text-emerald-400',
  market: 'bg-purple-500/10 text-purple-400',
}
const _SOURCE_LABEL: Record<string, string> = {
  strategy: '策略', signal: '信号', price: '价格', market: '异动',
}
const _SEVERITY_BAR: Record<string, string> = {
  info: 'bg-accent/40', warn: 'bg-warning', critical: 'bg-danger',
}

function MonitorWidget({ onStockClick }: { onStockClick: (event: AlertEvent) => void }) {
  const reduceMotion = useReducedMotion()
  const alerts = useQuery({
    queryKey: ['alerts', ''],
    queryFn: () => api.alertsList({ days: 7, limit: 10 }),
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  })
  const events: AlertEvent[] = alerts.data?.alerts ?? []
  const visibleEvents = events.filter((ev: AlertEvent) => !(ev.source === 'strategy' && !ev.symbol))

  if (alerts.isLoading) {
    return (
      <div className="space-y-2 py-1" role="status" aria-label="正在加载监控记录">
        {[0, 1, 2].map(i => (
          <div key={i} className="space-y-1.5 border-b border-border/60 pb-2 last:border-b-0">
            <Skeleton h="h-3" w="w-2/3" className="motion-reduce:animate-none" />
            <Skeleton h="h-2.5" w="w-full" className="motion-reduce:animate-none" />
          </div>
        ))}
      </div>
    )
  }

  if (alerts.isError && !alerts.data) {
    return (
      <div className="py-5 text-center" role="alert">
        <div className="text-[11px] text-danger">监控记录加载失败</div>
        <button
          type="button"
          onClick={() => alerts.refetch()}
          className="mt-2 rounded-btn px-2 py-1 text-[11px] text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          重试
        </button>
      </div>
    )
  }

  if (visibleEvents.length === 0) {
    return (
      <div className="py-7 text-center text-[11px] text-muted">暂无触发记录</div>
    )
  }

  return (
    <>
      <div>
        {alerts.isError && (
          <div className="border-b border-warning/20 bg-warning/[0.06] px-3 py-1.5 text-[10px] text-warning" role="status">
            更新失败，当前显示上次获取的记录
          </div>
        )}
        {visibleEvents.map((ev, i) => {
          const sev = _SEVERITY_BAR[ev.severity ?? 'info'] ?? _SEVERITY_BAR.info
          const pct = ev.change_pct ?? 0
          const isStrategy = ev.source === 'strategy'
          const sm = isStrategy ? ev.message?.match(/策略「([^」]+)」/) : null
          const sname = sm ? sm[1] : ''
          const isNew = ev.type === 'new_entry'
          return (
            <motion.div
              key={`${ev.ts}-${i}`}
              initial={reduceMotion ? false : { opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={reduceMotion ? { duration: 0 } : { duration: 0.18, delay: Math.min(i * 0.025, 0.2) }}
              className="relative border-b border-border/60 py-2 pl-3 pr-1 transition-colors last:border-b-0 hover:bg-elevated/35"
            >
              <div className={cn('absolute left-0 top-0 h-full w-0.5', sev)} />
              {/* 第一行: 代码 + 名称 + 价格 + 涨跌幅 (点击代码/名称弹日K) */}
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => ev.symbol && onStockClick(ev)}
                  title={ev.symbol ? `查看 ${ev.symbol} 日K` : undefined}
                  disabled={!ev.symbol}
                  aria-label={ev.symbol ? `查看 ${ev.name || ev.symbol} 日K` : undefined}
                  className="-mx-0.5 inline-flex min-w-0 shrink-0 cursor-pointer items-center gap-1 rounded-sm px-0.5 transition-colors hover:bg-elevated/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:hover:bg-transparent"
                >
                  <span className="font-mono text-[10px] font-medium text-foreground/80 hover:text-accent">{ev.symbol?.replace(/\.(SH|SZ|BJ)$/, '')}</span>
                  {ev.symbol && (() => {
                    const board = boardTag(ev.symbol)
                    return board && (
                      <span className={`inline-flex items-center justify-center h-3 w-3 rounded text-[7px] font-bold leading-none border ${board.color}`}>
                        {board.label}
                      </span>
                    )
                  })()}
                  {ev.name && <span className="text-[10px] text-secondary truncate max-w-[5rem] hover:text-foreground">{ev.name}</span>}
                </button>
                <span className="flex-1" />
                {ev.price != null && (
                  <span className="text-[10px] font-mono text-foreground/60 shrink-0">{fmtPrice(ev.price)}</span>
                )}
                {ev.change_pct != null && (
                  <span className={cn('text-[10px] font-mono font-medium shrink-0 w-12 text-right', pct >= 0 ? 'text-danger' : 'text-bear')}>
                    {fmtPct(pct)}
                  </span>
                )}
              </div>
              {/* 第二行: 策略类型走新格式, 其他走旧格式 */}
              {isStrategy ? (
                <div className="mt-0.5 flex items-center gap-1.5">
                  <span className={cn('text-[10px] font-medium', isNew ? 'text-danger' : 'text-emerald-400')}>
                    {isNew ? '进入' : '移出'}
                  </span>
                  <span className="text-[10px] text-muted">策略</span>
                  <span className="text-[10px] font-medium text-amber-400">「{sname}」</span>
                  <span className="flex-1" />
                  <span className="shrink-0 font-mono text-[10px] text-muted">
                    {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                  </span>
                </div>
              ) : (
                <>
                  <div className="mt-0.5 flex items-center gap-1.5">
                    <span className={cn('shrink-0 rounded-sm px-1 py-px text-[10px] font-medium', _SOURCE_BADGE[ev.source] ?? 'bg-elevated text-muted')}>
                      {_SOURCE_LABEL[ev.source] ?? ev.source}
                    </span>
                    {ev.message && (
                      <span className="flex-1 truncate text-[10px] text-muted">{ev.message}</span>
                    )}
                    <span className="shrink-0 font-mono text-[10px] text-muted">
                      {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                    </span>
                  </div>
                  {ev.signals && ev.signals.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {ev.signals.map((s, j) => (
                        <span key={j} className="rounded-sm bg-accent/[0.08] px-1 py-px text-[10px] text-accent">{cnSignal(s)}</span>
                      ))}
                    </div>
                  )}
                </>
              )}
            </motion.div>
          )
        })}
      </div>
    </>
  )
}

function KpiCell({ label, value, sub, tone = 'neutral' }: { label: ReactNode; value: ReactNode; sub?: string; tone?: 'bull' | 'bear' | 'accent' | 'neutral' }) {
  const isPlain = typeof value === 'string' || typeof value === 'number'
  const color = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : tone === 'accent' ? 'text-accent' : 'text-foreground'
  return (
    <div className="min-w-0 bg-surface px-3 py-2.5 transition-colors hover:bg-elevated/35">
      <div className="flex min-h-4 items-center gap-1 text-[11px] font-medium text-secondary">{label}</div>
      <div className={`mt-1.5 whitespace-nowrap font-mono text-lg font-semibold leading-none tabular-nums ${isPlain ? color : 'text-foreground'}`}>{value}</div>
      {sub && <div className="mt-1.5 truncate text-[11px] text-muted" title={sub}>{sub}</div>}
    </div>
  )
}

function IndexTicker({ item }: { item: OverviewMarket['indices'][number] }) {
  const pct = item.change_pct
  const isUp = (n(pct) ?? 0) >= 0
  return (
    <Link
      to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
      className="group grid min-w-0 grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1 bg-surface px-3 py-2.5 transition-colors hover:bg-elevated/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
    >
      <div className="truncate text-xs font-semibold text-foreground">{item.name || item.symbol}</div>
      <div className={`font-mono text-sm font-semibold tabular-nums ${pctClass(pct)}`}>{fmtIndexPct(pct)}</div>
      <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
      <div className={`flex items-center gap-1 font-mono text-[11px] tabular-nums ${pctClass(pct)}`}>
        {isUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {fmtPrice(item.last_price)}
      </div>
    </Link>
  )
}

function BreadthBar({ data }: { data: OverviewMarket['breadth'] }) {
  const denom = Math.max(data.total, 1)
  const upW = data.up / denom * 100
  const downW = data.down / denom * 100
  const flatW = Math.max(0, 100 - upW - downW)
  return (
    <div className="space-y-2.5">
      <div className="flex h-2 overflow-hidden rounded-full bg-elevated" aria-hidden="true">
        <div className="bg-bull/85" style={{ width: `${upW}%` }} />
        <div className="bg-muted/45" style={{ width: `${flatW}%` }} />
        <div className="bg-bear/85" style={{ width: `${downW}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-2 text-[11px]">
        <div className="text-bull">涨 <span className="font-mono font-semibold tabular-nums">{data.up}</span></div>
        <div className="text-center text-muted">平 <span className="font-mono font-semibold tabular-nums">{data.flat}</span></div>
        <div className="text-right text-bear">跌 <span className="font-mono font-semibold tabular-nums">{data.down}</span></div>
      </div>
    </div>
  )
}

function DistributionBars({ rows }: { rows: OverviewMarket['distribution'] }) {
  const maxCount = Math.max(...rows.map(r => r.count), 1)
  return (
    <div
      className="grid h-28 grid-cols-8 items-end gap-1 border-b border-border/70 pt-1"
      role="img"
      aria-label={`涨跌分布：${rows.map(r => `${r.label} ${r.count}只`).join('，')}`}
    >
      {rows.map((r, i) => {
        const positive = i >= 4
        return (
          <div key={r.label} className="flex h-full min-w-0 flex-col items-center justify-end gap-1">
            <div className="font-mono text-[10px] text-muted">{r.count || ''}</div>
            <div
              className={`w-full max-w-3 rounded-t-sm ${positive ? 'bg-bull/80' : 'bg-bear/80'}`}
              style={{ height: `${Math.max(4, r.count / maxCount * 78)}%` }}
              title={`${r.label}: ${r.count}只`}
            />
            <div className="max-w-full truncate text-[9px] text-muted">{r.label}</div>
          </div>
        )
      })}
    </div>
  )
}

function EmotionRadar({ radar, score }: { radar: OverviewMarket['radar']; score: number }) {
  const size = 240
  const cx = size / 2
  const cy = size / 2
  const maxR = 78
  const color = scoreColor(score)
  if (!radar.length) return <div className="flex h-52 items-center justify-center text-xs text-muted">暂无雷达数据</div>
  const points = radar.map((r, i) => {
    const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
    const radius = maxR * Math.max(0, Math.min(100, r.value)) / 100
    return {
      ...r,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      lx: cx + Math.cos(angle) * (maxR + 27),
      ly: cy + Math.sin(angle) * (maxR + 27),
      gx: cx + Math.cos(angle) * maxR,
      gy: cy + Math.sin(angle) * maxR,
    }
  })
  const polygon = points.map(p => `${p.x},${p.y}`).join(' ')
  const gridPolygons = [1, 0.66, 0.33].map((level, idx) => ({
    level,
    idx,
    points: radar.map((_, i) => {
      const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
      return `${cx + Math.cos(angle) * maxR * level},${cy + Math.sin(angle) * maxR * level}`
    }).join(' '),
  }))
  return (
    <div className="flex justify-center">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="h-56 w-full"
        role="img"
        aria-labelledby="emotion-radar-title emotion-radar-desc"
      >
        <title id="emotion-radar-title">市场情绪雷达，评分 {score}</title>
        <desc id="emotion-radar-desc">{radar.map(item => `${item.label} ${item.value}`).join('，')}</desc>
        <defs>
          <radialGradient id="emotionRadarFill" cx="50%" cy="45%" r="70%">
            <stop offset="0%" stopColor={color} stopOpacity="0.34" />
            <stop offset="100%" stopColor={color} stopOpacity="0.12" />
          </radialGradient>
          {/* 中心/网格用 CSS 变量取色, 亮暗主题自动切换 (SVG 属性支持 hsl(var(--x))) */}
          <radialGradient id="emotionRadarCenter" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor="hsl(var(--surface) / 0.92)" />
            <stop offset="68%" stopColor="hsl(var(--surface) / 0.70)" />
            <stop offset="100%" stopColor="hsl(var(--surface) / 0)" />
          </radialGradient>
        </defs>
        {gridPolygons.map(g => (
          <polygon
            key={g.level}
            points={g.points}
            fill={g.idx % 2 === 0 ? 'hsl(var(--elevated) / 0.55)' : 'hsl(var(--elevated) / 0.3)'}
            stroke={g.level === 1 ? 'hsl(var(--border) / 0.9)' : 'hsl(var(--border) / 0.5)'}
            strokeWidth={g.level === 1 ? 1.2 : 0.8}
          />
        ))}
        {points.map(p => <line key={p.key} x1={cx} y1={cy} x2={p.gx} y2={p.gy} stroke="hsl(var(--border) / 0.4)" />)}
        <polygon points={polygon} fill="url(#emotionRadarFill)" stroke={color} strokeWidth="2" />
        {points.map(p => <circle key={p.key} cx={p.x} cy={p.y} r="2.8" fill={color} stroke="hsl(var(--surface) / 0.9)" strokeWidth="1" />)}
        <circle cx={cx} cy={cy} r="29" fill="url(#emotionRadarCenter)" />
        <text x={cx} y={cy + 7} textAnchor="middle" className="fill-foreground font-mono text-[24px] font-bold">{score}</text>
        {points.map(p => (
          <text key={`${p.key}-label`} x={p.lx} y={p.ly + 4} textAnchor="middle" className="fill-secondary text-[10px] font-medium">{p.label}</text>
        ))}
      </svg>
    </div>
  )
}

function LadderMini({ limit }: { limit: OverviewMarket['limit'] }) {
  const tiers = limit.tiers.filter(t => t.boards >= 2).slice(0, 6)
  return (
    <div>
      <div className="flex items-end justify-between border-b border-border/60 pb-2.5">
        <div>
          <div className="text-[11px] text-muted">封板率</div>
          <div className="mt-1 font-mono text-xl font-semibold leading-none text-accent tabular-nums">{(limit.seal_rate ?? 0).toFixed(0)}%</div>
        </div>
        <div className="text-right">
          <div className="text-[11px] text-muted">梯队数量</div>
          <div className="mt-1 font-mono text-sm font-semibold text-foreground tabular-nums">{limit.tiers.length}</div>
        </div>
      </div>
      {tiers.length === 0 && <div className="py-7 text-center text-xs text-muted">暂无 2 板以上</div>}
      <div className="divide-y divide-border/60">
        {tiers.map(t => {
          const stocks = t.stocks ?? []
          const showStocks = stocks.length > 0 && stocks.length <= 3
          return (
            <div key={t.boards} className="py-2">
              <div className="grid grid-cols-[42px_1fr_auto] items-center gap-3">
                <span className={`font-mono text-sm font-bold ${t.boards >= 5 ? 'text-bull' : t.boards >= 3 ? 'text-accent' : 'text-secondary'}`}>{t.boards}板</span>
                <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
                  <div className="h-full rounded-full bg-bull/70" style={{ width: `${Math.min(100, t.count * 12)}%` }} />
                </div>
                <span className="font-mono text-xs font-semibold text-foreground tabular-nums">{t.count}</span>
              </div>
              {showStocks && (
                <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 pl-[54px] text-[10px] text-secondary">
                  {stocks.map(stock => <span key={stock.symbol}>{stock.name || stock.symbol}</span>)}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function MiniMetric({ label, value, cls = 'text-foreground' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="min-w-0 bg-surface px-2.5 py-2">
      <div className="truncate text-[11px] text-muted" title={label}>{label}</div>
      <div className={`mt-1 truncate font-mono text-sm font-semibold tabular-nums ${cls}`} title={value}>{value}</div>
    </div>
  )
}

function StockList({ title, rows, mode, onStockClick }: {
  title: string; rows: MarketSnapshotRow[]; mode: 'gain' | 'loss' | 'amount' | 'active';
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  return (
    <div className="min-w-0 bg-surface">
      <div className="flex min-h-9 items-center justify-between border-b border-border/60 px-3 py-2">
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <span className="font-mono text-[10px] text-muted">TOP {Math.min(rows.length, 8)}</span>
      </div>
      <div className="divide-y divide-border/50">
        {rows.slice(0, 8).map((r, idx) => (
          <button
            type="button"
            key={`${r.symbol}-${idx}`}
            className="grid min-h-10 w-full grid-cols-[20px_1fr_auto] items-center gap-2 px-3 py-1.5 text-left transition-colors hover:bg-elevated/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
            onClick={() => onStockClick?.(r.symbol, r.name ?? undefined)}
            aria-label={`查看 ${r.name || r.symbol} ${r.symbol}`}
          >
            <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-1">
                <span className="truncate text-xs font-medium text-foreground">{r.name || r.symbol}</span>
                {(() => {
                  const board = boardTag(r.symbol)
                  return board ? (
                    <span className={`inline-flex h-3 shrink-0 items-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
              </div>
              <div className="font-mono text-[10px] text-muted">{r.symbol}</div>
            </div>
            <div className="text-right">
              {mode === 'amount' ? (
                <>
                  <div className="font-mono text-xs text-foreground tabular-nums">{fmtBigNum(r.amount)}</div>
                  <div className={`font-mono text-[10px] tabular-nums ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : mode === 'active' ? (
                <>
                  <div className="font-mono text-xs text-accent tabular-nums">{fmtPrice(r.turnover_rate, 1)}%</div>
                  <div className={`font-mono text-[10px] tabular-nums ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : (
                <>
                  <div className={`font-mono text-xs font-semibold tabular-nums ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                  <div className="font-mono text-[10px] text-muted tabular-nums">{fmtPrice(r.close)}</div>
                </>
              )}
            </div>
          </button>
        ))}
        {rows.length === 0 && <div className="py-7 text-center text-xs text-muted">暂无数据</div>}
      </div>
    </div>
  )
}

function RankColumn({ title, rows, tone, onStockClick }: {
  title: string; rows: OverviewDimensionRankItem[]; tone: 'bull' | 'bear';
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  return (
    <div className="min-w-0 bg-surface px-3 py-2">
      <div className={`pb-1.5 text-[11px] font-semibold ${tone === 'bull' ? 'text-bull' : 'text-bear'}`}>{title}</div>
      <div className="divide-y divide-border/50">
        {rows.slice(0, 5).map((r, idx) => (
          <div key={`${title}-${r.name}-${idx}`} className="grid min-h-10 grid-cols-[18px_1fr_auto] items-center gap-2 py-1.5">
            <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              <div className="truncate text-xs font-medium text-foreground" title={r.name}>{r.name}</div>
              <div className="flex min-w-0 items-center gap-1 text-[10px] text-muted">
                <span className="shrink-0">{r.count}只 ·</span>
                {r.leader?.symbol ? (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); onStockClick?.(r.leader!.symbol!, r.leader!.name ?? undefined) }}
                    className="truncate rounded-sm hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    title={r.leader?.symbol ?? undefined}
                    aria-label={`查看${title}股 ${r.leader?.name ?? r.leader.symbol}`}
                  >{r.leader?.name ?? '—'}</button>
                ) : <span className="truncate">{r.leader?.name ?? '—'}</span>}
                {r.leader?.symbol && (() => {
                  const board = boardTag(r.leader.symbol)
                  return board ? (
                    <span className={`inline-flex h-3 shrink-0 items-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
              </div>
            </div>
            <div className={`font-mono text-xs font-semibold tabular-nums ${pctClass(r.avg_pct)}`}>{fmtStockPct(r.avg_pct)}</div>
          </div>
        ))}
      </div>
      {rows.length === 0 && <div className="py-5 text-center text-xs text-muted">暂无数据</div>}
    </div>
  )
}

function HotRankCard({ title, rank, configUrl, onStockClick }: {
  title: string; rank?: OverviewMarket['concept_rank']; configUrl: string;
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  const hasData = (rank?.leading?.length ?? 0) > 0 || (rank?.lagging?.length ?? 0) > 0
  return (
    <DashboardPanel
      icon={Flame}
      title={title}
      hint={
        <Link
          to={configUrl}
          className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          title={`进入${title}`}
          aria-label={`进入${title}`}
        >
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      }
      bodyClassName={hasData ? 'p-0' : undefined}
    >
      {hasData ? (
        <div className="grid grid-cols-1 gap-px bg-border/60 sm:grid-cols-2">
          <RankColumn title="领涨" rows={rank?.leading ?? []} tone="bull" onStockClick={onStockClick} />
          <RankColumn title="领跌" rows={rank?.lagging ?? []} tone="bear" onStockClick={onStockClick} />
        </div>
      ) : (
        <div className="py-4 text-center">
          <p className="text-[11px] text-muted">未配置扩展数据源</p>
          <Link
            to={configUrl}
            className="mt-1.5 inline-block text-[11px] text-accent transition-colors hover:text-accent"
          >
            前往配置 →
          </Link>
        </div>
      )}
    </DashboardPanel>
  )
}

function DashboardSkeleton() {
  return (
    <div
      className="mx-auto max-w-[1680px] space-y-3 p-3 sm:p-4"
      role="status"
      aria-live="polite"
      aria-label="正在加载市场看板"
    >
      <div className="grid grid-cols-1 gap-px overflow-hidden rounded-card border border-border bg-border sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="space-y-2 bg-surface p-3">
            <Skeleton h="h-3" w="w-2/5" className="motion-reduce:animate-none" />
            <Skeleton h="h-5" w="w-3/5" className="motion-reduce:animate-none" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border md:grid-cols-3 xl:grid-cols-6">
        {[0, 1, 2, 3, 4, 5].map(i => (
          <div key={i} className="space-y-2 bg-surface p-3">
            <Skeleton h="h-3" w="w-1/2" className="motion-reduce:animate-none" />
            <Skeleton h="h-5" w="w-2/3" className="motion-reduce:animate-none" />
            <Skeleton h="h-2.5" w="w-4/5" className="motion-reduce:animate-none" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          {[0, 1, 2].map(i => (
            <div key={i} className={PANEL_CLS}>
              <div className="flex min-h-10 items-center gap-2 border-b border-border/70 px-3">
                <Skeleton h="h-6" w="w-6" rounded="rounded-btn" className="motion-reduce:animate-none" />
                <Skeleton h="h-3" w="w-24" className="motion-reduce:animate-none" />
              </div>
              <div className="p-3">
                <Skeleton h="h-56" rounded="rounded-btn" className="motion-reduce:animate-none" />
              </div>
            </div>
          ))}
        </div>
        <div className={PANEL_CLS}>
          <div className="flex min-h-10 items-center gap-2 border-b border-border/70 px-3">
            <Skeleton h="h-6" w="w-6" rounded="rounded-btn" className="motion-reduce:animate-none" />
            <Skeleton h="h-3" w="w-20" className="motion-reduce:animate-none" />
          </div>
          <div className="space-y-3 p-3">
            <Skeleton h="h-8" className="motion-reduce:animate-none" />
            <Skeleton h="h-8" className="motion-reduce:animate-none" />
            <Skeleton h="h-8" className="motion-reduce:animate-none" />
          </div>
        </div>
      </div>
      <span className="sr-only">正在加载市场看板</span>
    </div>
  )
}

export function Dashboard() {
  const qc = useQueryClient()
  const [selectedDate, setSelectedDate] = useState<string | undefined>()
  const [manualFetching, setManualFetching] = useState(false)
  const [previewStock, setPreviewStock] = useState<{symbol: string; name?: string; alert?: AlertEvent} | null>(null)
  // 首次使用(无数据 + 未完成引导)自动弹窗: 同一会话只弹一次
  const [showWelcomeModal, setShowWelcomeModal] = useState(false)
  const dataStatus = useDataStatus({ staleTime: 60_000 })
  const overview = useQuery({
    queryKey: QK.overviewMarket(selectedDate),
    queryFn: () => api.overviewMarket(selectedDate),
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  })
  const data = overview.data
  const caps = useCapabilities()
  const settings = useSettings()
  const hasDepth = !!caps.data?.capabilities?.['depth5.batch']
  const sealedReady = !!data?.limit?.sealed_ready
  // none 档(无 key / 无效 key): 不再阻断功能, 仅实时行情等扩展能力受限
  const isNoKey = settings.data?.mode === 'none'
  // 无本地数据(enriched/daily 都没有)→ 常驻引导卡片
  // 注: 后端 status 的 rows 为性能刻意返回 0, 用 trading_days 判断是否有数据
  const ds = dataStatus.data
  const hasNoData = !!ds
    && (ds.enriched?.trading_days ?? 0) === 0
    && (ds.daily?.trading_days ?? 0) === 0

  // ===== 盘后管道触发(看板内一键获取数据) =====
  const [fetchJobId, setFetchJobId] = useState<string | null>(null)
  const fetchStatus = useQuery({
    queryKey: QK.pipelineJob(fetchJobId ?? ''),
    queryFn: () => api.pipelineJob(fetchJobId!),
    enabled: !!fetchJobId,
    refetchInterval: (q: any) => {
      const j = q.state.data
      return j && (j.status === 'succeeded' || j.status === 'failed') ? false : 1_000
    },
  })
  const startFetch = useMutation({
    mutationFn: api.pipelineRun,
    onSuccess: ({ job_id }) => {
      setFetchJobId(job_id)
      void qc.invalidateQueries({ queryKey: QK.pipelineJob(job_id) })
    },
  })
  const statusTransportFailed = !!fetchJobId && fetchStatus.isError
  const isFetching = startFetch.isPending
    || (!statusTransportFailed && (
      fetchStatus.data?.status === 'running'
      || fetchStatus.data?.status === 'pending'
    ))
  const fetchFailed = fetchStatus.data?.status === 'failed'
  const fetchSucceeded = fetchStatus.data?.status === 'succeeded'
  const handleFetchStart = () => {
    if (startFetch.isPending || fetchStatus.isFetching) return
    if (statusTransportFailed) {
      void fetchStatus.refetch()
      return
    }
    startFetch.mutate()
  }

  // 首次使用且无数据 → 自动弹一次引导弹窗(同会话只弹一次)
  useEffect(() => {
    if (!hasNoData) return
    if (settings.data?.onboarding_completed === false) return  // 还在引导流程中,不重复弹
    if (sessionStorage.getItem('tf_welcome_shown')) return
    sessionStorage.setItem('tf_welcome_shown', '1')
    setShowWelcomeModal(true)
  }, [hasNoData, settings.data?.onboarding_completed])

  // 同步完成后刷新看板数据
  useEffect(() => {
    if (fetchSucceeded) {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.overviewMarket(undefined) })
    }
  }, [fetchSucceeded, qc])

  // 组件重新挂载时(从其他页面切回)恢复正在运行的同步任务进度。
  // 原因: fetchJobId 是组件内状态, 切走页面时组件卸载、状态丢失, 切回后进度卡片消失。
  // 修复: 挂载时若无本地数据且未跟踪任何 job, 查一次后端是否有 active job, 有则接管。
  const resumeTriedRef = useRef(false)
  useEffect(() => {
    if (resumeTriedRef.current) return
    if (!hasNoData) return
    if (fetchJobId) return
    resumeTriedRef.current = true
    api.pipelineJobs(1).then(({ active_id }) => {
      if (active_id) setFetchJobId(active_id)
    }).catch(() => { /* 查询失败不阻塞, 用户仍可手动点击获取 */ })
  }, [hasNoData, fetchJobId])

  // 手动刷新: 先重建后端 Polars 缓存(解决跨天残留), 再重新拉看板数据
  const handleRefresh = async () => {
    setManualFetching(true)
    try {
      try {
        await api.refreshCache()
        await qc.invalidateQueries({ queryKey: ['overview-market'], refetchType: 'none' })
      } finally {
        await overview.refetch()
      }
    } catch {
      // API 层已展示错误提示，这里只保证按钮状态复位。
    } finally {
      setManualFetching(false)
    }
  }

  if ((overview.isLoading && !data) || (dataStatus.isLoading && !dataStatus.data)) {
    return (
      <div className="dashboard-theme min-h-full bg-base">
        <PageHeader title="市场看板" {...getNavIconMeta('/')} />
        <DashboardSkeleton />
      </div>
    )
  }

  if (!data) {
    return (
      <div className="dashboard-theme min-h-full bg-base">
        <PageHeader title="市场看板" {...getNavIconMeta('/')} />
        <div className="mx-auto flex max-w-[1680px] items-center justify-center p-6 sm:min-h-80">
          <div className="w-full max-w-sm rounded-card border border-danger/25 bg-surface p-6 text-center" role="alert">
            <Activity className="mx-auto h-5 w-5 text-danger" />
            <div className="mt-3 text-sm font-medium text-foreground">看板加载失败</div>
            <div className="mt-1 text-xs text-muted">请检查服务状态后重试。</div>
            <button
              type="button"
              onClick={() => overview.refetch()}
              disabled={overview.isFetching}
              className="mt-4 inline-flex h-9 items-center justify-center gap-2 rounded-btn bg-foreground px-4 text-xs font-medium text-surface transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {overview.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {overview.isFetching ? '重试中' : '重试'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  const score = data.emotion?.score ?? 50
  const strongUp = data.breadth.strong_up ?? 0
  const strongDown = data.breadth.strong_down ?? 0
  const latestDate = dataStatus.data?.enriched?.latest_date ?? null
  const currentDate = selectedDate ?? data.as_of ?? ''
  const isHistorical = !!selectedDate && !!latestDate && selectedDate < latestDate
  const isEmptyHistoricalSnapshot = !!selectedDate
    && data.as_of === selectedDate
    && data.breadth.total === 0
    && !overview.isFetching
  const isSealedDegrade = !hasDepth || isHistorical || !sealedReady
  // 实时模式: none / watchlist / full_market。
  // watchlist (Free 档) 仅自选 ≤5 只实时, 看板呈现的大盘数据实为盘后快照, 需提示避免误读。
  const quoteMode = data.quote_status?.mode as ('none' | 'watchlist' | 'full_market') | undefined
  const isSwitchingDate = overview.isFetching && !!selectedDate && data.as_of !== selectedDate
  const quoteRunning = !selectedDate
    && quoteMode === 'full_market'
    && !!data.quote_status?.running
    && !data.quote_status?.paused
    && !!data.quote_status?.is_trading_hours
  const quoteStatusLabel = isSwitchingDate
    ? '切换中'
    : quoteRunning
      ? '全市场实时'
      : selectedDate
        ? '历史快照'
        : '盘后快照'
  const quoteStatusClass = quoteRunning
    ? 'border-accent/30 bg-accent/[0.08] text-accent'
    : isSwitchingDate
      ? 'border-warning/30 bg-warning/[0.08] text-warning'
      : 'border-border bg-elevated/55 text-secondary'

  return (
    <div className="dashboard-theme min-h-full bg-base">
      <PageHeader
        title="市场看板"
        {...getNavIconMeta('/')}
        subtitle={hasNoData
          ? '等待首次行情数据'
          : isSwitchingDate
            ? `正在加载 ${selectedDate}`
            : data.as_of ? `${data.as_of} 市场快照` : undefined}
        titleExtra={!hasNoData && (
          <span
            className="inline-flex h-7 items-center gap-1.5 rounded-btn border px-2 text-[11px] font-medium"
            style={{
              color: scoreColor(score),
              borderColor: `color-mix(in srgb, ${scoreColor(score)} 30%, transparent)`,
              background: `color-mix(in srgb, ${scoreColor(score)} 9%, transparent)`,
            }}
          >
            {data.emotion.label}
            <strong className="font-mono text-xs tabular-nums">{score}</strong>
          </span>
        )}
        rightClassName="overflow-visible"
        right={!hasNoData && (
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {currentDate ? (
              <DatePicker
                value={currentDate}
                onChange={(date) => setSelectedDate(date === latestDate ? undefined : date)}
                min={dataStatus.data?.enriched?.earliest_date ?? undefined}
                max={latestDate ?? undefined}
                className="w-32"
              />
            ) : (
              <span className="font-mono text-xs text-secondary">—</span>
            )}
            {selectedDate && (
              <button
                type="button"
                onClick={() => setSelectedDate(undefined)}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn border border-border bg-elevated text-secondary transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                title="返回最新行情"
                aria-label="返回最新行情"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
            <div
              className={cn('inline-flex h-8 items-center gap-1.5 rounded-btn border px-2.5 text-[11px] font-medium', quoteStatusClass)}
              role="status"
              aria-live="polite"
            >
              {isSwitchingDate
                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                : <Timer className="h-3.5 w-3.5" />}
              <span>{quoteStatusLabel}</span>
              {quoteRunning && (
                <span className="border-l border-current/25 pl-1.5 font-mono tabular-nums">
                  {quoteAge(data.quote_status?.quote_age_ms)}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => { void handleRefresh() }}
              disabled={manualFetching || isSwitchingDate}
              aria-busy={manualFetching}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-elevated px-2.5 text-[11px] font-medium text-secondary transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${manualFetching ? 'animate-spin' : ''}`} />
              {manualFetching ? '重载中' : '重载'}
            </button>
          </div>
        )}
      />

      <div className="mx-auto max-w-[1680px] p-3 sm:p-4">
        {hasNoData && (
          <FetchDataCard
            isFetching={isFetching}
            isStarting={startFetch.isPending || (statusTransportFailed && fetchStatus.isFetching)}
            fetchFailed={fetchFailed || startFetch.isError || statusTransportFailed}
            stage={fetchStatus.data?.stage}
            fetchPct={fetchStatus.data?.progress}
            onStart={handleFetchStart}
            isNoKey={isNoKey}
          />
        )}

        <AnimatePresence>
          {showWelcomeModal && (
            <WelcomeFetchModal
              isNoKey={isNoKey}
              onClose={() => setShowWelcomeModal(false)}
              onStart={() => {
                startFetch.mutate()
                setShowWelcomeModal(false)
              }}
            />
          )}
        </AnimatePresence>

        {!hasNoData && isEmptyHistoricalSnapshot && (
          <div className={cn(PANEL_CLS, 'flex min-h-64 items-center justify-center p-6 text-center')} role="status">
            <div className="max-w-sm">
              <Info className="mx-auto h-5 w-5 text-warning" />
              <div className="mt-3 text-sm font-medium text-foreground">该日期没有可用行情</div>
              <p className="mt-1 text-xs leading-relaxed text-muted">可能是休市日，或本地数据尚未覆盖该日期。</p>
              <button
                type="button"
                onClick={() => setSelectedDate(undefined)}
                className="mt-4 inline-flex h-9 items-center justify-center gap-1.5 rounded-btn bg-foreground px-4 text-xs font-medium text-surface transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <RefreshCw className="h-3.5 w-3.5" />返回最新行情
              </button>
            </div>
          </div>
        )}

        {!hasNoData && !isEmptyHistoricalSnapshot && (
          <>
            {quoteMode === 'watchlist' && (
              <div className="mb-3 flex items-start gap-2 rounded-card border border-warning/25 bg-warning/[0.045] px-3 py-2.5 text-[11px] leading-relaxed" role="note">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
                <div className="min-w-0 flex-1 text-secondary">
                  当前为「自选实时」模式。看板展示的是<strong className="mx-1 text-foreground">盘后快照</strong>，仅自选股（{data.quote_status?.watchlist_symbol_count ?? 0} 只）支持实时监控。
                  <span className="ml-1 text-accent">全市场实时需 Starter+</span>
                </div>
              </div>
            )}

            <div className="mb-3 grid grid-cols-1 gap-px overflow-hidden rounded-card border border-border bg-border sm:grid-cols-2 xl:grid-cols-4" aria-label="核心指数">
              {data.indices.map(item => <IndexTicker key={item.symbol} item={item} />)}
            </div>

            <div className="mb-3 grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border md:grid-cols-3 xl:grid-cols-6" aria-label="市场关键指标">
              <KpiCell label="个股涨 / 平 / 跌" value={<><span className="text-bull">{data.breadth.up}</span><span className="text-muted">/</span><span className="text-muted">{data.breadth.flat}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.down}</span></>} sub={`上涨率 ${data.breadth.up_pct.toFixed(1)}%`} />
              <KpiCell label="强势 / 弱势" value={<><span className="text-bull">{strongUp}</span><span className="text-muted">/</span><span className="text-bear">{strongDown}</span></>} sub="涨跌 ≥3%" />
              <KpiCell label={<span className="inline-flex items-center gap-1">涨停 / 跌停<SealedBadge degraded={isSealedDegrade} hasDepth={hasDepth} isHistorical={isHistorical} sealedReady={sealedReady} sealedCountsUp={{ real: data.limit.limit_up, fake: data.limit.fake_up ?? 0, pending: 0 }} sealedCountsDown={{ real: data.limit.limit_down, fake: data.limit.fake_down ?? 0, pending: 0 }} rawUp={data.limit.limit_up + (data.limit.fake_up ?? 0)} rawDown={data.limit.limit_down + (data.limit.fake_down ?? 0)} invalidateKeys={['overview-market', 'limit-ladder']} /></span>} value={<><span className="text-bull">{data.limit.limit_up}</span><span className="text-muted">/</span><span className="text-bear">{data.limit.limit_down}</span></>} sub={`封板率 ${(data.limit.seal_rate ?? 0).toFixed(0)}%`} />
              <KpiCell label="最高连板" value={`${data.limit.max_boards || 0}板`} sub={(() => {
                const top = data.limit.tiers.find(tier => tier.boards === data.limit.max_boards)
                const stocks = top?.stocks ?? []
                return stocks.length > 0 && stocks.length <= 3
                  ? stocks.map(stock => stock.name || stock.symbol).join(' · ')
                  : `梯队 ${data.limit.tiers.length}`
              })()} tone="accent" />
              <KpiCell label="成交额" value={fmtBigNum(data.amount.total)} sub={`均额 ${fmtBigNum(data.amount.avg)}`} />
              <KpiCell label="换手 / 量比" value={`${fmtPrice(data.activity.avg_turnover, 1)}% / ${fmtPrice(data.activity.vol_ratio, 2)}`} sub={`高换手 ${data.activity.high_turnover} · 放量占比 ${fmtPrice(data.activity.high_vol_ratio, 1)}%`} tone="accent" />
            </div>

            <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_18rem]">
              <div className="min-w-0 space-y-3">
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
                  <DashboardPanel icon={BarChart3} title="涨跌分布 / 广度" hint={`${data.breadth.total}只`}>
                    <DistributionBars rows={data.distribution} />
                    <div className="mt-3">
                      <BreadthBar data={data.breadth} />
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-px overflow-hidden rounded-btn bg-border/60">
                      <MiniMetric label="平均涨跌" value={fmtStockPct(data.breadth.avg_pct)} cls={pctClass(data.breadth.avg_pct)} />
                      <MiniMetric label="中位涨跌" value={fmtStockPct(data.breadth.median_pct)} cls={pctClass(data.breadth.median_pct)} />
                    </div>
                  </DashboardPanel>

                  <DashboardPanel
                    icon={Sparkles}
                    title="情绪雷达"
                    hint={`评分 ${score}`}
                    bodyClassName="px-2 py-1"
                  >
                    <EmotionRadar radar={data.radar} score={score} />
                  </DashboardPanel>

                  <DashboardPanel icon={LineChart} title="趋势与活跃度" hint="盘面结构" bodyClassName="p-0">
                    <div className="grid grid-cols-3 gap-px bg-border/60">
                      <MiniMetric label="站上MA5" value={`${data.trend.above_ma5_pct.toFixed(0)}%`} cls="text-accent" />
                      <MiniMetric label="站上MA20" value={`${data.trend.above_ma20_pct.toFixed(0)}%`} cls="text-accent" />
                      <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
                      <MiniMetric label="60日新高" value={compactCount(data.trend.new_high)} cls="text-bull" />
                      <MiniMetric label="60日新低" value={compactCount(data.trend.new_low)} cls="text-bear" />
                      <MiniMetric label="高低比" value={`${data.trend.new_high + data.trend.new_low > 0 ? Math.round(data.trend.new_high / (data.trend.new_high + data.trend.new_low) * 100) : 50}%`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
                    </div>
                    <div className="border-t border-border/70 px-3 py-2 text-[11px] font-semibold text-secondary">盘中观察</div>
                    <div className="grid grid-cols-3 gap-px bg-border/60">
                      <MiniMetric label="炸板" value={`${data.limit.broken ?? 0}`} cls="text-warning" />
                      <MiniMetric label="高换手数" value={`${data.activity.high_turnover}`} cls="text-accent" />
                      <MiniMetric label="放量占比" value={`${fmtPrice(data.activity.high_vol_ratio, 1)}%`} cls="text-accent" />
                    </div>
                  </DashboardPanel>
                </div>

                <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.12fr_0.88fr]">
                  <HotRankCard title="概念热度" rank={data.concept_rank} configUrl="/concept-analysis" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                  <HotRankCard title="行业热度" rank={data.industry_rank} configUrl="/industry-analysis" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                </div>

                <DashboardPanel icon={BarChart3} title="市场排行" hint="点击个股查看日K" bodyClassName="p-0">
                  <div className="grid grid-cols-1 gap-px bg-border/60 sm:grid-cols-2 2xl:grid-cols-4">
                    <StockList title="涨幅榜" rows={data.top_gainers} mode="gain" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                    <StockList title="跌幅榜" rows={data.top_losers} mode="loss" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                    <StockList title="成交额榜" rows={data.turnover_leaders} mode="amount" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                    <StockList title="活跃换手" rows={data.active_leaders} mode="active" onStockClick={(symbol, name) => setPreviewStock({symbol, name})} />
                  </div>
                </DashboardPanel>
              </div>

              <aside className="min-w-0 space-y-3">
                <DashboardPanel
                  icon={Flame}
                  title="涨停梯队"
                  hint={<span className="inline-flex items-center gap-1">{`涨停 ${data.limit.limit_up}`}{isSealedDegrade && <span className="rounded-sm bg-warning/10 px-1 py-0.5 text-[10px] text-warning">{isHistorical ? '历史' : hasDepth ? '未修正' : '降级'}</span>}</span>}
                >
                  <LadderMini limit={data.limit} />
                </DashboardPanel>
                <DashboardPanel
                  icon={BellRing}
                  title="监控中心"
                  hint={
                    <Link
                      to="/monitor"
                      className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      title="进入监控中心"
                      aria-label="进入监控中心"
                    >
                      <ArrowUpRight className="h-3.5 w-3.5" />
                    </Link>
                  }
                  bodyClassName="px-3 py-1"
                >
                  <MonitorWidget onStockClick={(event) => {
                    if (event.symbol) {
                      setPreviewStock({ symbol: event.symbol, name: event.name ?? undefined, alert: event })
                    }
                  }} />
                </DashboardPanel>
              </aside>
            </div>
          </>
        )}

        <StockPreviewDialog
          symbol={previewStock?.symbol ?? null}
          name={previewStock?.name}
          triggerInfo={previewStock?.alert ? {
            price: previewStock.alert.price ?? null,
            changePct: previewStock.alert.change_pct ?? null,
            ts: previewStock.alert.ts,
            signals: previewStock.alert.signals,
            message: previewStock.alert.message,
          } : null}
          onClose={() => setPreviewStock(null)}
        />
      </div>
    </div>
  )
}

// ===== 无数据常驻引导卡片: 一键触发盘后管道获取行情数据(无 Key 也可) =====
function FetchDataCard({
  isFetching, isStarting, fetchFailed, stage, fetchPct, onStart, isNoKey,
}: {
  isFetching: boolean
  isStarting: boolean
  fetchFailed: boolean
  stage?: string
  fetchPct?: number
  onStart: () => void
  isNoKey: boolean
}) {
  const reduceMotion = useReducedMotion()
  const stageText = stage ? (STAGE_LABELS[stage] ?? stage) : '正在同步行情数据…'
  return (
    <div className={cn(PANEL_CLS, 'p-4')}>
      <div className="flex items-start gap-3">
        <div className="shrink-0 rounded-btn bg-accent/10 p-2">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-foreground">当前暂无数据</div>
          <p className="mt-1 text-xs text-secondary leading-relaxed">
            首次使用需获取行情数据后才能查看看板。系统将从免费数据源拉取近 1 年全 A 股日K(约 5500 只),预计 1-3 分钟,期间可继续浏览其他页面。
          </p>
          {isNoKey && (
            <div className="mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed text-warning/90">
              <Info className="mt-0.5 h-3 w-3 shrink-0" />
              <p>无需 API Key,当前为 None 档即可获取历史日K,可制定策略+回测。配置免费 Key 可解锁实时行情监控能力。</p>
            </div>
          )}

          {isFetching ? (
            <div className="mt-3">
              <div className="flex items-center justify-between text-[11px] text-muted mb-1.5">
                <span className="inline-flex items-center gap-1.5">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {isStarting ? '正在启动同步任务…' : stageText}
                </span>
                <span className="font-mono tabular">
                  {typeof fetchPct === 'number' ? `${Math.round(fetchPct)}%` : ''}
                </span>
              </div>
              <div
                className="h-1.5 overflow-hidden rounded-full bg-elevated"
                role="progressbar"
                aria-label={stageText}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={typeof fetchPct === 'number' ? Math.round(fetchPct) : undefined}
              >
                <motion.div
                  className="h-full w-full origin-left rounded-full bg-accent"
                  initial={reduceMotion ? false : { scaleX: 0 }}
                  animate={{ scaleX: Math.max(0.02, Math.min(1, (fetchPct ?? 0) / 100)) }}
                  transition={reduceMotion ? { duration: 0 } : { duration: 0.25, ease: 'easeOut' }}
                />
              </div>
            </div>
          ) : fetchFailed ? (
            <div className="mt-3 flex items-center gap-2">
              <span className="text-xs text-danger">同步失败,请重试</span>
              <button
                type="button"
                onClick={onStart}
                disabled={isStarting}
                aria-busy={isStarting}
                className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-foreground px-3 text-xs font-medium text-surface transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {isStarting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                {isStarting ? '重试中' : '重新获取'}
              </button>
            </div>
          ) : (
            <div className="mt-3 flex items-center gap-3">
              <button
                type="button"
                onClick={onStart}
                className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-foreground px-4 text-xs font-medium text-surface transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <Play className="h-3.5 w-3.5" />立即获取数据
              </button>
              <Link
                to="/data"
                className="inline-flex h-9 items-center gap-0.5 rounded-btn px-2 text-xs text-secondary transition-colors hover:bg-elevated hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                前往数据页
                <ArrowUpRight className="h-3 w-3 self-center" />
              </Link>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ===== 首次使用自动弹窗: 询问用户后触发盘后管道 =====
function WelcomeFetchModal({
  isNoKey, onClose, onStart,
}: {
  isNoKey: boolean
  onClose: () => void
  onStart: () => void
}) {
  const reduceMotion = useReducedMotion()
  return (
    <Modal
      onClose={onClose}
      labelledBy="dashboard-welcome-title"
      panelClassName="mx-4 w-full max-w-md overflow-hidden rounded-dialog border border-border bg-surface"
    >
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h2 id="dashboard-welcome-title" className="text-sm font-medium text-foreground">欢迎首次使用 · 获取行情数据</h2>
        <button
          type="button"
          onClick={onClose}
          className="flex h-8 w-8 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          title="关闭"
          aria-label="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="p-5 text-center">
        <motion.div
          initial={reduceMotion ? false : { scale: 0.92, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={reduceMotion ? { duration: 0 } : { duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
          className="mx-auto w-fit rounded-card bg-accent/10 p-3.5"
        >
          <Sparkles className="h-7 w-7 text-accent" />
        </motion.div>
        <h3 className="mt-4 text-base font-semibold text-foreground">首次使用,需先获取行情数据</h3>
        <p className="mt-2 text-xs text-secondary leading-relaxed">
          系统将从免费数据源拉取近 1 年全 A 股日K(约 5500 只),预计 1-3 分钟。
          同步期间可继续浏览其他页面,完成后看板自动刷新。
        </p>
        {isNoKey && (
          <div className="mt-3 flex items-center justify-center gap-1.5 rounded-btn bg-elevated/60 px-3 py-2 text-[11px] leading-relaxed text-muted">
            <Info className="h-3 w-3 shrink-0" />
            当前无需 API Key,None 档即可获取历史日K数据。
          </div>
        )}
        <div className="mt-5 flex items-center justify-center gap-2.5">
          <button
            type="button"
            onClick={onClose}
            className="h-9 rounded-btn px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            稍后再说
          </button>
          <button
            type="button"
            onClick={onStart}
            className="inline-flex h-9 items-center gap-2 rounded-btn bg-foreground px-5 text-sm font-semibold text-surface transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Play className="h-4 w-4" />开始获取
          </button>
        </div>
      </div>
    </Modal>
  )
}
