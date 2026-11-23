/**
 * 看板展示组件。市场概览和个股详情回调由 Dashboard 页提供，
 * 避免各卡片重复查询行情或自行挂载详情。
 */
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ArrowDownRight, ArrowUpRight, Flame } from 'lucide-react'
import { api, type MarketSnapshotRow, type OverviewDimensionRankItem, type OverviewMarket, type AlertEvent } from '@/lib/api'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { dimensionKindForSourceField, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { cnSignal } from '@/lib/signals'
import { useCustomSignalNames } from '@/lib/useCustomSignalNames'
import { strategyEventMeta, strategyName } from '@/lib/strategyMonitorEvents'
import { boardTag } from '@/components/stock-table/primitives'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { fmtPrice, fmtIndexPct, fmtStockPct, pctClass, scoreColor as scoreColorValue, SectionTitle, StockNameButton } from './shared'

const RANK_ROWS = 5

// 切股导航列表构建 (与列表展示行一致: StockList 只显示前 8)
export function stockListNav(rows: MarketSnapshotRow[]): NavItem[] {
  return toNavItems(rows.slice(0, 8))
}
export function rankNav(rank?: OverviewMarket['concept_rank']): NavItem[] {
  const leaders = [...(rank?.leading ?? []).slice(0, RANK_ROWS), ...(rank?.lagging ?? []).slice(0, RANK_ROWS)]
    .map(r => r.leader)
    .filter((l): l is NonNullable<typeof l> & { symbol: string } => !!l?.symbol)
  return toNavItems(leaders)
}

// ===== 指数行情条 =====
export function IndexTicker({ item }: { item: OverviewMarket['indices'][number] }) {
  const pct = item.change_pct
  const isUp = (pct ?? 0) >= 0
  return (
    <Link
      to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
      title={`查看 ${item.name || item.symbol} 行情`}
      className="grid min-w-0 grid-cols-[1fr_auto] items-center gap-x-2 gap-y-0.5 rounded-lg border border-border bg-elevated/45 px-1.5 py-1 shadow-[0_1px_1px_hsl(var(--border)/0.3)] backdrop-blur-sm transition-all hover:border-accent/40 hover:bg-elevated hover:shadow-[0_2px_6px_hsl(var(--accent)/0.15)] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
    >
      <div className="truncate text-xs font-medium text-foreground">{item.name || item.symbol}</div>
      <div className={`font-mono text-xs font-semibold ${pctClass(pct)}`}>{fmtIndexPct(pct)}</div>
      <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
      <div className={`flex items-center gap-1 font-mono text-[11px] ${pctClass(pct)}`}>
        {isUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {fmtPrice(item.last_price)}
      </div>
    </Link>
  )
}

// ===== 涨跌广度 =====
export function BreadthBar({ data }: { data: OverviewMarket['breadth'] }) {
  const denom = Math.max(data.total, 1)
  const upW = data.up / denom * 100
  const downW = data.down / denom * 100
  const flatW = Math.max(0, 100 - upW - downW)
  return (
    <div className="space-y-2">
      {/* 与上方涨跌分布同向: 左跌(绿) 右涨(红) */}
      <div className="flex h-2.5 overflow-hidden rounded-full bg-elevated">
        <div className="bg-bear/85" style={{ width: `${downW}%` }} />
        <div className="bg-muted/45" style={{ width: `${flatW}%` }} />
        <div className="bg-bull/85" style={{ width: `${upW}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div className="rounded bg-bear/8 px-2 py-1 text-bear">跌 <span className="font-mono">{data.down}</span></div>
        <div className="rounded bg-elevated/70 px-2 py-1 text-muted">平 <span className="font-mono">{data.flat}</span></div>
        <div className="rounded bg-bull/8 px-2 py-1 text-bull">涨 <span className="font-mono">{data.up}</span></div>
      </div>
    </div>
  )
}

// ===== 涨跌分布直方 =====
export function DistributionBars({ rows }: { rows: OverviewMarket['distribution'] }) {
  const maxCount = Math.max(...rows.map(r => r.count), 1)
  return (
    <div className="grid h-24 grid-cols-8 items-end gap-1 pt-1">
      {rows.map((r, i) => {
        const positive = i >= 4
        return (
          <div key={r.label} className="flex h-full min-w-0 flex-col items-center justify-end gap-0.5">
            <div className="font-mono text-[9px] text-muted">{r.count || ''}</div>
            <div
              className={`w-2 rounded-full ${positive ? 'bg-gradient-to-t from-bull/45 to-bull/90' : 'bg-gradient-to-t from-bear/45 to-bear/90'}`}
              style={{ height: `${Math.max(4, r.count / maxCount * 86)}%` }}
              title={`${r.label}: ${r.count}只`}
            />
            <div className="truncate text-[9px] text-muted">{r.label}</div>
          </div>
        )
      })}
    </div>
  )
}

// ===== 情绪雷达 (手写 SVG) =====
export function EmotionRadar({ radar, score }: { radar: OverviewMarket['radar']; score: number }) {
  const size = 240
  const cx = size / 2
  const cy = size / 2
  const maxR = 78
  const color = scoreColorValue(score)
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
      <svg viewBox={`0 0 ${size} ${size}`} className="h-56 w-full">
        <defs>
          <radialGradient id="emotionRadarFill" cx="50%" cy="45%" r="70%">
            <stop offset="0%" stopColor={`${color}57`} />
            <stop offset="100%" stopColor={`${color}1f`} />
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

// ===== 涨停梯队 =====
export function LadderMini({ limit, onStockClick, activeSymbol }: {
  limit: OverviewMarket['limit']
  onStockClick: (symbol: string, name?: string, navList?: NavItem[]) => void
  activeSymbol?: string
}) {
  const tiers = limit.tiers.filter(t => t.boards >= 2).slice(0, 6)
  const openStock = (symbol: string, name?: string) => {
    const stocks = tiers.flatMap(tier => tier.stocks ?? []).filter(stock => stock.symbol)
    onStockClick(symbol, name, toNavItems(stocks))
  }
  return (
    <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto">
      <div className="flex items-center justify-between rounded bg-elevated/55 px-2 py-1.5 text-[11px]">
        <span className="text-muted">封板率</span>
        <span className="font-mono text-accent">{(limit.seal_rate ?? 0).toFixed(0)}%</span>
      </div>
      {tiers.length === 0 && <div className="rounded border border-dashed border-border py-5 text-center text-xs text-muted">暂无 2 板以上</div>}
      {tiers.map(t => (
        <div key={t.boards} className="rounded bg-elevated/35 px-2 py-1.5">
          <div className="grid grid-cols-[42px_1fr_auto] items-center gap-2">
            <span className={`font-mono text-sm font-bold ${t.boards >= 5 ? 'text-bull' : t.boards >= 3 ? 'text-accent' : 'text-secondary'}`}>{t.boards}板</span>
            <div className="h-1.5 overflow-hidden rounded-full bg-base">
              <div className="h-full rounded-full bg-bull/70" style={{ width: `${Math.min(100, t.count * 12)}%` }} />
            </div>
            <span className="font-mono text-xs text-foreground">{t.count}</span>
          </div>
          {!!t.stocks?.length && (
            <div className="mt-1 flex flex-wrap gap-x-2 pl-[50px]">
              {t.stocks.map(s => (
                <StockNameButton key={s.symbol} symbol={s.symbol} name={s.name}
                  onClick={openStock}
                  className={cn('min-h-5 max-w-full text-[10px]', s.symbol === activeSymbol ? 'text-accent' : 'text-secondary')}
                />
              ))}
              </div>
            )}
          </div>
      ))}
    </div>
  )
}

// ===== 四大榜单 =====
export function StockList({ title, rows, mode, onStockClick, activeSymbol }: {
  title: string; rows: MarketSnapshotRow[]; mode: 'gain' | 'loss' | 'amount' | 'active';
  onStockClick?: (symbol: string, name?: string) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col rounded-card border border-border bg-surface/80 p-1.5">
      <div className="mb-1 flex shrink-0 items-center justify-between">
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <span className="text-[9px] text-muted">TOP {Math.min(rows.length, 8)}</span>
      </div>
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
        {rows.slice(0, 8).map((r, idx) => {
          const name = r.name?.trim() || undefined
          const board = boardTag(r.symbol)
          const clickable = !!(onStockClick && r.symbol)
          const Row = clickable ? 'button' : 'div'
          return (
            <Row
              key={`${r.symbol}-${idx}`}
              className={cn(
                'grid w-full grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-1.5 rounded-md border px-1.5 py-1 text-left transition-colors',
                r.symbol === activeSymbol ? 'border-accent/30 bg-accent/10' : 'border-transparent bg-elevated/40',
                clickable && 'group hover:border-border hover:bg-elevated focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
              )}
              {...(clickable ? {
                type: 'button' as const,
                'aria-label': `查看 ${name || r.symbol} 行情`,
                'aria-haspopup': 'dialog' as const,
                title: r.symbol,
                onClick: () => onStockClick?.(r.symbol, name),
              } : {})}
            >
              <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
              <span className="min-w-0">
                <span className="flex items-center gap-1">
                  <span className="truncate text-[11px] text-foreground group-hover:text-accent">{name || r.symbol}</span>
                  {board && (
                    <span className={`inline-flex h-3 shrink-0 items-center justify-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>
                      {board.label}
                    </span>
                  )}
                </span>
                <span className="block truncate font-mono text-[9px] text-muted">{r.symbol}</span>
              </span>
              <span className="text-right">
                {mode === 'amount' ? (
                  <>
                    <span className="block font-mono text-[11px] text-foreground">{fmtBigNum(r.amount)}</span>
                    <span className={`block font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</span>
                  </>
                ) : mode === 'active' ? (
                  <>
                    <span className="block font-mono text-[11px] text-accent">{fmtPrice(r.turnover_rate, 1)}%</span>
                    <span className={`block font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</span>
                  </>
                ) : (
                  <>
                    <span className={`block font-mono text-[11px] font-semibold ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</span>
                    <span className="block font-mono text-[9px] text-muted">{fmtPrice(r.close)}</span>
                  </>
                )}
              </span>
            </Row>
          )
        })}
        {rows.length === 0 && <div className="py-5 text-center text-xs text-muted">暂无数据</div>}
      </div>
    </div>
  )
}

// ===== 概念/行业热度列 =====
export function RankColumn({ title, rows, tone, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rows: OverviewDimensionRankItem[]; tone: 'bull' | 'bear';
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <div className={`text-[10px] font-medium ${tone === 'bull' ? 'text-bull' : 'text-bear'}`}>{title}</div>
      {rows.slice(0, RANK_ROWS).map((r, idx) => {
        const sourceField = r.source_field
        const kind = sourceField ? dimensionKindForSourceField(sourceField) : null
        const target = sourceField && kind ? { kind, value: r.name, sourceField } : null
        const openDimension = target && onDimensionClick ? () => onDimensionClick(target) : undefined
        const leader = r.leader
        const board = leader?.symbol ? boardTag(leader.symbol) : null
        const isActive = r.leader?.symbol != null && r.leader.symbol === activeSymbol
        return (
          <div
            key={`${title}-${r.name}-${idx}`}
            onClick={openDimension && (event => {
              if (!(event.target as Element).closest('button')) openDimension()
            })}
            className={cn(
              'grid grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-1 rounded-md border px-1.5 py-1 transition-colors',
              isActive ? 'border-accent/30 bg-accent/10' : 'border-transparent bg-elevated/40',
              openDimension && 'cursor-pointer hover:border-accent/40 hover:bg-elevated/70',
            )}
          >
            <span className="text-center font-mono text-[9px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              {openDimension ? (
                <button
                  type="button"
                  aria-label={`查看${r.name}成分股`}
                  aria-haspopup="dialog"
                  title={`查看${r.name}成分股`}
                  onClick={openDimension}
                  className="flex min-h-5 w-full items-center gap-0.5 rounded-sm text-left text-[11px] text-foreground hover:text-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                >
                  <span className="truncate">{r.name}</span>
                  <ArrowUpRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                </button>
              ) : <div className="truncate text-[11px] text-foreground" title={r.name}>{r.name}</div>}
              <div className="mt-0.5 flex min-w-0 items-center gap-1">
                <span className="shrink-0 font-mono text-[9px] text-muted">{r.count}只</span>
                <span className="text-muted">·</span>
                <StockNameButton symbol={leader?.symbol ?? ''} name={leader?.name}
                  onClick={onStockClick}
                  className="min-h-5 text-[10px] text-secondary"
                />
                {leader?.change_pct != null && (
                  <span className={`shrink-0 font-mono text-[9px] tabular-nums ${pctClass(leader.change_pct)}`}>
                    {fmtStockPct(leader.change_pct)}
                  </span>
                )}
                {board && (
                  <span className={`inline-flex h-3 shrink-0 items-center justify-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>
                    {board.label}
                  </span>
                )}
              </div>
            </div>
            <div className={`font-mono text-[10px] font-semibold ${pctClass(r.avg_pct)}`}>{fmtStockPct(r.avg_pct)}</div>
          </div>
        )
      })}
      {rows.length === 0 && <div className="rounded border border-dashed border-border py-4 text-center text-xs text-muted">暂无数据</div>}
    </div>
  )
}

// ===== 概念/行业热度卡 =====
export function HotRankCard({ title, rank, configUrl, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rank?: OverviewMarket['concept_rank']; configUrl?: string;
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  const hasData = (rank?.leading?.length ?? 0) > 0 || (rank?.lagging?.length ?? 0) > 0
  return (
    <section className="flex h-full min-h-0 flex-col rounded-card border border-border bg-surface/80 p-1.5">
      <SectionTitle icon={Flame} title={title} action={hasData && configUrl ? { to: configUrl, label: '板块详情' } : undefined} />
      {hasData ? (
        <div className="grid min-h-0 flex-1 grid-cols-2 items-start gap-2 overflow-y-auto">
          <RankColumn title="领涨" rows={rank?.leading ?? []} tone="bull" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
          <RankColumn title="领跌" rows={rank?.lagging ?? []} tone="bear" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
        </div>
      ) : (
        <div className="py-4 text-center">
          <p className="text-[11px] text-muted">{configUrl ? '未配置扩展数据源' : '暂无数据'}</p>
          {configUrl && (
            <Link to={configUrl} className="mt-1.5 inline-block text-[11px] text-accent hover:underline">
              前往配置
            </Link>
          )}
        </div>
      )}
    </section>
  )
}

// ===== 监控中心小组件 — 前 10 条触发记录 =====
const _SOURCE_BADGE: Record<string, string> = {
  strategy: 'bg-amber-400/10 text-amber-400',
  signal: 'bg-accent/10 text-accent',
  price: 'bg-emerald-400/10 text-emerald-400',
  market: 'bg-purple-500/10 text-purple-400',
  sector: 'bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
}
const _SOURCE_LABEL: Record<string, string> = {
  strategy: '策略', signal: '信号', price: '价格', market: '异动', sector: '板块',
}
const _SEVERITY_BAR: Record<string, string> = {
  info: 'bg-accent/40', warn: 'bg-warning', critical: 'bg-danger',
}

export function MonitorWidget({ onStockClick, activeSymbol, monitorHref }: {
  onStockClick: (event: AlertEvent, navList?: NavItem[]) => void
  activeSymbol?: string
  monitorHref?: string
}) {
  const customNames = useCustomSignalNames()
  const alerts = useQuery({
    queryKey: QK.alerts(),
    queryFn: () => api.alertsList({ days: 7, limit: 10 }),
    refetchInterval: 10000,
  })
  const events: AlertEvent[] = alerts.data?.alerts ?? []
  // 板块告警进入监控中心，不参与个股切换。
  const alertNav = toNavItems(events.filter((ev): ev is AlertEvent & { symbol: string } => ev.source !== 'sector' && !!ev.symbol))

  if (events.length === 0) {
    return (
      <div className="mt-1 py-6 text-center text-[11px] text-muted">暂无触发记录</div>
    )
  }

  return (
    <>
      <div className="mt-1 space-y-1.5">
        {events.map((ev, i) => {
          const sev = _SEVERITY_BAR[ev.severity ?? 'info'] ?? _SEVERITY_BAR.info
          const pct = ev.change_pct ?? 0
          const isStrategy = ev.source === 'strategy'
          const isSector = ev.source === 'sector'
          const clickable = isSector ? !!monitorHref : !!ev.symbol
          const actionClass = 'absolute inset-0 z-[1] rounded-md focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent'
          const board = ev.symbol ? boardTag(ev.symbol) : null
          const sname = isStrategy ? strategyName(ev.message ?? '') : ''
          const eventMeta = strategyEventMeta(ev.type)
          return (
            <motion.div
              key={`${ev.ts}-${i}`}
              initial={{ opacity: 0, y: -8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={{ duration: 0.3, delay: Math.min(i * 0.03, 0.3) }}
              className={cn(
                'relative overflow-hidden rounded-md border py-1.5 pl-2.5 pr-2 transition-colors',
                ev.symbol && ev.symbol === activeSymbol ? 'border-accent/40 bg-accent/5' : 'border-border/40 bg-surface/60',
                clickable && 'hover:border-accent/40 hover:bg-elevated/60',
              )}
            >
              <div className={cn('absolute left-0 top-0 h-full w-0.5', sev)} />
              {isSector && monitorHref && (
                <Link to={monitorHref} className={actionClass} aria-label="在监控中心查看板块告警" title="在监控中心查看板块告警" />
              )}
              {!isSector && ev.symbol && (
                <button
                  type="button"
                  className={actionClass}
                  aria-haspopup="dialog"
                  aria-label={`查看 ${ev.name?.trim() || ev.symbol} 行情`}
                  title={`查看 ${ev.symbol} 行情`}
                  onClick={() => onStockClick(ev, alertNav)}
                />
              )}
              <div className="flex items-center gap-1.5">
                <span className="inline-flex min-w-0 items-center gap-1">
                  {ev.symbol && <span className="shrink-0 font-mono text-[10px] font-medium text-foreground/80">{ev.symbol.replace(/\.(SH|SZ|BJ)$/, '')}</span>}
                  {board && (
                    <span className={`inline-flex h-3 w-3 shrink-0 items-center justify-center rounded border text-[7px] font-bold leading-none ${board.color}`}>
                      {board.label}
                    </span>
                  )}
                  {ev.name && <span className="max-w-[5rem] truncate text-[10px] text-secondary">{ev.name}</span>}
                </span>
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
                <>
                  {ev.symbol ? (
                    <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
                      <span className={cn('shrink-0 text-[9px] font-medium', eventMeta.className)}>
                        {eventMeta.action}
                      </span>
                      {sname
                        ? <span className="truncate text-[9px] font-medium text-amber-400">「{sname}」</span>
                        : ev.message && <span className="truncate text-[9px] text-muted">{ev.message}</span>}
                      <span className="flex-1" />
                      <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                        {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                      </span>
                    </div>
                  ) : (
                    <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
                      <span className="truncate text-[9px] text-muted">{ev.message}</span>
                      <span className="flex-1" />
                      <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                        {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                      </span>
                    </div>
                  )}
                  {ev.signals && ev.signals.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {ev.signals.map(signal => (
                        <span key={signal} className="rounded bg-accent/8 px-1 py-px text-[8px] text-accent/80">{cnSignal(signal, customNames)}</span>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="mt-0.5 flex items-center gap-1.5">
                    <span className={cn('shrink-0 rounded px-1 py-px text-[8px] font-medium', _SOURCE_BADGE[ev.source] ?? 'bg-elevated text-muted')}>
                      {_SOURCE_LABEL[ev.source] ?? ev.source}
                    </span>
                    {ev.message && (
                      <span className="text-[9px] text-muted truncate flex-1">{ev.message}</span>
                    )}
                    <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                      {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                    </span>
                  </div>
                  {ev.signals && ev.signals.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {ev.signals.map((s, j) => (
                        <span key={j} className="rounded bg-accent/8 px-1 py-px text-[8px] text-accent/80">{cnSignal(s, customNames)}</span>
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
