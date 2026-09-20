import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import {
  Activity, ChevronRight, Compass, FlaskConical, HelpCircle, History, Power,
  Radar, RefreshCw, Ruler, Search, Settings2,
} from 'lucide-react'
import {
  api, type AbnormalIntradayRow, type AbnormalOverview, type AbnormalRow,
  type AbnormalStatus, type AuctionBenchmarkItem, type AuctionBenchmarkPayload,
  type IntradaySignalKey,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { fmtPrice, fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'

/**
 * 异动监控 — 全时段异动中心, 按交易时间线分三个 tab:
 *
 * - 竞价异动 (盘前 9:15-9:25): 同花顺短线风向标名单 + 全市场竞价扫描 (待采集任务)
 * - 盘中异动 (盘中实时): enriched 当日信号聚合 — 涨停/炸板/翘板/跌停/新高/新低/放量
 * - 偏移异动 (多日累计): 交易所异动规则口径 (3日±20%/30%/40%, 10日+100%, 30日+200%)
 *   实时计算个股「偏离值/阈值」接近度, 找出处于异动边缘的标的。
 *
 * 偏移异动计算量可控: 主开关默认关闭, 开启后才发起轮询 (每 60s 一次); 关闭后
 * 保留展示上次计算结果 (含计算时间, 取自 localStorage)。规则口径通过工具栏「?」
 * 展开查看。告警走系统监控体系: 在「监控中心」创建异动监控规则后由后端持续评估,
 * 统一触发记录/站内通知/外部渠道推送。
 */

const WINDOW_KEYS = ['3d', '10d', '30d'] as const
type WindowKey = (typeof WINDOW_KEYS)[number]

const WINDOW_LABELS: Record<WindowKey, string> = {
  '3d': '3日偏离',
  '10d': '10日偏离',
  '30d': '30日偏离',
}

const STATUS_META: Record<AbnormalStatus, { label: string; cls: string; bar: string }> = {
  triggered: { label: '已触发', cls: 'bg-danger/15 text-danger', bar: 'bg-danger' },
  edge: { label: '异动边缘', cls: 'bg-warning/15 text-warning', bar: 'bg-warning' },
  watch: { label: '观察', cls: 'bg-elevated text-secondary', bar: 'bg-muted' },
}

const BOARDS = ['主板', '创业板', '科创板', '北交所'] as const

const REFRESH_MS = 60_000

type AbnormalTab = 'auction' | 'intraday' | 'deviation'

const TAB_META: Array<{ key: AbnormalTab; label: string; icon: typeof Compass; desc: string }> = [
  { key: 'auction', label: '竞价异动', icon: Compass, desc: '盘前 9:15-9:25 · 同花顺风向标 + 竞价扫描' },
  { key: 'intraday', label: '盘中异动', icon: Activity, desc: '当日量价信号 · 涨停/炸板/翘板/新高新低/放量' },
  { key: 'deviation', label: '偏移异动', icon: Ruler, desc: '多日累计偏离值 · 交易所异动规则接近度' },
]

// ---- 盘中信号元数据 (标签 + 配色, 与后端 _INTRADAY_SIGNALS 优先级同序) ----
const SIGNAL_KEYS: IntradaySignalKey[] = ['limit_up', 'broken', 'recovery', 'limit_down', 'new_high', 'new_low', 'volume_surge']

const SIGNAL_META: Record<IntradaySignalKey, { label: string; cls: string }> = {
  limit_up: { label: '涨停', cls: 'text-bull bg-bull/10 border-bull/25' },
  broken: { label: '炸板', cls: 'text-orange-400 bg-orange-400/10 border-orange-400/25' },
  recovery: { label: '翘板', cls: 'text-cyan-400 bg-cyan-400/10 border-cyan-400/25' },
  limit_down: { label: '跌停', cls: 'text-bear bg-bear/10 border-bear/25' },
  new_high: { label: '60日新高', cls: 'text-amber-400 bg-amber-400/10 border-amber-400/25' },
  new_low: { label: '60日新低', cls: 'text-sky-400 bg-sky-400/10 border-sky-400/25' },
  volume_surge: { label: '放量', cls: 'text-violet-400 bg-violet-400/10 border-violet-400/25' },
}

export function AbnormalMoves() {
  const [tab, setTab] = useState<AbnormalTab>('intraday')
  // navList: 弹窗切股的候选列表, 打开详情时由来源 tab 一并给出
  const [preview, setPreview] = useState<{ symbol: string; name: string; navList: NavItem[] } | null>(null)

  // 打开详情: 带上来源 tab 的候选列表, 切股限定在当前所见标的内
  const openStock = useCallback((symbol: string, name?: string, navList?: NavItem[]) => {
    setPreview({ symbol, name: name ?? symbol, navList: navList ?? [] })
  }, [])

  const handleNavigate = useCallback((sym: string, name?: string) => {
    setPreview(prev => (prev ? { ...prev, symbol: sym, name: name ?? sym } : prev))
  }, [])

  return (
    // 整页占满视口: 头部/tab固定, 只有各 tab 内容区滚动
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0">
        <PageHeader
          title="异动监控"
          subtitle="竞价 · 盘中 · 偏移 · 全时段异动中心"
          right={
            <Link
              to="/monitor"
              className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
              title="在监控中心创建「异动监控」规则: 后台持续评估, 触发时统一走触发记录/站内通知/外部渠道推送, 无需保持本页打开"
            >
              <Settings2 className="h-3 w-3" />
              告警规则
            </Link>
          }
        />
      </div>

      {/* tab 条: 交易时间线 竞价(盘前) → 盘中 → 偏移(多日) */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 px-5 pt-3">
        <div className="inline-flex items-center gap-0.5 rounded-full border border-border/50 bg-base/70 p-0.5">
          {TAB_META.map(t => {
            const Icon = t.icon
            const active = tab === t.key
            return (
              <button
                key={t.key}
                type="button"
                aria-pressed={active}
                onClick={() => setTab(t.key)}
                className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs transition-all ${
                  active
                    ? 'bg-accent/15 font-medium text-accent shadow-sm'
                    : 'text-secondary hover:text-foreground'
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {t.label}
              </button>
            )
          })}
        </div>
        <span className="text-[10px] text-muted">{TAB_META.find(t => t.key === tab)?.desc}</span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col px-5 pb-4 pt-3">
        {tab === 'auction' && <AuctionView onOpenStock={openStock} />}
        {tab === 'intraday' && (
          <IntradayView onPreview={(r, navList) => openStock(r.symbol, r.name ?? undefined, navList)} />
        )}
        {tab === 'deviation' && (
          <DeviationView onPreview={(r, navList) => openStock(r.symbol, r.name ?? undefined, navList)} />
        )}
      </div>

      {preview && (
        <StockPreviewDialog
          symbol={preview.symbol}
          name={preview.name}
          navList={preview.navList}
          onNavigate={handleNavigate}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )
}

// ================================================================
// 竞价异动 tab
// ================================================================

/** 追高风险阈值: 60日回测高开≥5%子集当日开盘买 -1.97% (温和高开才是名单 alpha 来源) */
const _BENCH_CHASE_RISK_PCT = 5

function AuctionView({ onOpenStock }: {
  onOpenStock: (symbol: string, name?: string, navList?: NavItem[]) => void
}) {
  const q = useQuery({
    queryKey: ['auction-benchmark', 'latest'],
    queryFn: () => api.auctionBenchmark(),
    staleTime: 5 * 60_000,
    retry: 1,
  })

  // fuyao 未配置: 整个 tab 的统一引导态 (风向标与全市场扫描都依赖 fuyao),
  // 不再展示零散的降级卡/占位卡 — 与偏移 tab「监控未开启」空态同款式
  if (q.data?.state === 'source_unavailable') {
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <div className="m-auto rounded-card border border-border bg-surface p-8 text-center">
          <span className="mx-auto grid h-10 w-10 place-items-center rounded-full bg-cyan-500/10 text-cyan-500 ring-1 ring-cyan-500/20">
            <Compass className="h-5 w-5" />
          </span>
          <div className="mt-3 text-sm font-medium text-foreground">竞价数据源未配置</div>
          <p className="mx-auto mt-2 max-w-md text-xs leading-relaxed text-muted">
            竞价异动 (同花顺盘前风向标与全市场竞价扫描) 依赖 fuyao 数据源,
            复盘页的龙虎榜同样来自该数据源。在「设置 → 数据源」配置 fuyao API Key 后即可使用。
          </p>
          <Link
            to="/settings?tab=data-sources"
            className="mt-5 inline-flex h-9 items-center gap-2 rounded-btn bg-accent px-4 text-xs font-medium text-base transition-colors hover:bg-accent/90"
          >
            前往配置数据源
            <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 overflow-y-auto">
      <BenchmarkCard q={q} onOpenStock={onOpenStock} />

      {/* 全市场竞价扫描: 采集任务启用后填充 (接口与批量能力已验证) */}
      <div className="rounded-card border border-dashed border-border bg-surface/50 px-4 py-4">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-elevated/60">
            <Radar className="h-4 w-4 text-muted/50" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-foreground">全市场竞价扫描</span>
              <span className="rounded-full border border-border bg-elevated px-2 py-px text-[9px] leading-tight text-muted">
                待采集任务启用
              </span>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              9:25 竞价终态后扫描全市场 (实测 5547 只约 2 秒), 自动筛出高开 ≥5% 且竞价量比 ≥10
              的标的并按日落盘积累历史。竞价明细无历史接口, 数据从采集启用之日起积累。
            </p>
          </div>
        </div>
      </div>

      <p className="px-1 text-[10px] leading-relaxed text-muted/70">
        风向标为同花顺盘前竞价筛选名单 (每日约 5~6 只)。60 日回测: 名单当日开盘买入均值 +0.54%
        (超额 +0.44%), 但高开 ≥5% 子集当日 -1.97% — 追高是陷阱, 次日无显著优势, 仅作当日观察。
      </p>
    </div>
  )
}

function BenchmarkCard({ q, onOpenStock }: {
  q: UseQueryResult<AuctionBenchmarkPayload, Error>
  onOpenStock: (symbol: string, name?: string, navList?: NavItem[]) => void
}) {
  const d = q.data
  // 切股导航列表: 本期名单 (须在提前 return 之前 memo, 之后不能再放 hook)
  const navItems = useMemo(
    () => toNavItems((d?.items ?? []).map(i => ({ symbol: i.thscode, name: i.name }))),
    [d],
  )

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-3 rounded-card border border-border bg-surface/80 px-4 py-3">
        <span className="grid h-8 w-8 shrink-0 animate-pulse place-items-center rounded bg-elevated">
          <Compass className="h-4 w-4 text-muted/50" />
        </span>
        <div className="flex items-center gap-2">
          <span className="h-3 w-14 animate-pulse rounded-full bg-elevated/80" />
          <span className="h-3 w-24 animate-pulse rounded-full bg-elevated/60" />
          <span className="h-3 w-20 animate-pulse rounded-full bg-elevated/40" />
        </div>
      </div>
    )
  }

  // source_unavailable (fuyao 未配置) 由 AuctionView 统一引导态处理, 此处不再分支

  if (!d || d.state === 'no_data') {
    return (
      <div className="flex items-center gap-3 rounded-card border border-border bg-surface/50 px-4 py-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-elevated/60">
          <Compass className="h-4 w-4 text-muted/50" />
        </span>
        <span className="text-[11px] text-muted">盘前风向标暂不可用{d?.message ? ` (${d.message.slice(0, 40)})` : ''}</span>
        <button onClick={() => q.refetch()} className="ml-auto text-[10px] text-accent hover:underline">重试</button>
      </div>
    )
  }

  const items = d.items ?? []
  const isFallback = d.state === 'fallback_prev'
  const ocs = items.map(i => i.day0_oc).filter((v): v is number => v != null)
  const avgOc = ocs.length ? ocs.reduce((a, b) => a + b, 0) / ocs.length : null

  return (
    <div className="rounded-card border border-border bg-surface/80">
      {/* 头部 */}
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-cyan-500/15 text-cyan-500 ring-1 ring-cyan-500/20">
          <Compass className="h-4 w-4" />
        </span>
        <span className="leading-tight">
          <span className="flex items-center gap-2">
            <span className="text-[13px] font-semibold text-foreground">盘前风向标</span>
            {isFallback && (
              <span
                className="rounded border border-warning/30 bg-warning/10 px-1.5 py-px text-[9px] leading-tight text-warning"
                title="目标日名单不可用, 已自动显示上一期"
              >
                显示上一期
              </span>
            )}
          </span>
          <span className="mt-0.5 block text-[10px] text-muted">
            {d.trade_date} · {items.length} 只 · 同花顺竞价筛选
            {avgOc != null && (
              <> · 当日开盘买均值 <span className={priceColorClass(avgOc)}>{fmtPct(avgOc)}</span></>
            )}
          </span>
        </span>
        <span className="ml-auto text-right text-[9px] leading-tight text-muted/70">
          次日无优势<br />仅当日观察
        </span>
      </div>

      {/* 名单 */}
      {items.length === 0 ? (
        <p className="border-t border-border/60 px-4 py-3 text-center text-[11px] text-muted">本期无名单数据</p>
      ) : (
        <div>
          <div className="flex items-center gap-2 border-t border-border/60 bg-elevated/50 px-4 py-1.5 text-[9px] font-medium uppercase tracking-wider text-muted/70">
            <span className="w-14 shrink-0">竞价</span>
            <span className="min-w-0 flex-1">股票</span>
            <span className="w-16 shrink-0 text-right">当日</span>
            <span className="w-16 shrink-0 text-right">次日</span>
          </div>
          {items.map((i: AuctionBenchmarkItem) => {
            const gap = i.auction_pct ?? null
            const chase = (gap ?? 0) >= _BENCH_CHASE_RISK_PCT
            return (
              <button
                key={i.thscode}
                type="button"
                onClick={() => onOpenStock(i.thscode, i.name ?? undefined, navItems)}
                className="flex w-full items-center gap-2 border-t border-border/30 px-4 py-2 text-left text-[11px] transition-colors hover:bg-accent/[0.05]"
                title={`查看 ${i.name ?? i.thscode} 详情 · 竞价 ${gap ?? '—'}%`}
              >
                <span className={('w-14 shrink-0 font-mono tabular-nums ' + priceColorClass(gap)).trim()}>
                  {gap == null ? '—' : fmtPct(gap / 100)}
                </span>
                <span className="flex min-w-0 flex-1 items-center gap-1.5">
                  <span className="truncate text-foreground">{i.name ?? i.thscode}</span>
                  <span className="shrink-0 font-mono text-[9px] text-muted">{i.ticker ?? i.thscode}</span>
                  {(() => { const b = boardTag(i.thscode); return b && (
                    <span className={`shrink-0 inline-flex items-center rounded border px-1 text-[8px] font-bold leading-tight ${b.color}`}>
                      {b.label}
                    </span>
                  ) })()}
                  {chase && (
                    <span
                      className="shrink-0 rounded border border-danger/30 bg-danger/10 px-1 text-[8px] font-medium leading-tight text-danger"
                      title="60日回测: 高开≥5%子集当日开盘买入平均 -1.97% (次日 +1.79%) — 追高陷阱"
                    >
                      追高风险
                    </span>
                  )}
                  {(i.tags ?? []).slice(0, 2).map(t => (
                    <span key={t} className="max-w-24 truncate rounded-full bg-base/70 px-1.5 py-px text-[9px] text-muted" title={t}>
                      {t}
                    </span>
                  ))}
                </span>
                <span
                  className={('w-16 shrink-0 text-right font-mono tabular-nums ' + priceColorClass(i.day0_oc)).trim()}
                  title={i.day0_pct != null ? `全天 ${fmtPct(i.day0_pct)}` : undefined}
                >
                  {i.day0_oc == null ? '—' : fmtPct(i.day0_oc)}
                </span>
                <span className={('w-16 shrink-0 text-right font-mono tabular-nums ' + (i.d1_pct == null ? 'text-muted' : priceColorClass(i.d1_pct))).trim()}>
                  {i.d1_pct == null ? '—' : fmtPct(i.d1_pct)}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ================================================================
// 盘中异动 tab
// ================================================================

function IntradayView({ onPreview }: {
  onPreview: (r: AbnormalIntradayRow, navList?: NavItem[]) => void
}) {
  const [sigFilter, setSigFilter] = useState<'all' | IntradaySignalKey>('all')
  const [boardFilter, setBoardFilter] = useState<'all' | (typeof BOARDS)[number]>('all')
  const [query, setQuery] = useState('')
  const [excludeSt, setExcludeSt] = useState(true)

  const q = useQuery({
    queryKey: QK.abnormalIntraday(500),
    queryFn: () => api.abnormalIntraday(500),
    refetchInterval: REFRESH_MS,
  })
  const data = q.data
  const counts = data?.counts ?? {}

  const rows = useMemo(() => {
    let list = data?.rows ?? []
    if (sigFilter !== 'all') list = list.filter(r => r.signals.includes(sigFilter))
    if (boardFilter !== 'all') {
      // boardTag: 创/科/北有徽章, 主板返回 null
      const want = boardFilter === '主板' ? null
        : boardFilter === '创业板' ? '创'
          : boardFilter === '科创板' ? '科' : '北'
      list = list.filter(r => (boardTag(r.symbol)?.label ?? null) === want)
    }
    if (excludeSt) list = list.filter(r => !(r.name ?? '').toUpperCase().includes('ST'))
    const s = query.trim().toLowerCase()
    if (s) list = list.filter(r => `${r.symbol} ${r.name ?? ''}`.toLowerCase().includes(s))
    return list
  }, [data, sigFilter, boardFilter, excludeSt, query])

  // 切股导航列表: 当前筛选后的行序
  const navItems = useMemo(() => toNavItems(rows), [rows])

  const total = (data?.rows ?? []).length

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {/* 信号筛选 chips (带各类型计数) + 工具行 */}
      <div className="flex shrink-0 flex-wrap items-center gap-1.5">
        <SigChip active={sigFilter === 'all'} onClick={() => setSigFilter('all')} label="全部" count={total} />
        {SIGNAL_KEYS.map(k => (
          <SigChip
            key={k}
            active={sigFilter === k}
            onClick={() => setSigFilter(k)}
            label={SIGNAL_META[k].label}
            count={counts[k] ?? 0}
            cls={SIGNAL_META[k].cls}
          />
        ))}
        <span className="ml-1 text-[10px] text-muted">
          数据截至 {data?.cache_date ?? '—'}
          {q.isFetching && ' · 更新中…'}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <SegmentedControl
            value={boardFilter}
            onChange={v => setBoardFilter(v)}
            options={[
              { value: 'all' as const, label: '全板块' },
              ...BOARDS.map(b => ({ value: b, label: b })),
            ]}
          />
          <button
            type="button"
            onClick={() => q.refetch()}
            className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
            title="立即刷新"
          >
            <RefreshCw className={`h-3 w-3 ${q.isFetching ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="过滤 ST/*ST 风险警示股票">
            <input type="checkbox" checked={excludeSt} onChange={e => setExcludeSt(e.target.checked)} className="h-3 w-3 accent-accent" />
            过滤ST
          </label>
          <div className="relative">
            <Search className="absolute left-2 top-1.5 h-3.5 w-3.5 text-muted" />
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="搜索代码/名称"
              className="h-7 w-40 rounded border border-border bg-base pl-7 pr-2 text-[11px] text-foreground"
            />
          </div>
        </div>
      </div>

      {/* 主表 */}
      <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border bg-surface">
        <table className="w-full min-w-[900px] text-xs">
          <thead className="sticky top-0 z-10 bg-surface">
            <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted">
              <th className="w-10 px-2 py-2 text-right">#</th>
              <th className="px-2 py-2 text-left">代码 / 名称</th>
              <th className="px-2 py-2 text-right">现价</th>
              <th className="px-2 py-2 text-right">今日</th>
              <th className="px-2 py-2 text-left">信号</th>
              <th className="px-2 py-2 text-right" title="当日成交量 / 前5日平均成交量">量比</th>
              <th className="px-2 py-2 text-right" title="当日高低价差 / 前收盘价">振幅</th>
              <th className="px-2 py-2 text-right">换手</th>
            </tr>
          </thead>
          <tbody>
            {q.isLoading ? (
              <tr><td colSpan={8} className="px-3 py-10 text-center text-muted">正在加载盘中信号…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={8} className="px-3 py-10 text-center text-muted">{data ? '当前筛选下没有命中标的' : '暂无数据'}</td></tr>
            ) : (
              rows.map((r, i) => (
                <IntradayRowView key={r.symbol} row={r} rank={i + 1} onPreview={() => onPreview(r, navItems)} />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SigChip({ active, onClick, label, count, cls }: {
  active: boolean
  onClick: () => void
  label: string
  count: number
  cls?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
        active
          ? (cls ?? 'border-accent/40 bg-accent/12 text-accent')
          : 'border-border bg-elevated text-secondary hover:text-foreground'
      }`}
    >
      <span className="font-mono text-xs font-semibold tabular-nums">{count}</span>
      {label}
    </button>
  )
}

function IntradayRowView({ row, rank, onPreview }: {
  row: AbnormalIntradayRow
  rank: number
  onPreview: () => void
}) {
  const board = boardTag(row.symbol)
  const clu = row.consecutive_limit_ups ?? 0
  return (
    <tr className="group border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/50">
      <td className="px-2 py-1.5 text-right font-mono text-[10px] text-muted/70">{rank}</td>
      <td className="px-2 py-1.5">
        <button
          type="button"
          onClick={onPreview}
          title="查看个股详情"
          className="flex min-w-0 items-center gap-1.5 text-left"
        >
          <span className="shrink-0 font-mono text-xs text-foreground transition-colors duration-150 group-hover:text-accent">{row.symbol}</span>
          <span className="min-w-0 max-w-40 truncate text-xs text-secondary transition-colors duration-150 group-hover:text-foreground">{row.name ?? '—'}</span>
          {board && (
            <span className={`shrink-0 rounded border px-1 text-[9px] font-bold leading-tight ${board.color}`}>
              {board.label}
            </span>
          )}
          {clu > 1 && (
            <span className="shrink-0 rounded border border-amber-500/30 bg-amber-500/10 px-1 text-[9px] font-bold leading-tight text-amber-500" title={`连续 ${clu} 日涨停`}>
              {clu}连板
            </span>
          )}
        </button>
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs text-secondary">{fmtPrice(row.close)}</td>
      <td className={`px-2 py-1.5 text-right font-mono text-xs font-medium ${priceColorClass(row.change_pct)}`}>
        {fmtPct(row.change_pct)}
      </td>
      <td className="px-2 py-1.5">
        <div className="flex flex-wrap items-center gap-1">
          {row.signals.map(s => (
            <span key={s} className={`rounded border px-1 text-[9px] font-medium leading-tight ${SIGNAL_META[s].cls}`}>
              {SIGNAL_META[s].label}
            </span>
          ))}
        </div>
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs tabular-nums text-secondary">
        {row.vol_ratio_5d != null ? row.vol_ratio_5d.toFixed(2) : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs tabular-nums text-secondary">
        {row.amplitude != null ? fmtPct(row.amplitude, 2) : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs tabular-nums text-secondary">
        {row.turnover_rate != null ? `${Number(row.turnover_rate).toFixed(2)}%` : '—'}
      </td>
    </tr>
  )
}

// ================================================================
// 偏移异动 tab (原有异动边缘监控, 逻辑保持不变)
// ================================================================

function DeviationView({ onPreview }: {
  onPreview: (r: AbnormalRow, navList?: NavItem[]) => void
}) {
  // 主开关: 默认关闭, 开启后才轮询计算 (仅控制本页计算, 后台告警由监控规则驱动)
  const [enabled, setEnabled] = useState(() => storage.abnormalEnabled.get(false))
  // 规则口径面板 (工具栏「?」)
  const [rulesOpen, setRulesOpen] = useState(false)
  // 上次计算结果: 开启时每次成功计算都落本地, 关闭后仍展示
  const [lastResult, setLastResult] = useState<AbnormalOverview | null>(
    () => (storage.abnormalLastResult.get(null) as AbnormalOverview | null) ?? null,
  )
  const [windowFilter, setWindowFilter] = useState<'all' | WindowKey>('all')
  const [direction, setDirection] = useState<'both' | 'up' | 'down'>('both')
  const [boardFilter, setBoardFilter] = useState<'all' | (typeof BOARDS)[number]>('all')
  const [minCloseness, setMinCloseness] = useState(0.5)
  const [query, setQuery] = useState('')
  const [watchlistOnly, setWatchlistOnly] = useState(false)
  // 默认过滤 ST/*ST 风险警示股票 (口径与后端 is_st_name 一致: 名称含 ST)
  const [excludeSt, setExcludeSt] = useState(true)

  const overview = useQuery({
    queryKey: QK.abnormalOverview(minCloseness, 300),
    queryFn: () => api.abnormalOverview(minCloseness, 300),
    enabled, // 关闭时零计算
    refetchInterval: enabled ? REFRESH_MS : false,
  })
  // 自选过滤在关闭 (查看上次结果) 时也可用: 自选列表是轻量接口, 不涉及全市场计算
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: watchlistOnly,
  })

  const toggleEnabled = (v: boolean) => {
    setEnabled(v)
    storage.abnormalEnabled.set(v)
    if (v) {
      overview.refetch()
    }
  }

  const data = overview.data
  useEffect(() => {
    if (!data) return
    setLastResult(data)
    storage.abnormalLastResult.set(data)
  }, [data])

  // 展示数据源: 开启 → 实时结果; 关闭 → 上次计算结果 (可能为空)
  const view = enabled ? data : lastResult
  const stale = !enabled && lastResult != null

  const watchSymbols = useMemo(() => {
    const set = new Set((watchlist.data?.symbols ?? []).map(e => e.symbol))
    return set
  }, [watchlist.data])

  const rows = useMemo(() => {
    let list = view?.rows ?? []
    if (windowFilter !== 'all') {
      list = list.filter(r => {
        const w = r.windows[windowFilter]
        return w != null && w.closeness >= minCloseness
      })
    }
    if (direction !== 'both') {
      list = list.filter(r => {
        const w = windowFilter !== 'all' ? r.windows[windowFilter] : dominantWindow(r)
        const v = w?.value ?? 0
        return direction === 'up' ? v > 0 : v < 0
      })
    }
    if (boardFilter !== 'all') list = list.filter(r => r.board === boardFilter)
    if (watchlistOnly) list = list.filter(r => watchSymbols.has(r.symbol))
    if (excludeSt) list = list.filter(r => !(r.name ?? '').toUpperCase().includes('ST'))
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter(r => `${r.symbol} ${r.name ?? ''}`.toLowerCase().includes(q))
    }
    return list
  }, [view, windowFilter, direction, boardFilter, watchlistOnly, excludeSt, watchSymbols, query, minCloseness])

  // 切股导航列表: 当前筛选后的行序
  const navItems = useMemo(() => toNavItems(rows), [rows])

  const counts = view?.counts
  const updating = overview.isFetching

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {/* 规则口径面板 (工具栏「?」展开) */}
      {rulesOpen && (
        <div className="shrink-0 rounded-card border border-border bg-surface p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {ruleChips()}
          </div>
          <p className="mt-2.5 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted">
            口径说明: 偏离值 = 个股 N 日累计涨跌幅 − 对应指数同期涨跌幅 (沪主板: 上证A指/上证指数,
            科创板: 科创50, 深主板: 深证A指/深证成指, 创业板: 创业板综指, 北: 北证50)。
            阈值为交易所异常波动披露标准的近似值, 仅供风险提示,
            不构成监管认定。每只股票在 3日/10日/30日 三档各算一个接近度 (|偏离值| ÷ 该档阈值,
            阈值随板块不同; 2026-07-06 起主板风险警示股票与普通股票同口径), 表格「接近度」列与状态取三档中的最高值,
            来源窗口的偏离值颜色加重显示、其余窗口淡化; ≥100% 已触发、≥70% 边缘、≥50% 观察。
            偏离列亦可在自选/选股的「异动」列组中启用, 并可作为监控规则与自定义信号的阈值字段。
          </p>
        </div>
      )}

      {/* 未开启且无历史结果: 说明 + 开启入口 (有上次结果时直接展示数据, 见下方 stale 横幅) */}
      {!enabled && !stale ? (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <div className="m-auto rounded-card border border-border bg-surface p-8 text-center">
            <FlaskConical className="mx-auto h-8 w-8 text-muted/50" />
            <div className="mt-3 text-sm font-medium text-foreground">监控未开启</div>
            <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-muted">
              开启后按交易所异动规则实时计算全市场个股的涨跌幅偏离值 (个股 N 日累计涨跌 −
              对应指数同期), 找出接近触发「异常波动 / 严重异常波动」的标的。
              计算量较大, 默认关闭; 每次计算的结果会保留, 关闭后仍可查看 (不再实时更新)。
            </p>
            <p className="mx-auto mt-2 max-w-lg text-[11px] leading-relaxed text-muted/80">
              需要告警推送时, 在<Link to="/monitor?new=abnormal" className="text-accent hover:underline">监控中心</Link>
              新建「异动监控」规则 —— 后台持续评估, 触发时统一走触发记录 / 站内通知 / 外部渠道推送,
              与本页开关互不影响。
            </p>
            <button
              type="button"
              onClick={() => toggleEnabled(true)}
              className="mt-5 inline-flex h-9 items-center gap-2 rounded-btn bg-accent px-4 text-xs font-medium text-base"
            >
              <Power className="h-4 w-4" />
              开启监控
            </button>
          </div>
        </div>
      ) : (
        <>
          {/* 关闭后展示上次计算结果 */}
          {stale && (
            <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-card border border-warning/25 bg-warning/5 px-3 py-2">
              <History className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-[11px] font-medium text-warning">已暂停计算 · 展示上次结果</span>
              <span className="text-[11px] text-secondary">
                上次计算 {fmtCalcTime(lastResult.asof)} · 数据截至 {lastResult.cache_date ?? '—'}
                {lastResult.includes_today ? ' (含今日收盘)' : ''}
              </span>
              <button
                type="button"
                onClick={() => toggleEnabled(true)}
                className="ml-auto inline-flex h-7 shrink-0 items-center gap-1 rounded border border-accent/40 bg-accent/10 px-2.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent/15"
              >
                <Power className="h-3 w-3" />
                开启实时计算
              </button>
            </div>
          )}

          {/* 统计 + 控制 */}
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <StatusChip label="已触发" count={counts?.triggered} tone="danger" />
            <StatusChip label="异动边缘" count={counts?.edge} tone="warning" />
            <StatusChip label="观察" count={counts?.watch} tone="muted" />
            {enabled && (
              <span className="text-[10px] text-muted">
                数据截至 {data?.cache_date ?? '—'}
                {data?.includes_today ? ' (含今日收盘)' : ' · 已叠加今日实时涨跌'}
                {data ? ` · 基准指数今日 ${(data.bench_rt_pct * 100).toFixed(2)}%` : ''}
              </span>
            )}
            <div className="ml-auto flex items-center gap-2">
              <button
                type="button"
                aria-pressed={rulesOpen}
                aria-label="查看异动规则口径"
                title="交易所异动规则口径 (阈值 / 偏离值计算方式)"
                onClick={() => setRulesOpen(v => !v)}
                className={`inline-flex h-7 w-7 items-center justify-center rounded border transition-colors ${
                  rulesOpen
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-border bg-base text-secondary hover:text-foreground'
                }`}
              >
                <HelpCircle className="h-3.5 w-3.5" />
              </button>
              {enabled && (
                <button
                  type="button"
                  onClick={() => overview.refetch()}
                  className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
                  title="立即刷新"
                >
                  <RefreshCw className={`h-3 w-3 ${updating ? 'animate-spin' : ''}`} />
                  刷新
                </button>
              )}
              {/* 主开关: 开启后才开始轮询计算 */}
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                aria-label="启用异动监控计算"
                onClick={() => toggleEnabled(!enabled)}
                className={`inline-flex h-7 items-center gap-2 rounded border px-2.5 text-[11px] font-medium transition-colors ${
                  enabled
                    ? 'border-accent/40 bg-accent/12 text-accent'
                    : 'border-border bg-base text-secondary hover:text-foreground'
                }`}
              >
                <Power className="h-3 w-3" />
                {enabled ? '监控中 · 每60秒计算' : '开启监控'}
              </button>
            </div>
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <SegmentedControl
              value={windowFilter}
              onChange={v => setWindowFilter(v)}
              options={[
                { value: 'all', label: '全部窗口' },
                ...WINDOW_KEYS.map(w => ({ value: w, label: WINDOW_LABELS[w] })),
              ]}
            />
            <SegmentedControl
              value={direction}
              onChange={v => setDirection(v)}
              options={[
                { value: 'both', label: '双向' },
                { value: 'up', label: '正向' },
                { value: 'down', label: '负向' },
              ]}
            />
            <SegmentedControl
              value={boardFilter}
              onChange={v => setBoardFilter(v)}
              options={[
                { value: 'all' as const, label: '全板块' },
                ...BOARDS.map(b => ({ value: b, label: b })),
              ]}
            />
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="只看自选列表中的标的">
              <input
                type="checkbox"
                checked={watchlistOnly}
                onChange={e => setWatchlistOnly(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              只看自选
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="过滤 ST/*ST 风险警示股票">
              <input
                type="checkbox"
                checked={excludeSt}
                onChange={e => setExcludeSt(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              过滤ST
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="接近度下限 (|偏离|/阈值)">
              接近度 ≥ {(minCloseness * 100).toFixed(0)}%
              <input
                type="range"
                min={30}
                max={100}
                step={5}
                value={minCloseness * 100}
                onChange={e => setMinCloseness(Number(e.target.value) / 100)}
                className="h-1 w-24 accent-accent"
              />
            </label>
            <div className="relative ml-auto">
              <Search className="absolute left-2 top-1.5 h-3.5 w-3.5 text-muted" />
              <input
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="搜索代码/名称"
                className="h-7 w-40 rounded border border-border bg-base pl-7 pr-2 text-[11px] text-foreground"
              />
            </div>
          </div>

          {/* 主表: 剩余空间内滚动 (页面本身不滚动) */}
          <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border bg-surface">
            <table className="w-full min-w-[860px] text-xs">
              <thead className="sticky top-0 z-10 bg-surface">
                <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted">
                  <th className="w-10 px-2 py-2 text-right">#</th>
                  <th className="px-2 py-2 text-left">代码 / 名称</th>
                  <th className="px-2 py-2 text-right">现价</th>
                  <th className="px-2 py-2 text-right">今日</th>
                  {WINDOW_KEYS.map(w => (
                    <th key={w} className="px-2 py-2 text-right">
                      {WINDOW_LABELS[w]}
                      <span className="ml-1 normal-case text-muted/60">(阈值)</span>
                    </th>
                  ))}
                  <th
                    className="w-36 px-2 py-2 text-left"
                    title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
                  >
                    接近度
                    <span className="ml-1 normal-case text-muted/60">(最高档)</span>
                  </th>
                  <th className="px-2 py-2 text-center">状态</th>
                </tr>
              </thead>
              <tbody>
                {overview.isLoading ? (
                  <tr>
                    <td colSpan={9} className="px-3 py-10 text-center text-muted">
                      正在计算全市场偏离值…
                    </td>
                  </tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="px-3 py-10 text-center text-muted">
                      {view ? '当前没有满足条件的标的' : '暂无数据'}
                    </td>
                  </tr>
                ) : (
                  rows.map((r, i) => (
                    <AbnormalRowView
                      key={r.symbol}
                      row={r}
                      rank={i + 1}
                      onPreview={() => onPreview(r, navItems)}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )

  function ruleChips() {
    return (view?.rules ?? FALLBACK_RULES).map((rule, i) => {
      // 对称窗口 (3日) 显示 ±X%; 严重异动窗口正负阈值不同, 显示 +X%/−Y%
      const thr = WINDOW_KEYS.map(w => {
        const t = rule.thresholds[w]
        const s = t.up === t.down ? `±${fmtThreshold(t.up)}` : `+${fmtThreshold(t.up)}/−${fmtThreshold(t.down)}`
        return `${w.replace('d', '日')}${s}`
      }).join(' / ')
      return (
        <div key={i} className="rounded border border-border bg-base px-2.5 py-2">
          <div className="text-[11px] font-medium text-foreground">
            {rule.board}
            {rule.st && <span className="ml-1 text-danger">ST</span>}
          </div>
          <div className="mt-0.5 font-mono text-[10px] text-muted">{thr}</div>
        </div>
      )
    })
  }
}

/** 上次计算时间 (服务端 asof 秒级时间戳 → 本地日期时间) */
function fmtCalcTime(asofSec: number): string {
  const d = new Date(asofSec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtThreshold(v: number | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(0)}%`
}

/** 全窗口里接近度最高的窗口 */
function dominantWindow(r: AbnormalRow): { key: WindowKey; value: number; threshold: number; closeness: number } | undefined {
  let best: { key: WindowKey; value: number; threshold: number; closeness: number } | undefined
  for (const w of WINDOW_KEYS) {
    const info = r.windows[w]
    if (info && (!best || info.closeness > best.closeness)) best = { key: w, ...info }
  }
  return best
}

function AbnormalRowView({ row, rank, onPreview }: {
  row: AbnormalRow
  rank: number
  onPreview: () => void
}) {
  const board = boardTag(row.symbol)
  const dominant = dominantWindow(row)
  const meta = STATUS_META[row.status]
  return (
    <tr className="group border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/50">
      <td className="px-2 py-1.5 text-right font-mono text-[10px] text-muted/70">{rank}</td>
      <td className="px-2 py-1.5">
        {/* 仅代码/名称可点击打开详情 (与自选列表一致), 其余单元格不可点 */}
        <button
          type="button"
          onClick={onPreview}
          title="查看个股详情"
          className="flex min-w-0 items-center gap-1.5 text-left"
        >
          <span className="shrink-0 font-mono text-xs text-foreground group-hover:text-accent transition-colors duration-150">{row.symbol}</span>
          <span className="min-w-0 max-w-40 truncate text-xs text-secondary group-hover:text-foreground transition-colors duration-150">{row.name ?? '—'}</span>
          {board && (
            <span className={`shrink-0 rounded px-1 text-[9px] font-bold leading-tight border ${board.color}`}>
              {board.label}
            </span>
          )}
          {row.st && (
            <span className="shrink-0 rounded border border-danger/30 bg-danger/10 px-1 text-[9px] font-bold text-danger">
              ST
            </span>
          )}
        </button>
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs text-secondary">{fmtPrice(row.close)}</td>
      <td className={`px-2 py-1.5 text-right font-mono text-xs font-medium ${priceColorClass(row.rt_pct)}`}>
        {fmtPct(row.rt_pct)}
      </td>
      {WINDOW_KEYS.map(w => {
        const info = row.windows[w]
        // 接近度取最高档: 来源窗口颜色加重 (加粗), 其余窗口淡化, 以此区分「哪一档」
        const isDominant = dominant?.key === w
        // 后端 threshold 已按偏离方向取对应侧 (严重异动负向阈值更严)
        const sign = info && info.value >= 0 ? '+' : '−'
        return (
          <td key={w} className="px-2 py-1.5 text-right">
            {info ? (
              <span
                className={`font-mono text-xs tabular-nums ${priceColorClass(info.value)} ${isDominant ? 'font-semibold' : 'opacity-45'}`}
                title={`阈值 ${sign}${fmtThreshold(info.threshold)} · 接近度 ${(info.closeness * 100).toFixed(0)}%${isDominant ? ' · 本行接近度来源' : ''}`}
              >
                {fmtPct(info.value)}
                <span className="ml-1 text-[9px] text-muted/60">/{sign}{fmtThreshold(info.threshold)}</span>
              </span>
            ) : (
              <span className="text-muted/40">—</span>
            )}
          </td>
        )
      })}
      <td className="px-2 py-1.5">
        <div
          className="flex items-center gap-1.5"
          title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
        >
          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-elevated">
            <div
              className={`h-full rounded-full transition-all ${meta.bar}`}
              style={{ width: `${Math.min(100, (dominant?.closeness ?? 0) * 100)}%` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-secondary">
            {((dominant?.closeness ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
      </td>
      <td className="px-2 py-1.5 text-center">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}>{meta.label}</span>
      </td>
    </tr>
  )
}

function StatusChip({ label, count, tone }: { label: string; count?: number; tone: 'danger' | 'warning' | 'muted' }) {
  const toneCls =
    tone === 'danger'
      ? 'border-danger/30 bg-danger/8 text-danger'
      : tone === 'warning'
        ? 'border-warning/30 bg-warning/8 text-warning'
        : 'border-border bg-elevated text-secondary'
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${toneCls}`}>
      <span className="font-mono text-sm font-semibold tabular-nums">{count ?? '—'}</span>
      {label}
    </span>
  )
}

function SegmentedControl<T extends string>({ value, onChange, options }: {
  value: T
  onChange: (v: T) => void
  options: Array<{ value: T; label: string }>
}) {
  return (
    <div className="inline-flex h-7 overflow-hidden rounded border border-border bg-base">
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`px-2.5 text-[11px] transition-colors ${
            value === o.value ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** 后端数据未到时的规则表兜底 (与后端 RULES_META 同步维护)
 *  阈值为 {正, 负} 双侧: 3日对称, 严重异动 10日+100%/−50%、30日+200%/−70% (负向更严)
 *  2026-07-06 起主板风险警示(ST)股票与普通股票同标准 (原±15%特别规定已废止) */
const FALLBACK_RULES: Array<{ board: string; st: boolean; thresholds: Record<string, { up: number; down: number }>; note: string }> = [
  { board: '主板', st: false, thresholds: { '3d': { up: 0.2, down: 0.2 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '创业板/科创板', st: false, thresholds: { '3d': { up: 0.3, down: 0.3 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '北交所', st: false, thresholds: { '3d': { up: 0.4, down: 0.4 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
]
