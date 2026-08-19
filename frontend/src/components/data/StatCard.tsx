import { motion } from 'framer-motion'
import { Loader2, CheckCircle2, Settings, Table2, AlertTriangle } from 'lucide-react'
import { formatNumber } from '@/lib/format'
import { fmtDate } from '@/lib/format'
import { cn } from '@/lib/cn'
import { StatusDot } from '@/components/ui/Primitives'
import { Skeleton } from './Skeleton'

// 卡片能力定义：capKey → 查 capability limits；tierReq → 无权限时显示的档位要求
// capKey 为空串表示该数据在 free-api 服务器(None 档/Free 档)即可获取,无需付费能力门控。
export const CARD_META: Record<string, {
  capKey: string   // 对应的 capability key，空串表示本地计算 / free 服务器可用
  tierReq: string  // 最低档位要求（无权限时显示）
}> = {
  // 标的维表走 exchanges 端点,free-api 服务器即可获取,无需付费能力
  instruments: { capKey: '',                        tierReq: '' },
  daily:       { capKey: 'kline.daily.batch',       tierReq: 'Starter+' },
  adj_factor:  { capKey: 'adj_factor',              tierReq: 'Starter+' },
  enriched:    { capKey: '',                        tierReq: '' },
  // ETF 复用日K批量能力(免费档 kline.daily.batch 即可),不显示档位徽章
  etf:         { capKey: 'kline.daily.batch',       tierReq: '' },
  minute:      { capKey: 'kline.minute.batch',      tierReq: 'Pro+' },
  financials:  { capKey: 'financial',                tierReq: 'Expert' },
}

export function Pill({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-btn border border-border bg-base/40 px-3 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-0.5 font-mono text-sm font-medium tabular-nums">{value}</div>
    </div>
  )
}

function CapBadge({ hasCap, isLocal, tierLabel, tierReq, capInfo, localSuffix }: {
  hasCap: boolean
  isLocal: boolean
  tierLabel?: string
  tierReq?: string
  capInfo?: { rpm: number | null; batch: number | null; subscribe: number | null } | undefined
  localSuffix?: string
}) {
  if (isLocal) {
    return (
      <span className="rounded bg-elevated px-1.5 py-px text-[10px] font-medium text-secondary">
        本地计算{localSuffix ? ` · ${localSuffix}` : ''}
      </span>
    )
  }

  if (hasCap && capInfo && tierLabel) {
    const parts = [tierLabel]
    if (capInfo.rpm != null) parts.push(`${capInfo.rpm}/min`)
    if (capInfo.batch != null && capInfo.batch > 1) parts.push(`${capInfo.batch}股/批`)
    return (
      <span className="rounded bg-accent/8 px-1.5 py-px font-mono text-[10px] font-medium text-accent/80">
        {parts.join(' · ')}
      </span>
    )
  }

  if (!hasCap && tierReq && tierReq !== 'Free') {
    return (
      <span className="rounded bg-warning/8 px-1.5 py-px text-[10px] font-medium text-warning/90">
        需数据源支持
      </span>
    )
  }

  if (hasCap) {
    return (
      <span className="rounded bg-accent/8 px-1.5 py-px text-[10px] font-medium text-accent/80">
        {tierLabel ?? '已授权'}
      </span>
    )
  }

  return null
}

export type FieldTab = { label: string; table: string }

export function StatCard({
  title, hint, stats, isInstrument = false, loading = false,
  active = false, done = false, skipped = false, stagePct = 0,
  tierKey, capLimits, tierLabel,
  auto, onSettings, onShowFields, settingsOpen, subLabel, localBadgeSuffix, fieldTabs,
}: {
  title: string
  hint: string
  stats: any | null | undefined
  isInstrument?: boolean
  loading?: boolean
  active?: boolean
  done?: boolean
  skipped?: boolean
  stagePct?: number
  tierKey?: string
  capLimits?: Record<string, { rpm: number | null; batch: number | null; subscribe: number | null }>
  tierLabel?: string
  onSettings?: () => void
  onShowFields?: (table?: string) => void
  settingsOpen?: boolean
  auto?: boolean
  subLabel?: string
  localBadgeSuffix?: string
  // 多表字段入口: [{label: '维表', table: 'index_instruments'}, ...]
  // 提供时渲染多个图标按钮(每个对应一张表的字段说明); 否则回退到单个 onShowFields
  fieldTabs?: FieldTab[]
}) {
  // 契约字段(/api/data/status TableStats):
  // - storage_mode=provider_on_demand: Provider 按需读取、不单独落盘, 不是"暂无数据"
  // - row_count_exact=false: rows 非精确统计; canonical_history.rows 为已发布下界
  // - freshness/local_overlay/latest_partition_symbols: 新鲜度与合并展示(canonical 全历史 + 本地 overlay)
  const providerOnDemand = stats?.storage_mode === 'provider_on_demand'
  const inexact = !providerOnDemand && stats?.row_count_exact === false
  const canonical = stats?.canonical_history ?? null
  const freshness = stats?.freshness ?? null
  const overlay = stats?.local_overlay ?? null
  const empty = loading || !stats || (stats.rows === 0 && !stats.trading_days && !stats.fields && !stats.available && !providerOnDemand)
  const borderCls = active
    ? 'border-accent/50'
    : done
      ? 'border-success/30'
      : 'border-border'
  const bgCls = active ? 'bg-accent/[0.03]' : 'bg-surface'

  const meta = tierKey ? CARD_META[tierKey] : undefined
  const isLocal = meta?.capKey === ''
  const capInfo = meta?.capKey ? capLimits?.[meta.capKey] : undefined
  const hasCap = isLocal || !!capInfo

  // 渲染字段说明入口图标
  // - fieldTabs 提供时: 返回 null (图标由 renderSubLabelInline 内联到文字后)
  // - 否则: 单个图标按钮 (onShowFields)
  const renderFieldButtons = () => {
    if (fieldTabs && fieldTabs.length > 0) return null
    if (onShowFields) {
      return (
        <button
          onClick={(e) => { e.stopPropagation(); onShowFields() }}
          className="ml-1 inline-flex align-middle rounded p-0.5 text-secondary transition-colors hover:bg-elevated hover:text-accent"
          title="查看字段说明"
        >
          <Table2 className="h-3 w-3" />
        </button>
      )
    }
    return null
  }

  // 单个图标按钮 (复用样式)
  const fieldIconButton = (tab: FieldTab) => (
    <button
      key={tab.table}
      onClick={(e) => { e.stopPropagation(); onShowFields?.(tab.table) }}
      className="-mt-px inline-flex align-middle rounded p-0.5 text-secondary transition-colors hover:bg-elevated hover:text-accent"
      title={`查看${tab.label}字段说明`}
    >
      <Table2 className="h-3 w-3" />
    </button>
  )

  // subLabel 文本内容 (不含图标)
  const subLabelText: string = subLabel
    ?? (isInstrument
      ? `标的 · ${((stats?.named ?? stats?.rows) ?? 0).toLocaleString()} 个含名称`
      : providerOnDemand
        ? (stats?.status_message ?? 'Provider 按需读取 · 不单独落盘')
        : stats?.fields
          ? '字段 · 复权 · 技术指标'
          : title === '日 K' && stats?.trading_days
            ? '日 · A股标的 · 日线'
            : stats?.trading_days && !stats?.rows
              ? '日 · A股标的 · 分钟级'
              : (() => {
                  const parts = [`行 · ${(stats?.symbols_covered ?? 0)} 只标的`]
                  if (stats?.trading_days) parts.push(`· ${stats.trading_days} 日`)
                  return parts.join(' ')
                })())

  // 行数未精确统计时, 大数字是 canonical 已发布下界(≥N), 在 subLabel 后缀说明
  const displaySubLabel = inexact && canonical?.rows ? `${subLabelText} · 未精确统计` : subLabelText

  // 有 fieldTabs 时: 把 subLabel 按分隔符拆开, 每个匹配词后面内联图标
  // 例如 "日 · 维表 · 日K · 指标" → 日 · 维表[icon] · 日K[icon] · 指标[icon]
  const renderSubLabelInline = () => {
    if (!fieldTabs || fieldTabs.length === 0) {
      return <>{displaySubLabel}{renderFieldButtons()}</>
    }
    const labels = fieldTabs.map(t => t.label)
    // 按非字母数字汉字的分隔符拆分, 保留分隔符
    const tokens = displaySubLabel.split(/(\s*·\s*|\s+)/).filter(t => t !== '')
    const used = new Set<string>()
    return (
      <>
        {tokens.map((tok, i) => {
          const trimmed = tok.trim()
          // 跳过纯分隔符
          if (trimmed === '' || trimmed === '·') return <span key={i}>{tok}</span>
          // 匹配某个 tab label (整体匹配, 避免部分子串误命中)
          const idx = labels.indexOf(trimmed)
          if (idx >= 0 && !used.has(trimmed)) {
            used.add(trimmed)
            return <span key={i}>{tok}{fieldIconButton(fieldTabs[idx])}</span>
          }
          return <span key={i}>{tok}</span>
        })}
      </>
    )
  }

  return (
    <div className={cn(
      'panel flex flex-col transition-all duration-300',
      borderCls,
      bgCls,
    )}>
      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        <div className="flex items-center gap-1.5">
          {auto !== undefined && !loading && (
            <span className="inline-flex items-center gap-1 text-[10px] font-medium">
              <StatusDot state={auto ? 'live' : 'off'} />
              <span className={auto ? 'text-accent/70' : 'text-muted'}>{auto ? '自动' : '关闭'}</span>
            </span>
          )}
          {active && <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />}
          {done && !active && !skipped && <CheckCircle2 className="h-3.5 w-3.5 text-success" />}
          {skipped && !active && (
            <span className="rounded bg-elevated px-1.5 py-px text-[10px] font-medium text-muted">
              本次跳过
            </span>
          )}
          {onSettings && (
            <button
              onClick={(e) => { e.stopPropagation(); onSettings() }}
              className={cn(
                'rounded p-0.5 transition-colors hover:bg-elevated',
                settingsOpen ? 'text-accent' : 'text-secondary',
              )}
            >
              <Settings className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      <div className="px-4 pb-1 text-[10px] text-muted">{hint}</div>

      <div className="px-4 pb-2">
        {loading ? (
          <Skeleton w="w-16" h="h-4" />
        ) : (
          <CapBadge
            hasCap={hasCap}
            isLocal={isLocal}
            tierLabel={tierLabel}
            tierReq={meta?.tierReq}
            capInfo={capInfo}
            localSuffix={localBadgeSuffix}
          />
        )}
      </div>

      <div className="px-4 pb-1">
        {loading ? (
          <>
            <Skeleton w="w-20" h="h-8" />
            <Skeleton w="w-24" h="h-3" className="mt-1" />
          </>
        ) : empty ? (
          <>
            <div className="metric-value text-2xl">—</div>
            <div className="mt-0.5 text-[11px] text-muted">
              暂无数据{renderFieldButtons()}
            </div>
          </>
        ) : (
          <>
            <div className="metric-value text-2xl">
              {providerOnDemand
                ? '按需'
                : inexact
                  ? canonical?.rows
                    ? `≥ ${formatNumber(canonical.rows)}`
                    : '未精确统计'
                  : stats.fields
                    ? stats.fields
                    : stats.trading_days && !stats.rows
                      ? stats.trading_days.toLocaleString()
                      : stats.available && !stats.rows
                        ? '可用'
                        : formatNumber(stats.rows)}
            </div>
            <div className="mt-0.5 text-[11px] text-muted">
              {renderSubLabelInline()}
            </div>
          </>
        )}
      </div>

      {/* 新鲜度警告: 上游新交易日待发布 — 避免"截至昨日"被误读为同步滞后 */}
      {!loading && freshness?.status === 'awaiting_publish' && (
        <div className="mx-4 mb-2 flex items-start gap-1.5 rounded-btn border border-warning/40 bg-warning/8 px-2.5 py-1.5">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0 text-warning" />
          <div className="text-[10px] leading-relaxed text-warning/90">
            {freshness.reason
              ?? [
                freshness.reference_date ? `数据截至 ${fmtDate(freshness.reference_date)}` : null,
                '上游新交易日待发布',
                (freshness.age_days ?? 0) > 0 ? `滞后 ${freshness.age_days} 天` : null,
              ].filter(Boolean).join(' · ')}
          </div>
        </div>
      )}

      <div className="mt-auto space-y-0.5 border-t border-border px-4 pb-4 pt-2">
        {loading ? (
          <>
            <div className="flex justify-between"><Skeleton w="w-6" h="h-3" /><Skeleton w="w-16" h="h-3" /></div>
            <div className="flex justify-between"><Skeleton w="w-4" h="h-3" /><Skeleton w="w-16" h="h-3" /></div>
          </>
        ) : empty ? (
          <>
            <div className="flex justify-between text-[11px]">
              <span className="text-muted">{isInstrument ? '快照日' : '起'}</span>
              <span className="font-mono text-secondary">—</span>
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-muted">{isInstrument ? '标的数' : '止'}</span>
              <span className="font-mono text-secondary">—</span>
            </div>
          </>
        ) : (
          <>
            <div className="flex justify-between text-[11px]">
              <span className="text-muted">{isInstrument ? '快照日' : '起'}</span>
              <span className="font-mono text-secondary">{fmtDate(isInstrument ? stats.latest_as_of : stats.earliest_date)}</span>
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-muted">{isInstrument ? '标的数' : '止'}</span>
              <span className="font-mono text-secondary">{isInstrument ? String(stats.rows) : fmtDate(stats.latest_date)}</span>
            </div>
            {/* 最新本地分区实际覆盖 / 总股票池(canonical 合并展示时提供) */}
            {stats.latest_partition_symbols != null && (
              <div className="flex justify-between text-[11px]">
                <span className="text-muted">最新分区</span>
                <span className="font-mono text-secondary">
                  {stats.latest_partition_symbols.toLocaleString()}
                  {stats.universe_symbols != null ? ` / ${stats.universe_symbols.toLocaleString()}` : ''}
                  {' '}只
                </span>
              </div>
            )}
            {/* 本地增量 overlay 简明说明(canonical 发布点之后的本地产出) */}
            {overlay && overlay.trading_days > 0 && (
              <div className="flex justify-between text-[11px]">
                <span className="text-muted">本地增量</span>
                <span className="font-mono text-secondary">
                  {overlay.trading_days} 日 · 至 {fmtDate(overlay.latest_date)}
                </span>
              </div>
            )}
          </>
        )}
      </div>

      {active && stagePct > 0 && (
        <div className="h-1 overflow-hidden rounded-b-[var(--panel-radius)] bg-elevated">
          <motion.div
            className="h-full bg-accent"
            initial={{ width: 0 }}
            animate={{ width: `${stagePct}%` }}
            transition={{ duration: 0.3, ease: 'easeOut' }}
          />
        </div>
      )}
    </div>
  )
}
