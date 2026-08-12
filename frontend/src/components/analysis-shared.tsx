/**
 * 概念/行业分析 — 共享 UI 组件
 *
 * 包含：
 * - AnalysisConfigDialog: 字段配置弹窗
 * - DimensionHeatmap: 维度热力图块
 * - OverviewStatCards: 总览统计卡片
 * - DimensionGroupSidebar: 维度分组侧边栏
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import {
  AlertCircle,
  BarChart3,
  ChevronDown,
  Database,
  DownloadCloud,
  RefreshCw,
  Search,
  Settings2,
  Tags,
  TrendingUp,
  X,
} from 'lucide-react'
import { api, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import type { DimensionGroup, QuoteMap } from '@/lib/analysis-adapter'
import { computeQuoteMetrics } from '@/lib/analysis-adapter'
import { fmtPct, priceColorClass } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

// ===== 配置类型 =====

export interface AnalysisFieldConfig {
  /** 选中的扩展数据源 ID */
  configId?: string
  /** 手动指定的维度字段（覆盖自动探测） */
  dimensionField?: string
  /** 层级字段统计级别（如行业 1/2/3 级） */
  hierarchyLevel?: 1 | 2 | 3
}

export interface SchemaOption {
  id: string
  label: string
  columns: { name: string; type: string; label: string }[]
}

// ===== 配置弹窗 =====

export function AnalysisConfigDialog({
  currentConfig,
  onSave,
  onClose,
  showHierarchyLevel = false,
}: {
  currentConfig: AnalysisFieldConfig
  onSave: (config: AnalysisFieldConfig) => void
  onClose: () => void
  showHierarchyLevel?: boolean
}) {
  const [draft, setDraft] = useState<AnalysisFieldConfig>(currentConfig)
  const backdrop = useDialogBackdrop(onClose)
  const { data: extList } = useQuery({
    queryKey: QK.extData,
    queryFn: api.extDataList,
  })
  const { data: schemaData } = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
  })

  const configs = extList?.items ?? []

  const fieldsForConfig = useMemo((): ExtDataField[] => {
    if (!draft.configId || !schemaData?.items) return []
    const schema = schemaData.items.find(s => s.id === draft.configId)
    return schema?.columns.map(c => ({ name: c.name, dtype: c.type, label: c.label })) ?? []
  }, [draft.configId, schemaData])

  const nonMetaFields = fieldsForConfig.filter(
    f => !['symbol', 'code', 'date', 'name'].includes(f.name),
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" {...backdrop}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-surface border border-border rounded-lg shadow-xl w-[420px]"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 pt-3 pb-2">
          <span className="text-sm font-medium">配置数据源</span>
          <button onClick={onClose} className="p-0.5 text-muted hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="px-4 pb-4 space-y-3">
          {/* 数据源选择 */}
          <div className="space-y-1.5">
            <span className="text-xs text-secondary">扩展数据源</span>
            <select
              value={draft.configId ?? ''}
              onChange={e => setDraft(d => ({ ...d, configId: e.target.value || undefined, dimensionField: undefined }))}
              className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
            >
              <option value="">自动选择</option>
              {configs.map(c => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
          </div>

          {/* 维度字段选择 */}
          {nonMetaFields.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-xs text-secondary">维度字段（留空自动探测）</span>
              <select
                value={draft.dimensionField ?? ''}
                onChange={e => setDraft(d => ({ ...d, dimensionField: e.target.value || undefined }))}
                className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
              >
                <option value="">自动探测</option>
                {nonMetaFields.map(f => (
                  <option key={f.name} value={f.name}>{f.label || f.name}</option>
                ))}
              </select>
            </div>
          )}

          {showHierarchyLevel && (
            <div className="space-y-1.5">
              <span className="text-xs text-secondary">统计层级</span>
              <select
                value={draft.hierarchyLevel ?? 2}
                onChange={e => setDraft(d => ({ ...d, hierarchyLevel: Number(e.target.value) as 1 | 2 | 3 }))}
                className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
              >
                <option value={1}>一级行业</option>
                <option value={2}>二级行业（默认）</option>
                <option value={3}>三级行业</option>
              </select>
            </div>
          )}

          <div className="text-[10px] text-muted leading-relaxed">
            系统自动检测数据结构：支持"个股→概念/行业"和"概念/行业→成分股列表"两种模式。
            {showHierarchyLevel ? ' 行业字段按 “-” 拆分为 1/2/3 级。' : ''}
          </div>
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-border">
          <button onClick={onClose} className="px-3 py-1.5 text-xs text-secondary hover:text-foreground">取消</button>
          <button
            onClick={() => { onSave(draft); onClose() }}
            className="px-3 py-1.5 text-xs bg-accent/15 text-accent rounded hover:bg-accent/25"
          >
            保存
          </button>
        </div>
      </motion.div>
    </div>
  )
}

// ===== 总览统计卡片 =====

export function OverviewStatCards({
  groups,
  totalStocks,
  configLabel,
  dateStr,
  accentColor,
}: {
  groups: DimensionGroup[]
  totalStocks: number
  configLabel: string
  dateStr?: string | null
  accentColor: string
}) {
  const cards = [
    { label: '数据源', value: configLabel, hint: dateStr ?? '快照', icon: Database },
    { label: '维度数量', value: groups.length, hint: '按维度聚合分组', icon: Tags },
    { label: '覆盖标的', value: totalStocks, hint: '去重股票数', icon: BarChart3 },
    {
      label: '最大分组',
      value: groups[0]?.key ?? '—',
      hint: groups[0] ? `${groups[0].count} 只` : '',
      icon: TrendingUp,
    },
  ]

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {cards.map(card => (
        <div key={card.label} className="rounded-card border border-border bg-surface p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted">{card.label}</span>
            <card.icon className="h-4 w-4" style={{ color: accentColor }} />
          </div>
          <div className="mt-2 text-xl font-semibold tracking-tight text-foreground truncate">
            {card.value}
          </div>
          <div className="mt-1 text-[11px] text-muted truncate">{card.hint}</div>
        </div>
      ))}
    </div>
  )
}

// ===== 维度热力图 =====

export function DimensionHeatmap({
  groups,
  quoteMap,
  selectedKey,
  onSelect,
  colorScheme,
}: {
  groups: DimensionGroup[]
  quoteMap: Map<string, QuoteMap>
  selectedKey: string | null
  onSelect: (key: string | null) => void
  colorScheme: 'blue' | 'amber'
}) {
  const [showAll, setShowAll] = useState(false)
  // 收起时最大高度（约 3 行标签高度）
  const collapsedMaxH = '8rem'

  const enriched = useMemo(() => {
    return groups.map(g => {
      const qm = computeQuoteMetrics(g.stocks, quoteMap)
      return { ...g, quoteMetrics: qm }
    })
  }, [groups, quoteMap])

  // 根据涨跌比渲染颜色强度
  const colors = colorScheme === 'blue'
    ? { up: [59, 130, 246], down: [96, 165, 250], bg: [30, 64, 175] }
    : { up: [245, 158, 11], down: [251, 191, 36], bg: [180, 83, 9] }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-muted">热度分布（按标的覆盖数）</span>
        <button
          onClick={() => setShowAll(v => !v)}
          className="text-[10px] text-muted hover:text-foreground flex items-center gap-0.5"
        >
          {showAll ? '收起' : `展开全部 (${enriched.length})`}
          <ChevronDown className={`h-3 w-3 transition-transform ${showAll ? 'rotate-180' : ''}`} />
        </button>
      </div>
      <div
        className="flex flex-wrap gap-1.5 overflow-hidden transition-[max-height] duration-200"
        style={{ maxHeight: showAll ? 'none' : collapsedMaxH }}
      >
        {enriched.map(g => {
          const qm = g.quoteMetrics
          const total = qm.upCount + qm.downCount + qm.flatCount
          const upRatio = total > 0 ? qm.upCount / total : 0.5
          const size = Math.max(0.6, Math.min(1.4, g.count / (enriched[0]?.count || 1)))
          const active = selectedKey === g.key
          const [r, gr, b] = colors.up

          return (
            <button
              key={g.key}
              onClick={() => onSelect(active ? null : g.key)}
              className="px-2.5 py-1.5 rounded-sm text-[11px] whitespace-nowrap cursor-pointer hover:brightness-125 transition-all"
              style={{
                fontSize: `${10 + size * 2}px`,
                color: active ? '#fff' : `rgba(${r},${gr},${b},${0.6 + upRatio * 0.4})`,
                backgroundColor: active
                  ? `rgba(${r},${gr},${b},0.7)`
                  : `rgba(${r},${gr},${b},${0.08 + upRatio * 0.15})`,
                outline: active ? `1px solid rgba(${r},${gr},${b},0.8)` : 'none',
                outlineOffset: 1,
              }}
              title={`${g.key}: ${g.count}只, 涨${qm.upCount}/跌${qm.downCount}, 均幅${qm.avgPct != null ? fmtPct(qm.avgPct) : '—'}`}
            >
              {g.key}
              <span className="ml-1 opacity-60">{g.count}</span>
              {qm.avgPct != null && (
                <span className={`ml-1 ${priceColorClass(qm.avgPct)}`} style={{ fontSize: '9px' }}>
                  {fmtPct(qm.avgPct)}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ===== 维度分组侧边栏 =====

export function DimensionGroupSidebar({
  groups,
  quoteMap,
  selectedKey,
  onSelect,
  searchValue,
  onSearchChange,
  kindLabel,
  colorScheme,
}: {
  groups: DimensionGroup[]
  quoteMap: Map<string, QuoteMap>
  selectedKey: string | null
  onSelect: (key: string) => void
  searchValue: string
  onSearchChange: (v: string) => void
  kindLabel: string
  colorScheme: 'blue' | 'amber'
}) {
  const q = searchValue.trim().toLowerCase()
  const filtered = q
    ? groups.filter(g => g.key.toLowerCase().includes(q))
    : groups

  const accentColor = colorScheme === 'blue' ? 'rgba(59,130,246,0.7)' : 'rgba(245,158,11,0.7)'
  const accentBg = colorScheme === 'blue' ? 'rgba(59,130,246,0.1)' : 'rgba(245,158,11,0.1)'
  const accentBorder = colorScheme === 'blue' ? 'rgba(59,130,246,0.25)' : 'rgba(245,158,11,0.25)'

  return (
    <section className="rounded-card border border-border bg-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-medium text-foreground">{kindLabel}榜单</h3>
        <p className="mt-0.5 text-[11px] text-muted">按覆盖标的数量排序</p>
      </div>
      <div className="p-3 border-b border-border/60">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            value={searchValue}
            onChange={e => onSearchChange(e.target.value)}
            placeholder={`搜索${kindLabel}`}
            className="h-8 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground placeholder:text-muted/50 focus:outline-none focus:border-accent/50"
          />
        </div>
      </div>
      <div className="max-h-[560px] overflow-auto p-2 space-y-1">
        {filtered.length === 0 ? (
          <div className="px-3 py-8 text-center text-xs text-muted">没有可展示的分组</div>
        ) : filtered.slice(0, 200).map((group, i) => {
          const active = selectedKey === group.key
          const qm = computeQuoteMetrics(group.stocks, quoteMap)
          return (
            <button
              key={group.key}
              onClick={() => onSelect(group.key)}
              className={cn(
                'w-full rounded-lg px-3 py-2 text-left transition-colors',
                active ? 'border' : 'border border-transparent hover:bg-elevated/60',
              )}
              style={active ? { backgroundColor: accentBg, borderColor: accentBorder } : undefined}
            >
              <div className="flex items-center gap-2">
                <span className="w-5 text-[10px] font-mono text-muted">#{i + 1}</span>
                <span className="flex-1 truncate text-xs font-medium text-foreground">{group.key}</span>
                <span className="font-mono text-xs text-secondary">{group.count}</span>
              </div>
              {/* 覆盖量进度条 */}
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-elevated">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${Math.max(6, (group.count / (filtered[0]?.count || 1)) * 100)}%`,
                    backgroundColor: accentColor,
                  }}
                />
              </div>
              {/* 行情摘要 */}
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted">
                {qm.avgPct != null && (
                  <span className={priceColorClass(qm.avgPct)}>
                    均幅 {fmtPct(qm.avgPct)}
                  </span>
                )}
                {qm.upCount + qm.downCount > 0 && (
                  <span>
                    <span className="text-bull">{qm.upCount}涨</span>
                    <span className="mx-0.5 text-muted/40">/</span>
                    <span className="text-bear">{qm.downCount}跌</span>
                  </span>
                )}
              </div>
            </button>
          )
        })}
      </div>
    </section>
  )
}

// ===== 行情摘要条 =====

export function QuoteSummaryBar({
  groups,
  quoteMap,
}: {
  groups: DimensionGroup[]
  quoteMap: Map<string, QuoteMap>
}) {
  const stats = useMemo(() => {
    let upTotal = 0, downTotal = 0, flatTotal = 0
    for (const g of groups) {
      const qm = computeQuoteMetrics(g.stocks, quoteMap)
      upTotal += qm.upCount
      downTotal += qm.downCount
      flatTotal += qm.flatCount
    }
    return { upTotal, downTotal, flatTotal }
  }, [groups, quoteMap])

  const total = stats.upTotal + stats.downTotal + stats.flatTotal
  if (total === 0) return null

  const upPct = (stats.upTotal / total) * 100
  const downPct = (stats.downTotal / total) * 100

  return (
    <div className="flex items-center gap-3 text-[11px]">
      <div className="flex-1 h-2 rounded-full overflow-hidden bg-elevated flex">
        <div className="h-full bg-bull/70 rounded-l-full" style={{ width: `${upPct}%` }} />
        <div className="flex-1" />
        <div className="h-full bg-bear/70 rounded-r-full" style={{ width: `${downPct}%` }} />
      </div>
      <span className="text-bull font-mono">{stats.upTotal}涨</span>
      <span className="text-muted">/</span>
      <span className="text-bear font-mono">{stats.downTotal}跌</span>
    </div>
  )
}

// ===== 配置按钮 =====

export function ConfigButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="p-1.5 hover:bg-surface text-muted hover:text-accent transition-colors"
      title="配置数据源"
    >
      <Settings2 className="h-3.5 w-3.5" />
    </button>
  )
}

/**
 * 内置预设 (概念/行业) 数据获取空状态。
 *
 * 当检测到内置预设存在但无数据时, 展示图标 + 提示 + 「获取数据」按钮,
 * 让用户手动触发拉取 (POST /api/ext-data/presets/{id}/fetch), 而非自动拉取。
 */
export function PresetFetchState({
  title,
  hint,
  isLoading,
  error,
  onFetch,
}: {
  title: string
  hint: string
  isLoading: boolean
  error: unknown
  onFetch: () => void
}) {
  const errMsg = error instanceof Error ? error.message : error ? String(error) : ''
  return (
    <div className="h-full grid place-items-center px-8 py-16">
      <div className="text-center max-w-md">
        <DownloadCloud className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} />
        <h2 className="mt-4 text-base font-medium text-foreground">{title}</h2>
        <p className="mt-2 text-sm text-secondary leading-relaxed">{hint}</p>
        <button
          onClick={onFetch}
          disabled={isLoading}
          className="mt-5 inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:brightness-110 disabled:opacity-60"
        >
          {isLoading ? (
            <><RefreshCw className="h-4 w-4 animate-spin" /> 获取中...</>
          ) : (
            <><DownloadCloud className="h-4 w-4" /> 获取数据</>
          )}
        </button>
        {errMsg && (
          <p className="mt-3 flex items-center justify-center gap-1.5 text-xs text-bear">
            <AlertCircle className="h-3.5 w-3.5" /> {errMsg}
          </p>
        )}
      </div>
    </div>
  )
}
