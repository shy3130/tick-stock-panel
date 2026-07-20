import { motion } from 'framer-motion'
import { Loader2, CheckCircle2, Settings, Table2 } from 'lucide-react'
import { formatNumber } from '@/lib/format'
import { fmtDate } from '@/lib/format'
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
    <div className="rounded-btn bg-base/40 border border-border px-3 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="font-mono text-sm font-medium tabular-nums mt-0.5">{value}</div>
    </div>
  )
}

function CapBadge({ hasCap, isLocal, tierLabel, tierReq, capInfo, localSuffix, customProvider }: {
  hasCap: boolean
  isLocal: boolean
  tierLabel?: string
  tierReq?: string
  capInfo?: { rpm: number | null; batch: number | null; subscribe: number | null } | undefined
  localSuffix?: string
  customProvider?: string | null
}) {
  // 走自定义数据源时, 显示数据源名而非 TickFlow 档位
  if (customProvider) {
    return (
      <span className="text-[10px] text-accent/80 bg-accent/8 rounded px-1.5 py-px font-medium">
        {customProvider}
      </span>
    )
  }

  if (isLocal) {
    return (
      <span className="text-[10px] text-secondary bg-elevated rounded px-1.5 py-px font-medium">
        本地计算{localSuffix ? ` · ${localSuffix}` : ''}
      </span>
    )
  }

  if (hasCap && capInfo && tierLabel) {
    const parts = [tierLabel, `${capInfo.rpm}/min`]
    if (capInfo.batch != null && capInfo.batch > 1) parts.push(`${capInfo.batch}股/批`)
    return (
      <span className="text-[10px] text-accent/80 bg-accent/8 rounded px-1.5 py-px font-mono font-medium">
        {parts.join(' · ')}
      </span>
    )
  }

  if (!hasCap && tierReq && tierReq !== 'Free') {
    // 缺权限且非 Free 档(付费档位才提示升级);Free 档人人可用,
    // 若显示"需 Free"会造成 Expert 等用户困惑(通常是探测瞬时失败丢能力)
    return (
      <span className="text-[10px] text-warning/90 bg-warning/8 rounded px-1.5 py-px font-medium">
        需 {tierReq}
      </span>
    )
  }

  if (hasCap) {
    return (
      <span className="text-[10px] text-accent/80 bg-accent/8 rounded px-1.5 py-px font-medium">
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
  tierKey, capLimits, tierLabel, customProvider,
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
  customProvider?: string | null
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
  const empty = loading || !stats || (stats.rows === 0 && !stats.trading_days && !stats.fields)
  const borderCls = active
    ? 'border-accent/50'
    : done
      ? 'border-bear/30'
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
          className="inline-flex align-middle ml-1 p-0.5 rounded hover:bg-elevated transition-colors text-secondary hover:text-accent"
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
      className="inline-flex align-middle -mt-px p-0.5 rounded hover:bg-elevated transition-colors text-secondary hover:text-accent"
      title={`查看${tab.label}字段说明`}
    >
      <Table2 className="h-3 w-3" />
    </button>
  )

  // subLabel 文本内容 (不含图标)
  const subLabelText = subLabel
    ?? (isInstrument
      ? `标的 · ${((stats?.named ?? stats?.rows) ?? 0).toLocaleString()} 个含名称`
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

  // 有 fieldTabs 时: 把 subLabel 按分隔符拆开, 每个匹配词后面内联图标
  // 例如 "日 · 维表 · 日K · 指标" → 日 · 维表[icon] · 日K[icon] · 指标[icon]
  const renderSubLabelInline = () => {
    if (!fieldTabs || fieldTabs.length === 0) {
      return <>{subLabelText}{renderFieldButtons()}</>
    }
    const labels = fieldTabs.map(t => t.label)
    // 按非字母数字汉字的分隔符拆分, 保留分隔符
    const tokens = subLabelText.split(/(\s*·\s*|\s+)/).filter(t => t !== '')
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
    <div className={`rounded-card border ${borderCls} ${bgCls} flex flex-col transition-all duration-300 ${active ? 'shadow-[0_0_16px_rgba(61,214,140,0.08)]' : ''}`}>
      <div className="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        <div className="flex items-center gap-1.5">
          {auto !== undefined && !loading && (
            <span className="inline-flex items-center gap-1 text-[10px] font-medium">
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${auto ? 'bg-accent shadow-[0_0_4px_rgba(61,214,140,0.5)]' : 'bg-muted'}`} />
              <span className={auto ? 'text-accent/70' : 'text-muted'}>{auto ? '自动' : '关闭'}</span>
            </span>
          )}
          {active && <Loader2 className="h-3.5 w-3.5 text-accent animate-spin" />}
          {done && !active && !skipped && <CheckCircle2 className="h-3.5 w-3.5 text-bear" />}
          {skipped && !active && (
            <span className="text-[10px] text-muted bg-elevated rounded px-1.5 py-px font-medium">
              本次跳过
            </span>
          )}
          {onSettings && (
            <button
              onClick={(e) => { e.stopPropagation(); onSettings() }}
              className={`p-0.5 rounded hover:bg-elevated transition-colors ${
                settingsOpen ? 'text-accent' : 'text-secondary'
              }`}
              title="设置"
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
            customProvider={customProvider}
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
            <div className="font-mono text-2xl font-bold tracking-tight tabular-nums text-foreground">—</div>
            <div className="text-[11px] text-muted mt-0.5">
              暂无数据{renderFieldButtons()}
            </div>
          </>
        ) : (
          <>
            <div className="font-mono text-2xl font-bold tracking-tight tabular-nums text-foreground">
              {stats.fields
                ? stats.fields
                : stats.trading_days && !stats.rows
                  ? stats.trading_days.toLocaleString()
                  : formatNumber(stats.rows)}
            </div>
            <div className="text-[11px] text-muted mt-0.5">
              {renderSubLabelInline()}
            </div>
          </>
        )}
      </div>

      <div className="mt-auto px-4 pb-4 pt-2 border-t border-border space-y-0.5">
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
          </>
        )}
      </div>

      {active && stagePct > 0 && (
        <div className="h-1 bg-elevated overflow-hidden rounded-b-card">
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
