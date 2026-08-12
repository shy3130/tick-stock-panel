import { useMemo, useState, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'framer-motion'
import {
  Activity,
  Crown,
  Layers3,
  RefreshCw,
  Repeat,
  Search,
  Settings2,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { AnalysisConfigDialog, DimensionHeatmap, PresetFetchState, type AnalysisFieldConfig } from '@/components/analysis-shared'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { RpsRotationDialog } from '@/components/RpsRotationDialog'
import { api, type MarketSnapshotRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'
import { resolveDimension, type DimensionGroup, type StockRow } from '@/lib/analysis-adapter'

const KEYWORDS = ['industry', '行业', 'sector', '申万', '中信']
const CANDIDATE_FIELDS = ['industry', '行业', 'sector', '申万', '中信', '行业名称', 'industry_name', 'sector_name']
const PAGE_LIMIT = 12000
const MAX_RENDERED_INDUSTRIES = 120
const MAX_RENDERED_STOCKS = 160

type SortMode = 'heat' | 'avgPct' | 'leader' | 'amount' | 'down'
type IndustryLevel = 1 | 2 | 3

interface EnrichedStock extends MarketSnapshotRow {
  leaderScore: number
  leaderParts: {
    momentum: number
    turnover: number
    amount: number
    cap: number
    volume: number
    boards: number
  }
}

interface IndustryStat {
  key: string
  stocks: EnrichedStock[]
  count: number
  avgPct: number | null
  medianPct: number | null
  upCount: number
  downCount: number
  flatCount: number
  upRate: number
  strongCount: number
  weakCount: number
  totalAmount: number
  avgTurnover: number | null
  avgVolRatio: number | null
  leader: EnrichedStock | null
  heatScore: number
  riskScore: number
}

// ===== 配置持久化 =====

function loadConfig(): AnalysisFieldConfig {
  return storage.industryAnalysisConfig.get({}) as AnalysisFieldConfig
}
function saveConfig(c: AnalysisFieldConfig) {
  storage.industryAnalysisConfig.set(c)
}

// ===== 自动选取最佳数据源 =====

function pickBestConfig(
  configs: { id: string; label: string; description?: string; fields: { name: string; label: string }[] }[],
): string {
  let best = '', bestScore = 0
  for (const c of configs) {
    const haystack = [c.id, c.label, c.description ?? '', ...c.fields.flatMap(f => [f.name, f.label])].join(' ').toLowerCase()
    const score = KEYWORDS.reduce((n, k) => n + (haystack.includes(k) ? 1 : 0), 0)
    if (score > bestScore) { bestScore = score; best = c.id }
  }
  return best
}

// ===== 工具函数 =====

function symbolKeys(symbol: unknown): string[] {
  const raw = String(symbol ?? '').trim()
  if (!raw) return []
  const plain = raw.replace(/\.\w+$/, '')
  return Array.from(new Set([raw, plain]))
}

function buildMarketMap(rows: MarketSnapshotRow[]) {
  const map = new Map<string, MarketSnapshotRow>()
  for (const r of rows) {
    for (const key of symbolKeys(r.symbol)) map.set(key, r)
  }
  return map
}

function clamp01(v: number) {
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function avg(values: number[]) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null
}

function median(values: number[]) {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

// ===== 龙头算法 =====

function leaderScore(stock: MarketSnapshotRow) {
  const pct = num(stock.change_pct) ?? 0
  const turnover = num(stock.turnover_rate) ?? 0
  const amount = num(stock.amount) ?? 0
  const cap = num(stock.float_market_cap) ?? num(stock.market_cap) ?? 0
  const volRatio = num(stock.vol_ratio_5d) ?? 1
  const boards = num(stock.consecutive_limit_ups) ?? 0

  const parts = {
    momentum: clamp01((pct + 0.02) / 0.12),
    turnover: clamp01(Math.log1p(Math.max(turnover, 0)) / Math.log1p(30)),
    amount: clamp01(Math.log1p(Math.max(amount, 0)) / Math.log1p(20_000_000_000)),
    cap: clamp01(Math.log1p(Math.max(cap, 0)) / Math.log1p(300_000_000_000)),
    volume: clamp01((volRatio - 1) / 4),
    boards: clamp01(boards / 5),
  }

  const score = (
    parts.momentum * 0.35 +
    parts.turnover * 0.22 +
    parts.amount * 0.18 +
    parts.cap * 0.15 +
    parts.volume * 0.07 +
    parts.boards * 0.03
  ) * 100

  return { score, parts }
}

function enrichStock(stock: StockRow, marketMap: Map<string, MarketSnapshotRow>): EnrichedStock {
  const market = symbolKeys(stock.symbol ?? stock.code).map(k => marketMap.get(k)).find(Boolean) ?? {}
  const merged = { ...stock, ...market } as MarketSnapshotRow & StockRow
  const ls = leaderScore(merged)
  return {
    ...merged,
    symbol: String(merged.symbol ?? stock.symbol ?? stock.code ?? ''),
    name: merged.name ?? String(stock.name ?? stock['股票简称'] ?? ''),
    leaderScore: ls.score,
    leaderParts: ls.parts,
  }
}

// ===== 行业统计计算 =====

function calcIndustryStat(group: DimensionGroup, marketMap: Map<string, MarketSnapshotRow>): IndustryStat {
  const seen = new Set<string>()
  const stocks = group.stocks
    .map(s => enrichStock(s, marketMap))
    .filter(s => {
      const key = String(s.symbol ?? '')
      if (!key) return false
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })

  const pctValues = stocks.map(s => num(s.change_pct)).filter((v): v is number => v != null)
  const turnoverValues = stocks.map(s => num(s.turnover_rate)).filter((v): v is number => v != null)
  const volValues = stocks.map(s => num(s.vol_ratio_5d)).filter((v): v is number => v != null)
  const totalAmount = stocks.reduce((sum, s) => sum + (num(s.amount) ?? 0), 0)
  const upCount = pctValues.filter(v => v > 0).length
  const downCount = pctValues.filter(v => v < 0).length
  const flatCount = Math.max(0, stocks.length - upCount - downCount)
  const strongCount = pctValues.filter(v => v >= 0.05).length
  const weakCount = pctValues.filter(v => v <= -0.05).length
  const leader = stocks.length ? [...stocks].sort((a, b) => b.leaderScore - a.leaderScore)[0] : null
  const avgPct = avg(pctValues)
  const medianPct = median(pctValues)
  const upRate = pctValues.length ? upCount / pctValues.length : 0
  const amountScore = clamp01(Math.log1p(totalAmount) / Math.log1p(80_000_000_000))
  const strongScore = stocks.length ? clamp01(strongCount / Math.max(1, stocks.length * 0.18)) : 0
  const leaderPart = clamp01((leader?.leaderScore ?? 0) / 100)
  const avgPart = clamp01(((avgPct ?? 0) + 0.02) / 0.09)
  const upPart = clamp01((upRate - 0.35) / 0.55)

  const heatScore = (avgPart * 0.38 + upPart * 0.2 + strongScore * 0.16 + amountScore * 0.12 + leaderPart * 0.14) * 100
  const riskScore = (clamp01((-(avgPct ?? 0) + 0.01) / 0.08) * 0.55 + clamp01(weakCount / Math.max(1, stocks.length * 0.18)) * 0.45) * 100

  return {
    key: group.key,
    stocks,
    count: stocks.length,
    avgPct,
    medianPct,
    upCount,
    downCount,
    flatCount,
    upRate,
    strongCount,
    weakCount,
    totalAmount,
    avgTurnover: avg(turnoverValues),
    avgVolRatio: avg(volValues),
    leader,
    heatScore,
    riskScore,
  }
}

function statSort(mode: SortMode) {
  return (a: IndustryStat, b: IndustryStat) => {
    switch (mode) {
      case 'avgPct': return (b.avgPct ?? -Infinity) - (a.avgPct ?? -Infinity)
      case 'leader': return (b.leader?.leaderScore ?? -Infinity) - (a.leader?.leaderScore ?? -Infinity)
      case 'amount': return b.totalAmount - a.totalAmount
      case 'down': return (a.avgPct ?? Infinity) - (b.avgPct ?? Infinity)
      case 'heat':
      default: return b.heatScore - a.heatScore
    }
  }
}

// ===== 行业层级分组 =====

function industryLevelName(key: string, level: IndustryLevel) {
  const parts = key.split('-').map(s => s.trim()).filter(Boolean)
  return parts[level - 1] || parts[parts.length - 1] || key
}

function groupByIndustryLevel(groups: DimensionGroup[], level: IndustryLevel): DimensionGroup[] {
  const map = new Map<string, DimensionGroup>()
  for (const group of groups) {
    const key = industryLevelName(group.key, level)
    const existing = map.get(key)
    if (existing) {
      existing.stocks.push(...group.stocks)
      existing.count = existing.stocks.length
    } else {
      map.set(key, {
        key,
        count: group.stocks.length,
        stocks: [...group.stocks],
        metrics: { ...group.metrics },
      })
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count)
}

// ===== 主页面 =====

export function IndustryAnalysis() {
  const [fieldConfig, setFieldConfig] = useState<AnalysisFieldConfig>(loadConfig)
  const [showConfig, setShowConfig] = useState(false)
  const [search, setSearch] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [sortMode, setSortMode] = useState<SortMode>('heat')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const [showRps, setShowRps] = useState(false)

  const configsQuery = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const availableConfigs = configsQuery.data?.items ?? []
  // 用户配置的 configId 可能已失效 (扩展数据被删除), 此时回退到自动选择,
  // 避免用失效 ID 请求接口报错; 用户仍可点配置按钮重新选择。
  const preferredConfigId = fieldConfig.configId || pickBestConfig(availableConfigs)
  const preferredConfig = availableConfigs.find(c => c.id === preferredConfigId)
  const activeConfigId = preferredConfig ? preferredConfigId : pickBestConfig(availableConfigs)
  const activeConfig = availableConfigs.find(c => c.id === activeConfigId)

  const rowsQuery = useQuery({
    queryKey: QK.extDataRows(activeConfigId, undefined, PAGE_LIMIT),
    queryFn: () => api.extDataRows(activeConfigId, { limit: PAGE_LIMIT }),
    enabled: !!activeConfigId,
  })

  // 内置行业预设 (ext_hy_ths) 手动获取数据
  const PRESET_INDUSTRY_ID = 'ext_hy_ths'
  const queryClient = useQueryClient()
  const fetchMutation = useMutation({
    mutationFn: () => api.extDataPresetFetch(PRESET_INDUSTRY_ID),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.extData })
      queryClient.invalidateQueries({ queryKey: QK.extDataRows(PRESET_INDUSTRY_ID, undefined, PAGE_LIMIT) })
    },
  })
  // 是否处于「内置行业预设存在但无数据」状态 → 显示获取按钮
  const needsIndustryFetch =
    !!activeConfig && activeConfig.id === PRESET_INDUSTRY_ID &&
    !rowsQuery.isLoading && (rowsQuery.data?.total ?? 0) === 0

  const marketQuery = useQuery({
    queryKey: QK.marketSnapshot,
    queryFn: api.marketSnapshot,
    staleTime: 60_000,
  })

  const marketMap = useMemo(() => buildMarketMap(marketQuery.data?.rows ?? []), [marketQuery.data?.rows])
  const resolved = useMemo(
    () => resolveDimension(rowsQuery.data, activeConfig, fieldConfig.dimensionField ? [fieldConfig.dimensionField, ...CANDIDATE_FIELDS] : CANDIDATE_FIELDS),
    [rowsQuery.data, activeConfig, fieldConfig.dimensionField],
  )

  const industryLevel = fieldConfig.hierarchyLevel ?? 2
  const groups = useMemo(() => groupByIndustryLevel(resolved.groups, industryLevel), [resolved.groups, industryLevel])

  const stats = useMemo(() => {
    return groups
      .map(g => calcIndustryStat(g, marketMap))
      .filter(s => s.count > 0)
  }, [groups, marketMap])

  const filteredStats = useMemo(() => {
    const q = search.trim().toLowerCase()
    const base = q ? stats.filter(s => s.key.toLowerCase().includes(q)) : stats
    return [...base].sort(statSort(sortMode))
  }, [stats, search, sortMode])

  const selected = filteredStats.find(s => s.key === selectedKey) ?? filteredStats[0] ?? null
  const leading = useMemo(() => [...stats].sort(statSort('heat')).slice(0, 10), [stats])
  const falling = useMemo(() => [...stats].sort(statSort('down')).slice(0, 10), [stats])
  const activeIndustry = useMemo(() => [...stats].sort(statSort('amount'))[0] ?? null, [stats])
  const industryBreadth = useMemo(() => {
    const priced = stats.filter(s => s.avgPct != null)
    return {
      up: priced.filter(s => (s.avgPct ?? 0) > 0).length,
      down: priced.filter(s => (s.avgPct ?? 0) < 0).length,
      flat: priced.filter(s => s.avgPct === 0).length,
    }
  }, [stats])

  const totalSymbols = useMemo(() => {
    const set = new Set<string>()
    stats.forEach(s => s.stocks.forEach(st => { if (st.symbol) set.add(st.symbol) }))
    return set.size
  }, [stats])

  // 热力图用的 quoteMap（兼容 DimensionHeatmap 组件接口）
  const heatmapQuoteMap = useMemo(() => {
    const map = new Map<string, { symbol: string; pct?: number; change_pct?: number; name?: string; [k: string]: unknown }>()
    for (const [k, v] of marketMap) {
      map.set(k, {
        ...v,
        change_pct: v.change_pct ?? undefined,
        name: v.name ?? undefined,
      })
    }
    return map
  }, [marketMap])

  const handleSaveConfig = (c: AnalysisFieldConfig) => {
    setFieldConfig(c)
    saveConfig(c)
    setSelectedKey(null)
  }

  if (configsQuery.isLoading) {
    return <div className="flex h-full items-center justify-center"><RefreshCw className="h-5 w-5 animate-spin text-muted" /></div>
  }

  if (!activeConfig) {
    // 极端情况: 无任何行业配置。仍提供一键获取内置行业数据入口
    return (
      <>
        <div className="flex h-full flex-col">
          <PageHeader
            title="行业分析"
            right={
              <button onClick={() => setShowConfig(true)} className="p-1.5 text-muted hover:bg-surface hover:text-accent" title="配置数据源">
                <Settings2 className="h-4 w-4" />
              </button>
            }
          />
          <PresetFetchState
            title="暂无行业数据"
            hint="从同花顺获取行业分类数据后即可使用行业分析"
            isLoading={fetchMutation.isPending}
            error={fetchMutation.error}
            onFetch={() => fetchMutation.mutate()}
          />
        </div>
        <AnimatePresence>
          {showConfig && <AnalysisConfigDialog currentConfig={fieldConfig} onSave={handleSaveConfig} onClose={() => setShowConfig(false)} showHierarchyLevel />}
        </AnimatePresence>
      </>
    )
  }

  const industryLevelLabel = `${industryLevel}级行业`

  return (
    <>
      <PageHeader
        title="行业分析"
        subtitle={`${industryLevelLabel} · ${marketQuery.data?.as_of ?? rowsQuery.data?.date ?? '最新'} · ${stats.length} 个行业 · ${totalSymbols} 只标的`}
        right={
          <div className="flex items-center gap-1">
            {/* RPS 轮动: 打开行业涨幅轮动矩阵对话框 */}
            <button
              onClick={() => setShowRps(true)}
              className="inline-flex items-center gap-1 rounded-btn border border-amber-400/40 bg-amber-400/15 px-2.5 py-1.5 text-[11px] text-amber-400 font-medium transition-colors hover:bg-amber-400/25 hover:border-amber-400/60"
              title="行业涨幅轮动矩阵"
            >
              <Repeat className="h-3.5 w-3.5" />涨幅RPS轮动分析
            </button>
            <button
              onClick={() => { rowsQuery.refetch(); marketQuery.refetch() }}
              disabled={rowsQuery.isFetching || marketQuery.isFetching}
              className="p-1.5 text-muted hover:bg-surface disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw className={cn('h-4 w-4', (rowsQuery.isFetching || marketQuery.isFetching) && 'animate-spin')} />
            </button>
            <button onClick={() => setShowConfig(true)} className="p-1.5 text-muted hover:bg-surface hover:text-accent" title="配置数据源">
              <Settings2 className="h-4 w-4" />
            </button>
          </div>
        }
      />

      <div className="min-h-full bg-[radial-gradient(circle_at_12%_0%,rgba(245,158,11,0.12),transparent_28%),radial-gradient(circle_at_85%_8%,rgba(244,63,94,0.08),transparent_28%)] px-6 py-5">
        <div className="mx-auto max-w-[1440px] space-y-5">
          <HeroPanel leading={leading[0]} falling={falling[0]} activeIndustry={activeIndustry} industryBreadth={industryBreadth} />

          <MarketPulse
            leading={leading}
            falling={falling}
            selectedKey={selected?.key ?? null}
            onSelect={setSelectedKey}
            onStockClick={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name ?? '') }}
          />

          {/* 热力图 */}
          {groups.length > 0 && (
            <DimensionHeatmap
              groups={groups}
              quoteMap={heatmapQuoteMap}
              selectedKey={selectedKey}
              onSelect={k => setSelectedKey(k)}
              colorScheme="amber"
            />
          )}

          {stats.length > 0 ? (
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[18rem_1fr]">
              <IndustryRail
                stats={filteredStats.slice(0, MAX_RENDERED_INDUSTRIES)}
                selectedKey={selected?.key ?? null}
                search={search}
                sortMode={sortMode}
                onSearch={v => { setSearch(v); setSelectedKey(null) }}
                onSort={setSortMode}
                onSelect={setSelectedKey}
              />
              <IndustryFocus stat={selected} onStockClick={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name ?? '') }} />
            </div>
          ) : rowsQuery.isLoading ? (
            <div className="rounded-2xl border border-border bg-surface px-6 py-16 text-center text-sm text-muted">正在计算行业强度...</div>
          ) : needsIndustryFetch ? (
            <PresetFetchState
              title="未获取行业数据"
              hint="内置行业数据源已就绪,点击下方按钮从同花顺获取行业分类数据"
              isLoading={fetchMutation.isPending}
              error={fetchMutation.error}
              onFetch={() => fetchMutation.mutate()}
            />
          ) : (
            <EmptyState icon={Layers3} title="未匹配到行业数据" hint={resolved.hint || '请检查扩展数据是否包含行业/板块相关字段'} />
          )}
        </div>
      </div>

      <AnimatePresence>
        {showConfig && <AnalysisConfigDialog currentConfig={fieldConfig} onSave={handleSaveConfig} onClose={() => setShowConfig(false)} showHierarchyLevel />}
      </AnimatePresence>

      {previewSymbol && (
        <StockPreviewDialog
          symbol={previewSymbol}
          name={previewName}
          onClose={() => { setPreviewSymbol(null); setPreviewName('') }}
        />
      )}
      {showRps && <RpsRotationDialog onClose={() => setShowRps(false)} kind="industry" />}
    </>
  )
}

// ===== HeroPanel =====

function HeroPanel({
  leading,
  falling,
  activeIndustry,
  industryBreadth,
}: {
  leading?: IndustryStat
  falling?: IndustryStat
  activeIndustry?: IndustryStat | null
  industryBreadth: { up: number; down: number; flat: number }
}) {
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
      <HeroMetric icon={TrendingUp} label="最强行业" value={leading?.key ?? '—'} hint={leading?.avgPct != null ? <span className={priceColorClass(leading.avgPct)}>{fmtPct(leading.avgPct)}</span> : '等待行情'} tone="up" />
      <HeroMetric icon={TrendingDown} label="最大风险" value={falling?.key ?? '—'} hint={falling?.avgPct != null ? <span className={priceColorClass(falling.avgPct)}>{fmtPct(falling.avgPct)}</span> : '等待行情'} tone="down" />
      <HeroMetric
        icon={Activity}
        label="涨跌行业"
        value={<><span className="text-bull">{industryBreadth.up}</span><span className="mx-1 text-muted">/</span><span className="text-bear">{industryBreadth.down}</span></>}
        hint={<><span className="text-bull">上涨</span><span className="mx-1 text-muted">/</span><span className="text-bear">下跌</span>{industryBreadth.flat ? <span className="text-muted"> · 平 {industryBreadth.flat}</span> : null}</>}
        tone="blue"
      />
      <HeroMetric icon={Activity} label="资金活跃" value={activeIndustry?.key ?? '—'} hint={activeIndustry ? fmtBigNum(activeIndustry.totalAmount) : '等待行情'} tone="blue" />
      <HeroMetric icon={Crown} label="龙头算法" value="6 因子" hint="强势 + 承接 + 容量" tone="gold" />
    </div>
  )
}

function HeroMetric({ icon: Icon, label, value, hint, tone }: {
  icon: typeof TrendingUp
  label: string
  value: ReactNode
  hint: ReactNode
  tone: 'up' | 'down' | 'gold' | 'blue'
}) {
  const toneClass = {
    up: 'text-bull bg-bull/10',
    down: 'text-bear bg-bear/10',
    gold: 'text-amber-300 bg-amber-400/10',
    blue: 'text-blue-300 bg-blue-400/10',
  }[tone]
  const valueClass = {
    up: 'text-bull',
    down: 'text-bear',
    gold: 'text-amber-300',
    blue: 'text-foreground',
  }[tone]
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-2">
      <div className="flex items-center justify-between text-[11px] text-muted">
        <span>{label}</span>
        <span className={cn('rounded-md p-1', toneClass)}><Icon className="h-3.5 w-3.5" /></span>
      </div>
      <div className={cn('mt-1 truncate text-sm font-semibold', valueClass)}>{value}</div>
      <div className="mt-0.5 truncate text-[11px] text-muted">{hint}</div>
    </div>
  )
}

// ===== MarketPulse =====

function MarketPulse({
  leading,
  falling,
  selectedKey,
  onSelect,
  onStockClick,
}: {
  leading: IndustryStat[]
  falling: IndustryStat[]
  selectedKey: string | null
  onSelect: (key: string) => void
  onStockClick: (symbol: string, name?: string) => void
}) {
  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      <PulseList title="领涨主线" items={leading} mode="up" selectedKey={selectedKey} onSelect={onSelect} onStockClick={onStockClick} />
      <PulseList title="领跌方向" items={falling} mode="down" selectedKey={selectedKey} onSelect={onSelect} onStockClick={onStockClick} />
    </div>
  )
}

function PulseList({
  title,
  items,
  mode,
  selectedKey,
  onSelect,
  onStockClick,
}: {
  title: string
  items: IndustryStat[]
  mode: 'up' | 'down'
  selectedKey: string | null
  onSelect: (key: string) => void
  onStockClick: (symbol: string, name?: string) => void
}) {
  const toneText = mode === 'up' ? 'text-bull' : 'text-bear'
  const toneBorder = mode === 'up' ? 'border-bull/20' : 'border-bear/20'
  const toneBg = mode === 'up' ? 'bg-bull/10' : 'bg-bear/10'
  const toneHover = mode === 'up' ? 'hover:border-bull/35' : 'hover:border-bear/35'

  return (
    <div className={cn('rounded-xl border bg-base/35 p-2', toneBorder)}>
      <div className="mb-1.5 flex items-center justify-between px-1">
        <div className={cn('flex items-center gap-1.5 text-xs font-medium', toneText)}>
          {mode === 'up' ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
          {title}
        </div>
        <span className="rounded-full bg-elevated/60 px-2 py-0.5 text-[10px] text-muted">Top 10</span>
      </div>
      <div className="space-y-1">
        {items.map((item, idx) => {
          const active = selectedKey === item.key
          const leaders = [...item.stocks].sort((a, b) => b.leaderScore - a.leaderScore).slice(0, 3)
          const upPct = item.count > 0 ? (item.upCount / item.count) * 100 : 0
          const downPct = item.count > 0 ? (item.downCount / item.count) * 100 : 0
          const flatPct = Math.max(0, 100 - upPct - downPct)
          return (
            <button
              key={item.key}
              onClick={() => onSelect(item.key)}
              className={cn(
                'block w-full rounded-lg border border-transparent bg-surface/45 px-2 py-1.5 text-left transition-colors',
                active ? cn('bg-blue-400/[0.08]', toneBorder) : cn('hover:bg-elevated/35', toneHover),
              )}
            >
              <div className="grid gap-2 md:grid-cols-[minmax(0,0.9fr)_minmax(16rem,1.1fr)] md:items-center">
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className={cn('flex h-5 w-5 shrink-0 items-center justify-center rounded-md font-mono text-[10px]', idx < 3 ? cn(toneBg, toneText) : 'bg-elevated/70 text-muted')}>{idx + 1}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="truncate text-xs font-medium text-foreground">{item.key}</span>
                        <span className="shrink-0 text-[10px] text-muted">
                          <span className="text-bull">{item.upCount}</span>涨
                          <span className="mx-0.5 text-muted/40">/</span>
                          <span className="text-bear">{item.downCount}</span>跌
                        </span>
                        <span className={cn('ml-auto shrink-0 font-mono text-[10px] tabular-nums', priceColorClass(item.avgPct))}>{item.avgPct != null ? fmtPct(item.avgPct) : '—'}</span>
                      </div>
                    </div>
                  </div>
                  <div className="ml-7 mt-1">
                    <div className="flex h-1 overflow-hidden rounded-full bg-elevated">
                      <div className="h-full bg-bull/70" style={{ width: `${upPct}%` }} />
                      {flatPct > 0 && <div className="h-full bg-muted/25" style={{ width: `${flatPct}%` }} />}
                      <div className="h-full bg-bear/70" style={{ width: `${downPct}%` }} />
                    </div>
                  </div>
                </div>
                <div className="grid min-w-0 grid-cols-3 gap-1">
                  {Array.from({ length: 3 }).map((_, i) => {
                    const stock = leaders[i]
                    return stock ? (
                      <span key={stock.symbol} title={stock.name || stock.symbol} onClick={e => { e.stopPropagation(); onStockClick(stock.symbol, stock.name || undefined) }} className={cn('flex min-w-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] cursor-pointer hover:brightness-125', i === 0 ? 'bg-amber-300/10 text-foreground' : 'bg-elevated/60 text-secondary')}>
                        <span className="flex min-w-0 items-center gap-1">
                          <span className="min-w-0 truncate font-medium">{stock.name || stock.symbol}</span>
                        </span>
                        <span className={cn('shrink-0 font-mono', priceColorClass(stock.change_pct))}>{stock.change_pct != null ? fmtPct(stock.change_pct) : '—'}</span>
                      </span>
                    ) : <span key={i} className="rounded-md bg-elevated/30 px-1.5 py-0.5 text-[10px] text-muted/40">—</span>
                  })}
                </div>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ===== IndustryRail（左侧列表面板） =====

function IndustryRail({
  stats,
  selectedKey,
  search,
  sortMode,
  onSearch,
  onSort,
  onSelect,
}: {
  stats: IndustryStat[]
  selectedKey: string | null
  search: string
  sortMode: SortMode
  onSearch: (v: string) => void
  onSort: (v: SortMode) => void
  onSelect: (v: string) => void
}) {
  return (
    <section className="rounded-2xl border border-border bg-surface p-2.5">
      <div className="px-1 pb-2.5">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-foreground">行业矩阵</h3>
          <span className="text-[10px] text-muted">Top {stats.length}</span>
        </div>
        <div className="mt-2 relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input value={search} onChange={e => onSearch(e.target.value)} placeholder="搜索行业" className="h-8 w-full rounded-lg border border-border bg-base pl-8 pr-3 text-xs text-foreground outline-none focus:border-accent/50" />
        </div>
        <div className="mt-2 grid grid-cols-5 overflow-hidden rounded-lg border border-border text-[10px]">
          {([
            ['heat', '强度'], ['avgPct', '涨幅'], ['leader', '龙头'], ['amount', '成交'], ['down', '跌幅'],
          ] as [SortMode, string][]).map(([key, label]) => (
            <button key={key} onClick={() => onSort(key)} className={cn('py-1.5 transition-colors', sortMode === key ? 'bg-amber-500/15 text-amber-400' : 'bg-base text-muted hover:text-foreground')}>{label}</button>
          ))}
        </div>
      </div>
      <div className="max-h-[620px] overflow-auto rounded-lg border border-border/50">
        {stats.map(item => {
          const active = selectedKey === item.key
          return (
            <button key={item.key} onClick={() => onSelect(item.key)} className={cn('w-full border-b border-border/50 px-2.5 py-2 text-left transition-colors last:border-b-0', active ? 'bg-amber-500/[0.08]' : 'hover:bg-elevated/40')}>
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">{item.key}</span>
                <span className={cn('font-mono text-xs', priceColorClass(item.avgPct))}>{item.avgPct != null ? fmtPct(item.avgPct) : '—'}</span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-[10px] text-muted">
                <span>{item.count}只</span>
                <span className="text-bull">{item.upCount}涨</span>
                <span className="text-bear">{item.downCount}跌</span>
                <span className="ml-auto">强度 {item.heatScore.toFixed(0)}</span>
              </div>
            </button>
          )
        })}
      </div>
    </section>
  )
}

// ===== IndustryFocus（右侧聚焦面板） =====

function IndustryFocus({ stat, onStockClick }: { stat: IndustryStat | null; onStockClick: (symbol: string, name?: string) => void }) {
  if (!stat) return null
  const stocks = [...stat.stocks].sort((a, b) => b.leaderScore - a.leaderScore).slice(0, MAX_RENDERED_STOCKS)
  const topLeaders = stocks.slice(0, 3)
  return (
    <section className="flex max-h-[720px] flex-col overflow-hidden rounded-2xl border border-border bg-surface">
      <div className="shrink-0 border-b border-border px-5 py-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <h3 className="truncate text-xl font-semibold text-foreground">{stat.key}</h3>
              <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-400">强度 {stat.heatScore.toFixed(0)}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
              <span>{stat.count} 只成分</span>
              <span className={priceColorClass(stat.avgPct)}>平均 {stat.avgPct != null ? fmtPct(stat.avgPct) : '—'}</span>
              <span>上涨占比 {(stat.upRate * 100).toFixed(0)}%</span>
              <span>成交额 {fmtBigNum(stat.totalAmount)}</span>
              {stat.avgTurnover != null && <span>均换手 {stat.avgTurnover.toFixed(2)}%</span>}
            </div>
          </div>
          <div className="grid grid-cols-5 gap-2 lg:w-[520px]">
            <MiniStat label="均涨" value={stat.avgPct != null ? fmtPct(stat.avgPct) : '—'} cls={priceColorClass(stat.avgPct)} />
            <MiniStat label="中位" value={stat.medianPct != null ? fmtPct(stat.medianPct) : '—'} cls={priceColorClass(stat.medianPct)} />
            <MiniStat label="强势" value={`${stat.strongCount}`} cls="text-bull" />
            <MiniStat label="弱势" value={`${stat.weakCount}`} cls="text-bear" />
            <MiniStat label="量比" value={stat.avgVolRatio != null ? stat.avgVolRatio.toFixed(2) : '—'} cls="text-foreground" />
          </div>
        </div>
      </div>

      <div className="grid shrink-0 gap-3 border-b border-border bg-base/25 p-4 lg:grid-cols-[1fr_1.15fr]">
        <LeaderStage stocks={topLeaders} onStockClick={onStockClick} />
        <ScoreExplain stock={topLeaders[0]} />
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="bg-elevated/60 text-[11px] text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">排名</th>
              <th className="px-4 py-2 font-medium">股票</th>
              <th className="px-4 py-2 font-medium">涨跌幅</th>
              <th className="px-4 py-2 font-medium">换手率</th>
              <th className="px-4 py-2 font-medium">成交额</th>
              <th className="px-4 py-2 font-medium">流通市值</th>
              <th className="px-4 py-2 font-medium">量比</th>
              <th className="px-4 py-2 font-medium">龙头分</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/70">
            {stocks.map((s, idx) => (
              <tr key={`${s.symbol}-${idx}`} className="hover:bg-elevated/30 cursor-pointer" onClick={() => onStockClick(s.symbol, s.name || undefined)}>
                <td className="px-4 py-2 font-mono text-muted">{idx + 1}</td>
                <td className="px-4 py-2">
                  <div className="font-medium text-foreground">{s.name || '—'}</div>
                  <div className="font-mono text-[10px] text-muted">{s.symbol}</div>
                </td>
                <td className={cn('px-4 py-2 font-mono tabular-nums', priceColorClass(s.change_pct))}>{s.change_pct != null ? fmtPct(s.change_pct) : '—'}</td>
                <td className="px-4 py-2 font-mono text-foreground">{s.turnover_rate != null ? `${s.turnover_rate.toFixed(2)}%` : '—'}</td>
                <td className="px-4 py-2 font-mono text-foreground">{fmtBigNum(s.amount)}</td>
                <td className="px-4 py-2 font-mono text-foreground">{fmtBigNum(s.float_market_cap ?? s.market_cap)}</td>
                <td className="px-4 py-2 font-mono text-foreground">{s.vol_ratio_5d != null ? s.vol_ratio_5d.toFixed(2) : '—'}</td>
                <td className="px-4 py-2">
                  <div className="flex items-center gap-2">
                    <span className="w-9 font-mono text-amber-300">{s.leaderScore.toFixed(0)}</span>
                    <div className="h-1.5 w-16 rounded-full bg-elevated"><div className="h-full rounded-full bg-amber-300" style={{ width: `${Math.max(4, s.leaderScore)}%` }} /></div>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {stat.stocks.length > MAX_RENDERED_STOCKS && <div className="shrink-0 border-t border-border px-4 py-2 text-center text-[11px] text-muted">仅展示龙头分前 {MAX_RENDERED_STOCKS} 只，共 {stat.stocks.length} 只</div>}
    </section>
  )
}

function MiniStat({ label, value, cls }: { label: string; value: string; cls: string }) {
  return <div className="rounded-lg border border-border/60 bg-base/35 px-2 py-1.5"><div className="text-[10px] text-muted">{label}</div><div className={cn('mt-0.5 truncate text-sm font-semibold', cls)}>{value}</div></div>
}

function LeaderStage({ stocks, onStockClick }: { stocks: EnrichedStock[]; onStockClick: (symbol: string, name?: string) => void }) {
  if (!stocks.length) return <div className="rounded-xl border border-border/60 bg-surface p-4 text-sm text-muted">暂无龙头候选</div>
  return (
    <div className="rounded-xl border border-border/60 bg-surface p-3">
      <div className="mb-2 flex items-center gap-2 text-xs font-medium text-amber-300">
        <Crown className="h-3.5 w-3.5" />
        本行业三龙头
      </div>
      <div className="grid gap-2 md:grid-cols-3">
        {stocks.map((stock, idx) => (
          <div key={stock.symbol} onClick={() => onStockClick(stock.symbol, stock.name || undefined)} className={cn('rounded-lg border p-3 cursor-pointer hover:brightness-110 transition-all', idx === 0 ? 'border-amber-400/25 bg-amber-400/[0.06]' : 'border-border/60 bg-base/35')}>
            <div className="flex items-center justify-between gap-2">
              <span className={cn('text-[10px] font-medium', idx === 0 ? 'text-amber-300' : 'text-muted')}>{idx === 0 ? '主龙头' : `辅龙 ${idx}`}</span>
              <span className="font-mono text-[11px] text-amber-300">{stock.leaderScore.toFixed(0)}</span>
            </div>
            <div className="mt-2 truncate text-sm font-medium text-foreground">{stock.name || stock.symbol}</div>
            <div className="mt-0.5 flex items-center justify-between text-[11px]">
              <span className="font-mono text-muted">{stock.symbol}</span>
              <span className={cn('font-mono', priceColorClass(stock.change_pct))}>{stock.change_pct != null ? fmtPct(stock.change_pct) : '—'}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ScoreExplain({ stock }: { stock?: EnrichedStock }) {
  if (!stock) return <div className="rounded-xl border border-border/60 bg-surface p-4 text-sm text-muted">暂无评分拆解</div>
  const parts = stock.leaderParts
  return (
    <div className="rounded-xl border border-border/60 bg-surface p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-foreground">主龙头评分拆解</span>
        <span className="text-[11px] text-muted">涨幅 / 换手 / 成交 / 市值 / 量比 / 连板</span>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        <Part label="动能" value={parts.momentum} cls="bg-rose-400" />
        <Part label="换手" value={parts.turnover} cls="bg-orange-400" />
        <Part label="成交" value={parts.amount} cls="bg-blue-400" />
        <Part label="市值" value={parts.cap} cls="bg-cyan-400" />
        <Part label="量比" value={parts.volume} cls="bg-purple-400" />
        <Part label="连板" value={parts.boards} cls="bg-amber-300" />
      </div>
    </div>
  )
}

function Part({ label, value, cls }: { label: string; value: number; cls: string }) {
  return <div className="rounded-lg bg-base/35 px-2 py-1.5"><div className="mb-1 flex justify-between text-[10px] text-muted"><span>{label}</span><span>{Math.round(value * 100)}</span></div><div className="h-1 rounded-full bg-elevated"><div className={cn('h-full rounded-full', cls)} style={{ width: `${Math.max(3, value * 100)}%` }} /></div></div>
}
