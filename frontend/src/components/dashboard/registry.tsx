/**
 * 看板组件注册表 — 每个可摆放组件的类型、标签、图标、尺寸约束与渲染。
 *
 * render(ctx, item?) 输出该组件的完整卡片(自带边框/标题, 与抽离前视觉一致);
 * DashboardGrid 负责定位/拖拽/缩放壳, 不感知具体组件内部。
 */
import type { CSSProperties } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, BarChart3, BellRing, Flame, Gauge, LineChart, Link2, Sparkles, Target, TrendingUp, TrendingDown, Coins, Repeat } from 'lucide-react'
import type { AlertEvent, OverviewMarket } from '@/lib/api'
import { fmtBigNum } from '@/lib/format'
import type { DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { SealedBadge } from '@/components/SealedBadge'
import { scoreColor, fmtPrice, fmtStockPct, pctClass, compactCount, SectionTitle, MiniMetric, KpiCell } from './shared'
import {
  IndexTicker, BreadthBar, DistributionBars, EmotionRadar, LadderMini, StockList, HotRankCard, MonitorWidget,
  stockListNav, rankNav,
} from './widgets'
import type { DashboardItem, WidgetType } from './layout'
import { ExternalLinkWidget } from './ExternalLinkWidget'

/** 渲染上下文: 数据切片与交互回调(由 Dashboard 页提供, 组件不自取数据) */
export interface WidgetCtx {
  data: OverviewMarket
  score: number
  hasDepth: boolean
  sealedReady: boolean
  isSealedDegrade: boolean
  /** 来源榜当前选中的 symbol (StockPreviewDialog 高亮), source 如 'gain' | 'concept' | 'alert' */
  activeSymbol: (source: string) => string | undefined
  openStock: (source: string, symbol: string, name?: string, navList?: { symbol: string; name?: string }[]) => void
  openDimension: (t: DimensionMembersTarget) => void
  openAlert: (ev: AlertEvent, navList?: { symbol: string; name?: string }[]) => void
}

export interface WidgetDef {
  id: WidgetType
  label: string
  icon: typeof Flame
  minW: number
  minH: number
  /** 默认尺寸(添加新实例时), 布局默认坐标在 DEFAULT_LAYOUT */
  defW: number
  defH: number
  render: (ctx: WidgetCtx, item: DashboardItem) => ReactNode
}

function sectionCard(children: ReactNode, opts: { className?: string; style?: CSSProperties } = {}): ReactNode {
  return (
    <section
      style={opts.style}
      className={`h-full overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)] ${opts.className ?? ''}`}
    >
      {children}
    </section>
  )
}

function KpiWidget({ ctx }: { ctx: WidgetCtx }) {
  const { data, hasDepth, isSealedDegrade, sealedReady } = ctx
  const strongUp = data.breadth.strong_up ?? 0
  const strongDown = data.breadth.strong_down ?? 0
  return (
    <div className="grid h-full grid-cols-3 gap-1 xl:grid-cols-6">
      <KpiCell label="个股涨 / 平 / 跌" value={<><span className="text-bull">{data.breadth.up}</span><span className="text-muted">/</span><span className="text-muted">{data.breadth.flat}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.down}</span></>} sub={`上涨率 ${data.breadth.up_pct.toFixed(1)}%`} />
      <KpiCell label="强势 / 弱势" value={<><span className="text-bull">{strongUp}</span><span className="text-muted">/</span><span className="text-bear">{strongDown}</span></>} sub="涨跌 ≥3%" />
      <KpiCell label={<span className="inline-flex items-center gap-1">涨停 / 跌停<SealedBadge degraded={isSealedDegrade} hasDepth={hasDepth} isHistorical={false} sealedReady={sealedReady} sealedCountsUp={{ real: data.limit.limit_up, fake: data.limit.fake_up ?? 0, pending: 0 }} sealedCountsDown={{ real: data.limit.limit_down, fake: data.limit.fake_down ?? 0, pending: 0 }} rawUp={data.limit.limit_up + (data.limit.fake_up ?? 0)} rawDown={data.limit.limit_down + (data.limit.fake_down ?? 0)} invalidateKeys={['overview-market', 'limit-ladder']} /></span>} value={<><span className="text-bull">{data.limit.limit_up}</span><span className="text-muted">/</span><span className="text-bear">{data.limit.limit_down}</span></>} sub={`封板率 ${(data.limit.seal_rate ?? 0).toFixed(0)}%`} />
      <KpiCell label="最高连板" value={`${data.limit.max_boards || 0}板`} sub={(() => {
        const top = data.limit.tiers.find(t => t.boards === data.limit.max_boards)
        const stocks = top?.stocks ?? []
        if (stocks.length > 0 && stocks.length <= 3) return stocks.map(s => s.name || s.symbol).join(' · ')
        return `梯队 ${data.limit.tiers.length}`
      })()} tone="accent" />
      <KpiCell label="成交额" value={fmtBigNum(data.amount.total)} sub={`均额 ${fmtBigNum(data.amount.avg)}`} />
      <KpiCell label="换手 / 量比" value={`${fmtPrice(data.activity.avg_turnover, 1)}% / ${fmtPrice(data.activity.vol_ratio, 2)}`} sub={`高换手 ${data.activity.high_turnover} · 放量占比 ${fmtPrice(data.activity.high_vol_ratio, 1)}%`} tone="accent" />
    </div>
  )
}

function TrendWidget({ ctx }: { ctx: WidgetCtx }) {
  const { data } = ctx
  return (
    <div className="flex h-full flex-col">
      <div>
        <SectionTitle icon={LineChart} title="趋势强度" hint="均线/新高低" />
        <div className="grid grid-cols-3 gap-1.5">
          <MiniMetric label="站上MA5" value={`${data.trend.above_ma5_pct.toFixed(0)}%`} cls="text-accent" />
          <MiniMetric label="站上MA20" value={`${data.trend.above_ma20_pct.toFixed(0)}%`} cls="text-accent" />
          <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
          <MiniMetric label="60日新高" value={`${compactCount(data.trend.new_high)}`} cls="text-bull" />
          <MiniMetric label="60日新低" value={`${compactCount(data.trend.new_low)}`} cls="text-bear" />
          <MiniMetric label="高低比" value={`${data.trend.new_high + data.trend.new_low > 0 ? Math.round(data.trend.new_high / (data.trend.new_high + data.trend.new_low) * 100) : 50}%`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
        </div>
      </div>
      <div className="mt-1.5 border-t border-border pt-1.5">
        <SectionTitle icon={Target} title="实用监控" hint="盘中观察" />
        <div className="grid grid-cols-3 gap-1.5">
          <MiniMetric label="炸板" value={`${data.limit.broken ?? 0}`} cls="text-warning" />
          <MiniMetric label="跌停" value={`${data.limit.limit_down ?? 0}`} cls="text-bear" />
          <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
          <MiniMetric label="新高/新低" value={`${compactCount(data.trend.new_high)}/${compactCount(data.trend.new_low)}`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
          <MiniMetric label="高换手数" value={`${data.activity.high_turnover}`} cls="text-accent" />
          <MiniMetric label="放量占比" value={`${fmtPrice(data.activity.high_vol_ratio, 1)}%`} cls="text-accent" />
        </div>
      </div>
    </div>
  )
}

function MonitorSection({ ctx }: { ctx: WidgetCtx }) {
  return (
    <section className="h-full overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <BellRing className="h-3.5 w-3.5 text-accent" />
          <h2 className="text-xs font-semibold text-foreground">监控中心</h2>
          <span className="font-mono text-[10px] text-muted">实时信号</span>
        </div>
        <Link to="/monitor" className="inline-flex items-center justify-center h-5 w-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors" title="进入监控中心">
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      </div>
      <div className="max-h-full overflow-y-auto">
        <MonitorWidget
          activeSymbol={ctx.activeSymbol('alert')}
          onStockClick={ctx.openAlert}
        />
      </div>
    </section>
  )
}

/** 外链渲染见 ExternalLinkWidget (registry ext-link 项) */

export const WIDGET_DEFS: WidgetDef[] = [
  {
    id: 'indices', label: '指数行情', icon: Gauge, minW: 4, minH: 2, defW: 12, defH: 2,
    render: ctx => (
      <div className="grid h-full grid-cols-2 gap-1 md:grid-cols-4">
        {ctx.data.indices.map(item => <IndexTicker key={item.symbol} item={item} />)}
      </div>
    ),
  },
  {
    id: 'kpi', label: '大盘指标', icon: BarChart3, minW: 6, minH: 2, defW: 12, defH: 2,
    render: ctx => <KpiWidget ctx={ctx} />,
  },
  {
    id: 'distribution', label: '涨跌分布 / 广度', icon: BarChart3, minW: 3, minH: 6, defW: 4, defH: 8,
    render: ctx => sectionCard(
      <>
        <SectionTitle icon={BarChart3} title="涨跌分布 / 广度" hint={`${ctx.data.breadth.total}只`} />
        <DistributionBars rows={ctx.data.distribution} />
        <div className="mt-2">
          <BreadthBar data={ctx.data.breadth} />
        </div>
        <div className="mt-2 grid grid-cols-2 gap-1.5">
          <MiniMetric label="平均涨跌" value={fmtStockPct(ctx.data.breadth.avg_pct)} cls={pctClass(ctx.data.breadth.avg_pct)} />
          <MiniMetric label="中位涨跌" value={fmtStockPct(ctx.data.breadth.median_pct)} cls={pctClass(ctx.data.breadth.median_pct)} />
        </div>
      </>,
    ),
  },
  {
    id: 'radar', label: '情绪雷达', icon: Sparkles, minW: 3, minH: 7, defW: 4, defH: 8,
    render: ctx => sectionCard(
      <>
        <SectionTitle icon={Sparkles} title="情绪雷达" hint={`情绪评分 ${ctx.score}`} />
        <EmotionRadar radar={ctx.data.radar} score={ctx.score} />
      </>,
      { style: { borderColor: `${scoreColor(ctx.score)}40` } },
    ),
  },
  {
    id: 'trend', label: '趋势强度 / 实用监控', icon: LineChart, minW: 3, minH: 6, defW: 4, defH: 8,
    render: ctx => sectionCard(<TrendWidget ctx={ctx} />),
  },
  {
    id: 'concept-rank', label: '概念热度', icon: Flame, minW: 3, minH: 6, defW: 6, defH: 8,
    render: ctx => (
      <HotRankCard title="概念热度" rank={ctx.data.concept_rank} configUrl="/concept-analysis"
        activeSymbol={ctx.activeSymbol('concept')}
        onStockClick={(symbol, name) => ctx.openStock('concept', symbol, name, rankNav(ctx.data.concept_rank))}
        onDimensionClick={ctx.openDimension} />
    ),
  },
  {
    id: 'industry-rank', label: '行业热度', icon: Flame, minW: 3, minH: 6, defW: 6, defH: 8,
    render: ctx => (
      <HotRankCard title="行业热度" rank={ctx.data.industry_rank} configUrl="/industry-analysis"
        activeSymbol={ctx.activeSymbol('industry')}
        onStockClick={(symbol, name) => ctx.openStock('industry', symbol, name, rankNav(ctx.data.industry_rank))}
        onDimensionClick={ctx.openDimension} />
    ),
  },
  {
    id: 'gainers', label: '涨幅榜', icon: TrendingUp, minW: 2, minH: 7, defW: 3, defH: 10,
    render: ctx => (
      <StockList title="涨幅榜" rows={ctx.data.top_gainers} mode="gain"
        activeSymbol={ctx.activeSymbol('gain')}
        onStockClick={(symbol, name) => ctx.openStock('gain', symbol, name, stockListNav(ctx.data.top_gainers))} />
    ),
  },
  {
    id: 'losers', label: '跌幅榜', icon: TrendingDown, minW: 2, minH: 7, defW: 3, defH: 10,
    render: ctx => (
      <StockList title="跌幅榜" rows={ctx.data.top_losers} mode="loss"
        activeSymbol={ctx.activeSymbol('loss')}
        onStockClick={(symbol, name) => ctx.openStock('loss', symbol, name, stockListNav(ctx.data.top_losers))} />
    ),
  },
  {
    id: 'turnover', label: '成交额榜', icon: Coins, minW: 2, minH: 7, defW: 3, defH: 10,
    render: ctx => (
      <StockList title="成交额榜" rows={ctx.data.turnover_leaders} mode="amount"
        activeSymbol={ctx.activeSymbol('amount')}
        onStockClick={(symbol, name) => ctx.openStock('amount', symbol, name, stockListNav(ctx.data.turnover_leaders))} />
    ),
  },
  {
    id: 'activity', label: '活跃换手', icon: Repeat, minW: 2, minH: 7, defW: 3, defH: 10,
    render: ctx => (
      <StockList title="活跃换手" rows={ctx.data.active_leaders} mode="active"
        activeSymbol={ctx.activeSymbol('active')}
        onStockClick={(symbol, name) => ctx.openStock('active', symbol, name, stockListNav(ctx.data.active_leaders))} />
    ),
  },
  {
    id: 'ladder', label: '涨停梯队', icon: Flame, minW: 3, minH: 6, defW: 4, defH: 8,
    render: ctx => sectionCard(
      <>
        <SectionTitle icon={Flame} title="涨停梯队" hint={<span className="inline-flex items-center gap-1">{`涨停 ${ctx.data.limit.limit_up}`}{ctx.isSealedDegrade && <span className="text-[9px] px-1 rounded bg-yellow-500/10 text-yellow-600 dark:text-yellow-500">{ctx.hasDepth ? '未修正' : '降级'}</span>}</span>} />
        <LadderMini limit={ctx.data.limit} />
      </>,
    ),
  },
  {
    id: 'monitor', label: '监控中心', icon: BellRing, minW: 2, minH: 6, defW: 4, defH: 10,
    render: ctx => <MonitorSection ctx={ctx} />,
  },
  {
    id: 'ext-link', label: '外部链接', icon: Link2, minW: 2, minH: 5, defW: 6, defH: 10,
    render: (_ctx, item) => <ExternalLinkWidget title={item.p?.title ?? '外部链接'} url={item.p?.url ?? 'about:blank'} />,
  },
]

const _DEF_INDEX = new Map(WIDGET_DEFS.map(d => [d.id, d]))

export function widgetDef(id: WidgetType): WidgetDef | undefined {
  return _DEF_INDEX.get(id)
}

export function isKnownWidgetType(t: string): boolean {
  return _DEF_INDEX.has(t as WidgetType)
}

export function minSizeOf(t: WidgetType): { w: number; h: number } {
  const d = _DEF_INDEX.get(t)
  return { w: d?.minW ?? 2, h: d?.minH ?? 4 }
}

/** 默认布局 — 用户定稿 v2(2026-09-27): 顶部通栏 → 中段四列等高(分布/雷达/趋势/梯队 各8行) → 双热度+右侧监控高栏 → 四榜单。
 * 坐标必须是竖直压实后的稳定形态(列内不留空隙), 否则 RGL 渲染时会自动上提,
 * 导致「恢复默认」的 JSON 等值判断永不成立、每次都落 blob 而非 null。 */
export const DEFAULT_LAYOUT: DashboardItem[] = [
  { i: 'indices', t: 'indices', x: 0, y: 0, w: 12, h: 2 },
  { i: 'kpi', t: 'kpi', x: 0, y: 2, w: 12, h: 2 },
  { i: 'distribution', t: 'distribution', x: 0, y: 4, w: 3, h: 8 },
  { i: 'radar', t: 'radar', x: 3, y: 4, w: 3, h: 8 },
  { i: 'trend', t: 'trend', x: 6, y: 4, w: 3, h: 8 },
  { i: 'ladder', t: 'ladder', x: 9, y: 4, w: 3, h: 8 },
  { i: 'concept-rank', t: 'concept-rank', x: 0, y: 12, w: 4, h: 9 },
  { i: 'industry-rank', t: 'industry-rank', x: 4, y: 12, w: 4, h: 9 },
  { i: 'monitor', t: 'monitor', x: 8, y: 12, w: 4, h: 22 },
  { i: 'gainers', t: 'gainers', x: 0, y: 21, w: 2, h: 13 },
  { i: 'losers', t: 'losers', x: 2, y: 21, w: 2, h: 13 },
  { i: 'turnover', t: 'turnover', x: 4, y: 21, w: 2, h: 13 },
  { i: 'activity', t: 'activity', x: 6, y: 21, w: 2, h: 13 },
]
