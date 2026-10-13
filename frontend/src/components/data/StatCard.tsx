import { motion } from 'framer-motion'
import { Loader2, CheckCircle2, Settings, Table2, Download } from 'lucide-react'
import { formatNumber } from '@/lib/format'
import { fmtDate } from '@/lib/format'
import { MissingCapChip } from '@/lib/capability-labels'
import { Skeleton } from './Skeleton'

// 卡片能力定义：capKey → 查 capability limits；missingCapName → 无权限时提示的能力名
// capKey 为空串表示该数据在免费服务器(None/Free)或本地即可获取,无需能力门控。
export const CARD_META: Record<string, {
  capKey: string          // 对应的 capability key，空串表示本地计算 / 免费服务器可用
  missingCapName: string  // 缺能力时提示的能力名(空串表示缺能力也不显示徽章)
}> = {
  // 标的维表走 exchanges 端点,免费服务器即可获取,无需付费能力
  instruments: { capKey: '',                        missingCapName: '' },
  daily:       { capKey: 'kline.daily.batch',       missingCapName: '日 K(批量)' },
  adj_factor:  { capKey: 'adj_factor',              missingCapName: '复权因子' },
  enriched:    { capKey: '',                        missingCapName: '' },
  // ETF 复用日K批量能力(免费档即可),缺能力时随日K卡提示,不单独显示徽章
  etf:         { capKey: 'kline.daily.batch',       missingCapName: '' },
  minute:      { capKey: 'kline.minute.batch',      missingCapName: '分钟 K(批量)' },
  financials:  { capKey: 'financial',               missingCapName: '财务数据' },
  regime:      { capKey: '',                        missingCapName: '' },
}

export function Pill({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-btn bg-base/40 border border-border px-3 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="font-mono text-sm font-medium tabular-nums mt-0.5">{value}</div>
    </div>
  )
}

// 卡片能力徽章的输入: TickFlow 套餐限额对象, 或路由门控合并后的布尔可用性
// (矩阵 usable 合并进 caps 时, 非对象真值表示「经其他数据源可用, 无套餐限额可显」)。
export type CapLimitValue = { rpm: number | null; batch: number | null; subscribe: number | null } | boolean

function CapBadge({ hasCap, isLocal, missingCapName, capInfo, localSuffix, customProvider }: {
  hasCap: boolean
  isLocal: boolean
  missingCapName?: string
  capInfo?: { rpm: number | null; batch: number | null; subscribe: number | null } | undefined
  localSuffix?: string
  customProvider?: string | null
}) {
  // 走自定义数据源时, 显示数据源名 (能力来源对所有数据源统一表达)
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

  if (hasCap && capInfo) {
    const parts = ['可用', `${capInfo.rpm}/min`]
    if (capInfo.batch != null && capInfo.batch > 1) parts.push(`${capInfo.batch}股/批`)
    return (
      <span className="text-[10px] text-accent/80 bg-accent/8 rounded px-1.5 py-px font-mono font-medium">
        {parts.join(' · ')}
      </span>
    )
  }

  if (!hasCap && missingCapName) {
    // 能力标准对所有数据源一致: 缺能力提示能力名而非档位, 点击跳数据源设置
    return <MissingCapChip label={missingCapName} />
  }

  if (hasCap) {
    return (
      <span className="text-[10px] text-accent/80 bg-accent/8 rounded px-1.5 py-px font-medium">
        可用
      </span>
    )
  }

  return null
}

export type FieldTab = { label: string; table: string }

export function StatCard({
  title, hint, stats, isInstrument = false, loading = false,
  active = false, done = false, skipped = false, stagePct = 0,
  tierKey, capLimits, customProvider,
  auto, onSettings, onShowFields, settingsOpen, subLabel, localBadgeSuffix, fieldTabs,
  onPull, pullTitle,
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
  capLimits?: Record<string, CapLimitValue>
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
  /** 提供时在齿轮左侧渲染拉取入口 (用于不在盘后管道内的数据集, 如财务) */
  onPull?: () => void
  pullTitle?: string
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
  // 布尔值 (路由门控合并) 只表达可用性; 限额信息仅当值为套餐对象时展示
  const rawCap = meta?.capKey ? capLimits?.[meta.capKey] : undefined
  const capInfo = rawCap && typeof rawCap === 'object' ? rawCap : undefined
  const hasCap = isLocal || !!rawCap

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
          {onPull && (
            <button
              onClick={(e) => { e.stopPropagation(); onPull() }}
              className="rounded p-0.5 text-secondary transition-colors hover:bg-elevated hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
              title={pullTitle ?? '手动拉取'}
              aria-label={pullTitle ?? '手动拉取'}
            >
              <Download className="h-3.5 w-3.5" />
            </button>
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
            missingCapName={meta?.missingCapName}
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
