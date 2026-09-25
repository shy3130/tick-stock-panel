import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  AlertCircle,
  AlertTriangle,
  CandlestickChart as CandlestickIcon,
  Check,
  CheckCircle2,
  Database,
  ExternalLink,
  Eye,
  EyeOff,
  FileWarning,
  KeyRound,
  Landmark,
  ListChecks,
  Loader2,
  Lock,
  Plus,
  Puzzle,
  Radio,
  RefreshCw,
  Route,
  Save,
  Scale,
  Timer,
  Trash2,
  Zap,
} from 'lucide-react'
import {
  api,
  type CapabilityMatrix,
  type CapabilityRoute,
  type DataSourceItem,
  type PluginDataSourceItem,
  type Preferences,
  type ProviderField,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilities, usePreferences } from '@/lib/useSharedQueries'
import { AnchorWrap } from '@/lib/useCardFlash'
import { CAP_LABELS, TIER_RANK, tierRank, tierStyle, TierTag } from '@/lib/capability-labels'
import { toast } from '@/components/Toast'
import { DataSourceEditor } from './DataSourceEditor'
import { TickFlowKeySection, TierHelpPopover, useInvalidateTierRelated } from './Keys'

const DATASET_LABEL: Record<string, string> = {
  realtime: '实时',
  daily: '日K',
  minute: '分钟',
  adj_factor: '除权',
  depth5: '五档',
  financial: '财务',
  full_minute: '全量分钟',
}

/** 能力图标 (纯展示; 能力清单本身由后端注册表驱动) */
const CAP_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  realtime: Radio,
  daily: CandlestickIcon,
  minute: Timer,
  full_minute: Zap,
  adj_factor: Scale,
  financial: Landmark,
}

/** TickFlow 档位要求文本: none → 全档位, 其余 → starter+ 形式 */
function tierReqText(tier: string): string {
  return tier === 'none' ? '全档位' : `${tier}+`
}

/** TickFlow 档位要求徽标: 按所需档位配色, 当前档位不足时琥珀描边提示。
 *  仅用于 TickFlow 详情的档位介绍表 (能力卡上不展示档位信息)。 */
function TierReqChip({ tier, currentLabel }: { tier: string; currentLabel?: string }) {
  const text = tierReqText(tier)
  const req = TIER_RANK[tier] ?? -1
  const unmet = currentLabel != null && tierRank(currentLabel) < req
  const t = tierStyle(tier)
  return (
    <span
      title={unmet
        ? `TickFlow 该能力需 ${text} — 当前档位 ${currentLabel}`
        : `TickFlow 该能力需 ${text}`}
      className={`inline-flex h-[15px] shrink-0 items-center gap-1 rounded px-1.5 text-[9px] font-bold font-mono leading-none ${unmet ? 'ring-1 ring-warning/60' : ''}`}
      style={t.tagBg}
    >
      <span className="h-1 w-1 rounded-full shrink-0" style={t.dotStyle} />
      <span className="capitalize" style={t.labelTextStyle}>{text}</span>
    </span>
  )
}

/** TickFlow「已适配全档位」标识: Expert 三色渐变(全档位体系里最醒目的身份色) */
function AllTiersBadge({ size = 'text-[10px]' }: { size?: string }) {
  const t = tierStyle('expert')
  return (
    <span
      className="inline-flex shrink-0 items-center rounded px-1.5 py-0.5 font-medium leading-none"
      style={t.tagBg}
      title="无 Key 到 Expert 各订阅档位均有可用能力 — 日K全档位可用, 除权/分钟/财务等高级能力按订阅档位解锁"
    >
      <span className={size} style={t.labelTextStyle}>✦ 已适配全档位</span>
    </span>
  )
}

/** 提供方标签样式: 当前项高亮(accent), 其余弱化可点; 未就绪源禁用置灰 */
function tagCls(active: boolean, disabled = false, interactive = true) {
  const base = 'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors select-none'
  if (disabled) return `${base} bg-elevated/40 text-muted/50 cursor-not-allowed`
  if (!interactive) return `${base} cursor-default ${active ? 'bg-accent/15 text-accent font-medium' : 'bg-elevated/60 text-muted/70'}`
  return `${base} cursor-pointer disabled:opacity-50 ${
    active
      ? 'bg-accent/15 text-accent font-medium'
      : 'bg-elevated/60 text-muted/70 hover:bg-accent/15 hover:text-accent'
  }`
}

/** 源名 → 展示名 (特殊值 + 候选/待就绪表查找, 找不到回退原始名) */
function displayOfName(matrix: CapabilityMatrix | undefined, name: string): string {
  if (name === 'tickflow') return 'TickFlow'
  if (name === 'same_as_daily') return '跟随日K'
  for (const cap of matrix?.capabilities ?? []) {
    const hit = cap.candidates.find(c => c.name === name) ?? cap.pending.find(c => c.name === name)
    if (hit) return hit.display
  }
  return name
}

/** 乐观更新: 把一组偏好字段变更应用到矩阵缓存 (每能力独立路由, current 即生效) */
function patchMatrix(
  matrix: CapabilityMatrix,
  changes: Partial<Record<ProviderField, string>>,
): CapabilityMatrix {
  const caps = matrix.capabilities.map(c => ({ ...c }))
  for (const cap of caps) {
    const value = cap.field != null ? changes[cap.field] : undefined
    if (value !== undefined) {
      cap.current = value
      cap.current_display = displayOfName({ capabilities: caps }, value)
      cap.effective = value
      cap.effective_display = cap.current_display
    }
  }
  return { ...matrix, capabilities: caps }
}

const DEFAULT_ROUTING: Record<ProviderField, string> = {
  daily_data_provider: 'tickflow',
  adj_factor_provider: 'tickflow',
  minute_data_provider: 'tickflow',
  full_minute_data_provider: 'tickflow',
  depth5_data_provider: 'tickflow',
  realtime_data_provider: 'tickflow',
  financial_data_provider: 'tickflow',
}

/** 单个能力卡: 当前生效提供方 + 候选切换标签。
 *  candidates 只含当前可提供该能力的源; 未就绪源 (pending) 置灰提示;
 *  生效源无法提供该能力 (usable=False, 无论档位不足还是源未就绪) 时显示琥珀警示。 */
function CapabilityCard({ cap, pendingKey, onSelect }: {
  cap: CapabilityRoute
  pendingKey: string | null
  onSelect: (field: ProviderField, provider: string) => void
}) {
  const Icon = CAP_ICON[cap.id] ?? Database
  const busy = (provider: string) => pendingKey === `${cap.field}:${provider}`
  // 能力中立判定: usable=False 即当前路由的源供不了 (TickFlow 档位不足或插件未就绪同待遇)
  const unmet = !cap.usable
  const chipsEmpty = cap.candidates.length === 0 && cap.pending.length === 0
  return (
    <div className="rounded-lg border border-border/50 bg-elevated/20 px-3 py-2.5 flex flex-col transition-colors hover:border-border">
      <div className="flex items-center gap-1.5">
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-accent/10">
          <Icon className="h-3 w-3 text-accent" />
        </span>
        <div className="text-xs font-medium text-foreground truncate">{cap.label}</div>
      </div>
      {cap.desc && <div className="mt-1 text-[10px] text-muted/70 truncate">{cap.desc}</div>}

      {/* 当前生效提供方 */}
      <div className="mt-2 flex items-center gap-1.5 min-w-0">
        <span className="text-[9px] font-medium uppercase tracking-wider text-muted/50 shrink-0">当前</span>
        {unmet ? (
          <span
            className="inline-flex items-center gap-1 text-[11px] font-medium text-warning truncate"
            title={`「${cap.label}」当前路由的源无法提供该能力 — 可切换下方可用源, 或在数据源区接入其他源`}
          >
            <AlertTriangle className="h-3 w-3 shrink-0" />
            能力不可用
          </span>
        ) : (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-accent shrink-0" />
            <span className="text-[11px] font-medium text-foreground truncate">{cap.effective_display}</span>
          </>
        )}
      </div>

      {/* 候选标签: 点谁该能力就由谁提供 */}
      <div className="flex flex-wrap gap-1 mt-2 pt-2 border-t border-border/50 min-h-[26px] items-center">
        {cap.candidates.map(c => {
          const active = cap.current === c.name
          // field=null → 不可路由能力 (仅 TickFlow 提供): 渲染为非交互标签,
          // 保持激活高亮但不可点击 (用 button+disabled 会被 opacity-50 冲淡成灰色)
          const routable = cap.field != null
          if (!routable) {
            return (
              <span
                key={c.name}
                title={`「${cap.label}」仅 ${c.display} 提供 (不可路由)`}
                className={tagCls(active, false, false)}
              >
                {c.display}
              </span>
            )
          }
          return (
            <button
              key={c.name}
              type="button"
              disabled={pendingKey != null}
              onClick={() => cap.field != null && onSelect(cap.field, c.name)}
              title={`「${cap.label}」由 ${c.display} 提供`}
              className={tagCls(active)}
            >
              {busy(c.name) && <Loader2 className="h-2.5 w-2.5 animate-spin" />}
              {c.display}
            </button>
          )
        })}
        {/* 未就绪源: 声明了该能力但依赖/Key 未配好, 置灰并说明原因 */}
        {cap.pending.map(c => (
          <span
            key={c.name}
            title={c.note || c.status || '该源当前不可用'}
            className={tagCls(false, true)}
          >
            <AlertCircle className="h-2.5 w-2.5" />
            {c.display}
          </span>
        ))}
        {chipsEmpty && (
          <span className="text-[10px] text-muted/50">
            暂无可用提供方
          </span>
        )}
      </div>
    </div>
  )
}

/** 能力路由区 (页面主视图): 每个能力一张卡, 点候选标签即刻切换 (乐观更新) */
function CapabilityRoutingSection() {
  const qc = useQueryClient()
  const matrix = useQuery({ queryKey: QK.capabilityMatrix, queryFn: api.capabilityMatrix })
  const [pendingKey, setPendingKey] = useState<string | null>(null)

  const invalidateRouting = () => {
    qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
    qc.invalidateQueries({ queryKey: QK.preferences })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }

  /** 切换前先把变更写进矩阵/偏好缓存, 界面零延迟响应; 失败回滚 */
  const applyOptimistic = (changes: Partial<Record<ProviderField, string>>) => {
    const prevMatrix = qc.getQueryData<CapabilityMatrix>(QK.capabilityMatrix)
    const prevPrefs = qc.getQueryData<Preferences>(QK.preferences)
    if (prevMatrix) qc.setQueryData(QK.capabilityMatrix, patchMatrix(prevMatrix, changes))
    if (prevPrefs) qc.setQueryData(QK.preferences, { ...prevPrefs, ...changes })
    return { prevMatrix, prevPrefs }
  }

  const rollback = (ctx: { prevMatrix?: CapabilityMatrix; prevPrefs?: Preferences } | undefined) => {
    if (ctx?.prevMatrix) qc.setQueryData(QK.capabilityMatrix, ctx.prevMatrix)
    if (ctx?.prevPrefs) qc.setQueryData(QK.preferences, ctx.prevPrefs)
  }

  const routeMut = useMutation({
    mutationFn: ({ field, provider }: { field: ProviderField; provider: string }) =>
      // 动态键经运行时字段名收敛为合法偏好键 (字段名来自后端注册表)
      api.updateDataProviders({ [field]: provider } as Partial<Pick<Preferences, ProviderField>>),
    onMutate: async (v) => {
      setPendingKey(`${v.field}:${v.provider}`)
      await qc.cancelQueries({ queryKey: QK.capabilityMatrix })
      await qc.cancelQueries({ queryKey: QK.preferences })
      return applyOptimistic({ [v.field]: v.provider })
    },
    onSuccess: (_d, v) => {
      const cap = matrix.data?.capabilities.find(c => c.field === v.field)
      toast(`「${cap?.label || v.field}」已切换为 ${displayOfName(matrix.data, v.provider)}`, 'success')
    },
    onError: (e: Error, _v, ctx) => {
      rollback(ctx)
      toast(`路由切换失败: ${e.message}`, 'error')
    },
    onSettled: () => {
      setPendingKey(null)
      invalidateRouting()
    },
  })

  const resetMut = useMutation({
    mutationFn: () => api.updateDataProviders(DEFAULT_ROUTING),
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: QK.capabilityMatrix })
      await qc.cancelQueries({ queryKey: QK.preferences })
      return applyOptimistic(DEFAULT_ROUTING)
    },
    onSuccess: () => toast('能力路由已恢复默认', 'success'),
    onError: (e: Error, _v, ctx) => {
      rollback(ctx)
      toast(`恢复失败: ${e.message}`, 'error')
    },
    onSettled: invalidateRouting,
  })

  const list = matrix.data?.capabilities ?? []
  const anyCustom = list.some(c => c.current !== c.default)

  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-1 gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <Route className="h-4 w-4 text-secondary shrink-0" />
          <h2 className="text-sm font-medium text-foreground">能力路由</h2>
          <span className="text-[10px] text-muted/60 shrink-0">{list.length} 个能力</span>
        </div>
        {anyCustom && (
          <button
            onClick={() => resetMut.mutate()}
            disabled={resetMut.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn text-xs text-muted hover:text-foreground hover:bg-elevated transition-colors disabled:opacity-50 shrink-0"
          >
            <RefreshCw className={`h-3 w-3 ${resetMut.isPending ? 'animate-spin' : ''}`} />
            恢复默认
          </button>
        )}
      </div>
      <p className="text-[11px] text-muted mb-4">
        每个能力独立选择提供方 — 点标签即刻生效。选项只列出当前可提供该能力的源
        (各源按自身可用性过滤, 详见下方数据源介绍); 未就绪的源置灰提示。
      </p>

      {matrix.isError ? (
        <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg border border-danger/20 bg-danger/5">
          <div className="flex items-center gap-2 text-xs text-danger min-w-0">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">能力矩阵加载失败: {(matrix.error as Error)?.message || '未知错误'}</span>
          </div>
          <button
            onClick={() => matrix.refetch()}
            className="text-xs text-muted hover:text-foreground shrink-0"
          >
            重试
          </button>
        </div>
      ) : matrix.isLoading || list.length === 0 ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-[120px] rounded-lg border border-border/50 bg-elevated/20 animate-pulse" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
          {list.map(cap => (
            <CapabilityCard
              key={cap.id}
              cap={cap}
              pendingKey={pendingKey}
              onSelect={(field, provider) => routeMut.mutate({ field, provider })}
            />
          ))}
        </div>
      )}
    </section>
  )
}

/** 插件 API Key 配置区块 (嵌入插件详情卡, 不再独立成卡)。
 *  先探后存: 后端用候选 Key 实探一次, 无效不落盘; secrets.json 优先于 .env。 */
function PluginKeyConfig({ plugin }: { plugin: PluginDataSourceItem }) {
  const qc = useQueryClient()
  const [keyInput, setKeyInput] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [saved, setSaved] = useState(false)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.dataSources })
    qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }

  const save = useMutation({
    mutationFn: () => api.savePluginKey(plugin.name, keyInput.trim()),
    onSuccess: (data) => {
      invalidate()
      if (data.ok) {
        setKeyInput('')
        setSaved(true)
        setTimeout(() => setSaved(false), 2000)
      }
    },
    onError: (e: Error) => toast(`保存失败: ${e.message}`, 'error'),
  })

  const clear = useMutation({
    mutationFn: () => api.clearPluginKey(plugin.name),
    onSuccess: (data) => {
      invalidate()
      if (data.ok) {
        toast(
          data.plugin_available
            ? '已清除界面配置的 Key(.env 中的同名变量仍然生效)'
            : 'Key 已清除,插件不再可用',
          'success',
        )
      }
    },
    onError: (e: Error) => toast(`清除失败: ${e.message}`, 'error'),
  })

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <KeyRound className="h-3.5 w-3.5 text-secondary" />
        <h3 className="text-xs font-medium text-foreground">API Key</h3>
        <span className="text-[10px] text-muted/50 uppercase tracking-wider">{plugin.api_key_env}</span>
      </div>

      {/* 申请说明 + 官网链接 (对齐 TickFlow Key 区话术) */}
      <p className="text-xs text-secondary leading-relaxed mb-4">
        {plugin.homepage ? (
          <>
            在{' '}
            <a
              href={plugin.homepage}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline inline-flex items-baseline gap-0.5"
            >
              {plugin.display_name} 官网
              <ExternalLink className="h-3 w-3 self-center" />
            </a>
            {' '}申请获取。
          </>
        ) : (
          <>向该数据源官方申请 API Key。</>
        )}
        Key 仅存本地 (secrets.json 优先, 环境变量 {plugin.api_key_env} 兜底),不会上传任何第三方,请妥善保管。
      </p>

      {/* 当前状态 */}
      <div className="flex items-center justify-between mb-4">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-muted">状态</div>
          <div className="mt-1 flex items-center gap-2 min-w-0">
            {plugin.available ? (
              <>
                <CheckCircle2 className="h-4 w-4 text-bear shrink-0" />
                <span className="text-sm font-medium shrink-0">已配置</span>
                {/* 生效 Key 脱敏串 (secrets.json 优先, .env 兜底) — 常驻显示, 与 TickFlow Key 一致 */}
                {plugin.api_key_masked && (
                  <span className="font-mono text-xs text-secondary truncate" title={plugin.api_key_masked}>
                    {plugin.api_key_masked}
                  </span>
                )}
              </>
            ) : (
              <>
                <AlertCircle className="h-4 w-4 text-muted shrink-0" />
                <span className="text-sm font-medium text-muted shrink-0">未配置</span>
                <span className="text-xs text-muted/70 truncate" title={plugin.status}>{plugin.status}</span>
              </>
            )}
          </div>
        </div>
        {plugin.available && (
          <button
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated text-secondary hover:text-danger text-xs transition-colors duration-150 ease-smooth disabled:opacity-50 shrink-0"
          >
            <Trash2 className="h-3 w-3" />
            清除
          </button>
        )}
      </div>

      {/* 输入 */}
      <form
        onSubmit={(e) => { e.preventDefault(); if (keyInput.trim()) save.mutate() }}
        className="space-y-2"
      >
        <div className="relative">
          <input
            type={revealing ? 'text' : 'password'}
            placeholder={plugin.available ? '粘贴新 Key 替换当前' : `粘贴 ${plugin.display_name} API Key`}
            value={keyInput}
            onChange={(e) => { setKeyInput(e.target.value); if (saved) setSaved(false) }}
            autoComplete="off"
            className="w-full px-3 py-2 pr-9 rounded-input bg-base border border-border text-sm font-mono focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth"
          />
          <button
            type="button"
            onClick={() => setRevealing((v) => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-foreground transition-colors duration-150 ease-smooth"
            tabIndex={-1}
            aria-label={revealing ? '隐藏' : '显示'}
          >
            {revealing ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
        <button
          type="submit"
          disabled={save.isPending || (!keyInput.trim() && !saved)}
          className="w-full h-9 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all"
        >
          {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
          {save.isPending ? '验证中...' : saved ? '已保存' : '保存并检测'}
        </button>
      </form>

      {/* 无效 Key —— 先探后存: 探测失败时不存储 */}
      {save.data && !save.data.ok && (
        <div className="mt-3 text-xs text-danger flex items-center gap-1.5">
          <AlertCircle className="h-3 w-3 shrink-0" />
          {save.data.error || 'Key 无效,未保存'}
        </div>
      )}
      {save.isError && (
        <div className="mt-3 text-xs text-danger">
          保存失败:{String((save.error as Error).message)}
        </div>
      )}
    </div>
  )
}

/** 能力芯片: 三态 — 服务中(高亮+勾) / 已适配(灰) / 档位锁定(锁, 仅 TickFlow) */
function CapabilityChips({ caps, servingSet, isTickFlow }: {
  caps: CapabilityRoute[]
  servingSet: Set<string>
  isTickFlow: boolean
}) {
  if (caps.length === 0) return <span className="text-[10px] text-muted/40">未声明能力</span>
  return (
    <div className="flex flex-wrap gap-1">
      {caps.map(cap => {
        const servingNow = servingSet.has(cap.id)
        const locked = isTickFlow && !cap.tf_available
        const cls = servingNow
          ? 'bg-accent/15 text-accent'
          : locked
            ? 'bg-warning/8 text-warning/70'
            : 'bg-elevated/60 text-muted/70'
        const title = servingNow
          ? `正在提供「${cap.label}」`
          : locked
            ? `TickFlow 需 ${tierReqText(cap.tf_tier)} · 当前档位未解锁`
            : `已适配「${cap.label}」`
        return (
          <span
            key={cap.id}
            title={title}
            className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] leading-none ${cls}`}
          >
            {servingNow
              ? <Check className="h-2.5 w-2.5" />
              : locked ? <Lock className="h-2.5 w-2.5" /> : null}
            {DATASET_LABEL[cap.id] || cap.id}
          </span>
        )
      })}
    </div>
  )
}

export function SettingsDataSourcesPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const sources = useQuery({ queryKey: QK.dataSources, queryFn: api.dataSources })
  const matrix = useQuery({ queryKey: QK.capabilityMatrix, queryFn: api.capabilityMatrix })
  const [selected, setSelected] = useState<string>('tickflow') // 当前在下方配置的源 name
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const builtin: DataSourceItem[] = sources.data?.builtin ?? []
  const pluginList: PluginDataSourceItem[] = sources.data?.plugins ?? []
  const customList: DataSourceItem[] = sources.data?.custom ?? []
  const errors = sources.data?.errors ?? []
  const activeName = prefs.data?.daily_data_provider || 'tickflow'

  const pluginItems: DataSourceItem[] = pluginList.map(p => ({
    name: p.name, display_name: p.display_name, datasets: p.datasets,
  }))
  const allItems = [
    ...builtin,
    ...pluginItems,
    ...customList,
  ]
  const customNames = new Set(customList.map(c => c.name))

  const selectedCustom = customList.find(s => s.name === selected)

  // ===== 各能力当前的有效提供方 (除权 same_as_daily = 跟随日K) =====
  // 用于能力芯片"服务中"态与详情卡标识: 路由切换在上方能力路由区, 这里只读展示
  const dailyPref = prefs.data?.daily_data_provider || 'tickflow'
  const adjPref = prefs.data?.adj_factor_provider || 'same_as_daily'
  const effProvider: Record<string, string> = {
    daily: dailyPref,
    adj_factor: adjPref === 'same_as_daily' ? dailyPref : adjPref,
    minute: prefs.data?.minute_data_provider || 'tickflow',
    full_minute: prefs.data?.full_minute_data_provider || 'tickflow',
    realtime: prefs.data?.realtime_data_provider || 'tickflow',
    depth5: prefs.data?.depth5_data_provider || 'tickflow',
    financial: prefs.data?.financial_data_provider || 'tickflow',
  }
  const servingDatasets = (name: string) =>
    Object.entries(effProvider).filter(([, v]) => v === name).map(([k]) => k)
  const servingSetOf = (name: string) => new Set(servingDatasets(name))

  const matrixCaps = matrix.data?.capabilities ?? []

  // 数据源集合/插件可用性/档位变化都会改变能力候选集 → 统一连带失效
  const invalidateSources = () => {
    qc.invalidateQueries({ queryKey: QK.dataSources })
    qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
    qc.invalidateQueries({ queryKey: QK.preferences })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }

  const reload = useMutation({
    mutationFn: api.reloadDataSources,
    onSuccess: () => {
      invalidateSources()
      toast('配置已重新加载', 'success')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteDataSource(name),
    onSuccess: () => {
      invalidateSources()
      setSelected('tickflow')
      setConfirmDelete(null)
      toast('数据源已删除', 'success')
    },
  })

  const switchProvider = useMutation({
    mutationFn: async (name: string) => {
      // 一键套用: 该源适配了哪些数据集就接管哪些, 其余回默认
      if (name === 'tickflow') {
        return api.updateDataProviders(DEFAULT_ROUTING)
      }
      const supported = new Set(
        allItems.find(s => s.name === name)?.datasets ?? []
      )
      const pick = (dataset: string) =>
        supported.has(dataset) ? name : DEFAULT_ROUTING[`${dataset}_data_provider` as ProviderField] ?? 'tickflow'
      return api.updateDataProviders({
        daily_data_provider: pick('daily'),
        adj_factor_provider: pick('adj_factor'),
        realtime_data_provider: pick('realtime'),
        minute_data_provider: pick('minute'),
        financial_data_provider: pick('financial'),
      })
    },
    onSuccess: (_d, name) => {
      invalidateSources()
      const display = allItems.find(s => s.name === name)?.display_name || name
      toast(`已让「${display}」接管其适配的能力`, 'success')
    },
    onError: (e: Error) => toast(`切换失败: ${e.message}`, 'error'),
  })

  const editExisting = useMutation({
    mutationFn: (name: string) => api.dataSource(name),
    onSuccess: (_data, name) => setSelected(name),
  })

  const installMut = useMutation({
    mutationFn: (name: string) => api.installPlugin(name),
    onSuccess: (data) => {
      invalidateSources()
      if (data.install_ok) {
        toast('插件依赖安装成功', 'success')
      } else {
        toast(data.install_message || '安装失败', 'error')
      }
    },
    onError: (e: Error) => toast(`安装失败: ${e.message}`, 'error'),
  })

  const uninstallMut = useMutation({
    mutationFn: (name: string) => api.uninstallPlugin(name),
    onSuccess: (data) => {
      invalidateSources()
      if (data.uninstall_ok) {
        toast(data.uninstall_message || '已卸载', 'success')
      } else {
        toast(data.uninstall_message || '卸载失败', 'error')
      }
    },
    onError: (e: Error) => toast(`卸载失败: ${e.message}`, 'error'),
  })

  // 插件 name → 状态 (供卡片渲染时判断 available/installing 等)
  const pluginMap = new Map(pluginList.map(p => [p.name, p]))

  return (
    <div className="space-y-5 max-w-5xl">
      {/* ===== 上区: 能力路由 (能力为主视图, 点标签切换提供方) ===== */}
      <CapabilityRoutingSection />

      {/* ===== 下区: 数据源 (源为主视图, 配置/接入) ===== */}
      <AnchorWrap highlight={highlight} anchor="data-sources">
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5 min-w-0">
            <Database className="h-4 w-4 text-secondary shrink-0" />
            <h2 className="text-sm font-medium text-foreground">数据源</h2>
            <span
              className="text-[10px] text-muted/40 font-mono truncate hidden lg:inline max-w-[480px]"
              title={sources.data?.config_dir}
            >
              {sources.data?.config_dir}
            </span>
          </div>
          <button
            onClick={() => reload.mutate()}
            disabled={reload.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn text-xs text-muted hover:text-foreground hover:bg-elevated transition-colors disabled:opacity-50 shrink-0"
          >
            <RefreshCw className={`h-3 w-3 ${reload.isPending ? 'animate-spin' : ''}`} />
            重新加载
          </button>
        </div>

        {/* 插件化说明 (置顶黄色提示条): 接入自有行情 → 把文档交给 AI */}
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2.5">
          <Puzzle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <div className="text-[11px] leading-relaxed text-muted">
            <span className="text-secondary">数据源已插件化</span>
            ,接入自有行情?把文档发给 AI 即可自动接入:
            <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/custom-data-source.md</span>
            (自有 HTTP 接口) ·
            <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/plugin-development.md</span>
            (插件开发)
          </div>
        </div>

        {/* 数据源卡片 - 横向网格 (配置入口) */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {allItems.map(item => {
            const servingSet = servingSetOf(item.name)
            const isTf = item.name === 'tickflow'
            if (isTf) {
              // 名义路由到 tickflow 但当前档位提供不了的能力 → 按锁定态展示, 不算服务中
              for (const c of matrixCaps) {
                if (!c.tf_available) servingSet.delete(c.id)
              }
            }
            const servingCount = servingSet.size
            const isSelected = selected === item.name
            const plugin = pluginMap.get(item.name)
            const pluginUnavailable = plugin && !plugin.available
            const installing = installMut.isPending && installMut.variables === item.name
            const uninstalling = uninstallMut.isPending && uninstallMut.variables === item.name
            const declared = new Set(item.datasets)
            // TickFlow 展示注册表全量能力 (含档位锁定态); 其余源按声明过滤
            const chipCaps = isTf
              ? matrixCaps
              : matrixCaps.filter(c => declared.has(c.id))
            return (
              <div
                key={item.name}
                onClick={() => {
                  // 未就绪插件仅当支持界面配 Key 时可点开(进详情配置); 其余不可选
                  if (pluginUnavailable && !plugin?.api_key_env) return
                  setSelected(item.name)
                  // 只有用户自定义源 (YAML) 才进编辑器; tickflow 和插件不可编辑
                  if (customNames.has(item.name)) {
                    editExisting.mutate(item.name)
                  }
                }}
                className={`relative text-left rounded-lg border px-3.5 py-3 transition-all ${
                  pluginUnavailable && !plugin?.api_key_env
                    ? 'border-border/40 bg-elevated/10 opacity-70'
                    : isSelected
                      ? 'border-accent/50 bg-accent/5 ring-1 ring-accent/20 cursor-pointer'
                      : 'border-border/60 bg-elevated/20 hover:bg-elevated/40 cursor-pointer'
                }`}
              >
                {/* 名称行: 服务点 + 名称 + 档位/第三方标识 */}
                <div className="flex items-center gap-2">
                  <span
                    className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                      pluginUnavailable ? 'bg-muted/30' : servingCount > 0 ? 'bg-accent' : 'bg-transparent border border-muted/40'
                    }`}
                  />
                  <span className={`text-sm truncate flex-1 ${servingCount > 0 ? 'font-medium text-foreground' : 'text-secondary'}`}>
                    {item.display_name}
                  </span>
                  {isTf && matrix.data?.tickflow_tier && (
                    <TierTag label={matrix.data.tickflow_tier} />
                  )}
                  {!customNames.has(item.name) && (
                    <span className="shrink-0 rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">第三方</span>
                  )}
                </div>

                {/* 能力芯片: 服务中 / 已适配 / 档位锁定 */}
                <div className="mt-2">
                  <CapabilityChips caps={chipCaps} servingSet={servingSet} isTickFlow={isTf} />
                </div>

                {/* 底部: 状态提示 + 操作 */}
                <div className="mt-2 pt-2 border-t border-border/50 flex items-center justify-between gap-2 min-h-[22px]">
                  <span className="text-[10px] text-muted/50 truncate min-w-0">
                    {servingCount > 0
                      ? `服务中 ${servingCount} 项能力`
                      : pluginUnavailable
                        ? (plugin?.runtime === 'none' ? '点击配置 Key' : (plugin?.install_hint || plugin?.status || ''))
                        : ''}
                  </span>
                  <div className="flex items-center gap-1 shrink-0">
                    {pluginUnavailable ? (
                      plugin?.runtime !== 'none' && (
                        installing ? (
                          <span className="inline-flex items-center gap-1 text-[10px] text-accent">
                            <RefreshCw className="h-2.5 w-2.5 animate-spin" /> 安装中...
                          </span>
                        ) : (
                          <button
                            onClick={(e) => { e.stopPropagation(); installMut.mutate(item.name) }}
                            disabled={installMut.isPending}
                            className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors disabled:opacity-50"
                          >
                            安装
                          </button>
                        )
                      )
                    ) : (
                      <>
                        <button
                          onClick={(e) => { e.stopPropagation(); switchProvider.mutate(item.name) }}
                          disabled={switchProvider.isPending}
                          className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors disabled:opacity-50"
                        >
                          套用
                        </button>
                        {plugin && plugin?.runtime !== 'none' && (
                          uninstalling ? (
                            <RefreshCw className="h-2.5 w-2.5 animate-spin text-muted" />
                          ) : (
                            <button
                              onClick={(e) => { e.stopPropagation(); uninstallMut.mutate(item.name) }}
                              disabled={uninstallMut.isPending}
                              className="text-[10px] text-muted/50 hover:text-danger transition-colors disabled:opacity-40"
                              title="卸载依赖"
                            >
                              卸载
                            </button>
                          )
                        )}
                      </>
                    )}
                  </div>
                </div>
              </div>
            )
          })}

          {/* 新增数据源卡片 */}
          <button
            onClick={() => setSelected('__new__')}
            className={`rounded-lg border border-dashed px-3.5 py-3 transition-all flex items-center justify-center gap-1.5 text-sm ${
              selected === '__new__'
                ? 'border-accent/50 bg-accent/5 text-accent'
                : 'border-border/50 text-muted hover:text-foreground hover:border-border hover:bg-elevated/30'
            }`}
          >
            <Plus className="h-3.5 w-3.5" />
            新增数据源
          </button>
        </div>

        {/* 错误提示 */}
        {errors.length > 0 && (
          <div className="mt-3 flex items-start gap-1.5 px-3 py-2 rounded-lg bg-danger/5 border border-danger/20">
            <FileWarning className="h-3.5 w-3.5 text-danger shrink-0 mt-0.5" />
            <div className="text-[11px] text-danger/80 leading-relaxed space-y-0.5">
              {errors.map((err, idx) => (
                <div key={idx}>
                  <span className="font-mono">{err.name || err.path}</span>: {err.errors.join('; ')}
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-3 flex items-center gap-3 text-[10px] text-muted/50 flex-wrap">
          <span>芯片: 高亮=服务中 · 灰=已适配 · <Lock className="inline h-2.5 w-2.5" />=需更高档位</span>
          <span className="text-muted/30">·</span>
          <span>单击卡片查看介绍与配置, 点「套用」让该源接管其适配的全部能力</span>
        </div>

      </section>
      </AnchorWrap>

      {/* ===== 下方: 选中源的介绍 + 配置 (单一卡片布局) ===== */}
      <AnimatePresence mode="wait">
        <motion.div
          key={selected}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.15 }}
        >
          {selected === 'tickflow' ? (
            <TickFlowDetail active={servingDatasets('tickflow').length > 0} matrix={matrix.data} />
          ) : selected === '__new__' || customNames.has(selected) ? (
            selected === '__new__' ? (
              <DataSourceEditor
                key={selected}
                initial={null}
                existingName={undefined}
                onCancel={() => setSelected('tickflow')}
                onSaved={() => {
                  invalidateSources()
                  setSelected('tickflow')
                }}
                activeName={activeName}
                onActivate={(name) => switchProvider.mutate(name)}
              />
            ) : (
              <DataSourceEditor
                key={selected}
                initial={null}
                existingName={selected}
                onCancel={() => setSelected('tickflow')}
                onSaved={() => {
                  invalidateSources()
                  // 强制清除该源的详情缓存, 下次编辑重新拉取最新配置
                  qc.removeQueries({ queryKey: ['data-source-detail', selected] })
                }}
                activeName={activeName}
                onActivate={(name) => switchProvider.mutate(name)}
                onDelete={selectedCustom ? () => setConfirmDelete(selected) : undefined}
              />
            )
          ) : pluginList.find(x => x.name === selected) ? (
            <PluginDetail
              plugin={pluginList.find(x => x.name === selected)!}
              isActive={servingDatasets(selected).length > 0}
              matrixCaps={matrixCaps}
              servingSet={servingSetOf(selected)}
            />
          ) : null}
        </motion.div>
      </AnimatePresence>

      {/* 删除确认弹窗 */}
      {confirmDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setConfirmDelete(null)}
          />
          <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
            <h3 className="text-sm font-medium text-foreground mb-2">删除数据源</h3>
            <p className="text-xs text-secondary mb-5">
              确认删除「{customList.find(s => s.name === confirmDelete)?.display_name || confirmDelete}」? 该数据源的配置文件将被移除,此操作不可撤销。
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setConfirmDelete(null)}
                className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => remove.mutate(confirmDelete)}
                disabled={remove.isPending}
                className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50"
              >
                {remove.isPending ? '删除中...' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/** 插件详情: 介绍 + 适配能力 + Key 配置合并为单一卡片 */
function PluginDetail({ plugin, isActive, matrixCaps, servingSet }: {
  plugin: PluginDataSourceItem
  isActive: boolean
  matrixCaps: CapabilityRoute[]
  servingSet: Set<string>
}) {
  const declared = new Set(plugin.datasets)
  return (
    <section className="rounded-card border border-border bg-surface p-6">
      {/* 介绍 */}
      <div className="flex items-start gap-4">
        <div className="h-11 w-11 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
          <Zap className="h-5 w-5 text-accent" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <h3 className="text-[16px] leading-6 font-semibold text-foreground">{plugin.display_name}</h3>
            <span className="text-[10px] text-muted/50 uppercase tracking-wider">插件 · {plugin.runtime}</span>
            <span className="rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">第三方</span>
            {isActive && (
              <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-2 py-1 rounded">
                <Check className="h-2.5 w-2.5" /> 服务中
              </span>
            )}
          </div>
          {plugin.description && <p className="text-xs text-secondary leading-relaxed">{plugin.description}</p>}
          <div className="mt-2">
            <CapabilityChips
              caps={matrixCaps.filter(c => declared.has(c.id))}
              servingSet={servingSet}
              isTickFlow={false}
            />
          </div>
        </div>
      </div>

      {/* 主体: 操作 + Key 配置 (左) | 能力适配表 (右), 布局对齐 TickFlow 详情 */}
      <div className="mt-5 pt-5 border-t border-border grid grid-cols-1 lg:grid-cols-[1fr_1.15fr] gap-6 items-start">
        <div className="min-w-0">
          {/* 独立状态行仅用于无 Key 配置区的插件; 有 Key 区时「状态」行已展示, 避免重复 */}
          {!plugin.available && !plugin.api_key_env && (
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted">{plugin.status}</span>
            </div>
          )}

          {/* API Key 配置 (声明了 api_key_env 的插件) */}
          {plugin.api_key_env && (
            <div className={plugin.available ? 'mt-4' : ''}>
              <PluginKeyConfig plugin={plugin} />
            </div>
          )}
        </div>

        {/* 能力适配表: 全部能力 × 该源适配状态 (样式对齐 TickFlow 能力档位表) */}
        <div className="min-w-0">
          <div className="rounded-lg border border-border/60 bg-elevated/20 divide-y divide-border/50">
            {matrixCaps.map(cap => {
              const Icon = CAP_ICON[cap.id] ?? Database
              const serving = servingSet.has(cap.id)
              const declaredCap = declared.has(cap.id)
              return (
                <div key={cap.id} className="flex items-center gap-2.5 px-3 py-1.5">
                  <Icon className="h-3.5 w-3.5 text-secondary shrink-0" />
                  <div className="flex-1 min-w-0 truncate">
                    <span className="text-xs text-foreground">{cap.label}</span>
                    <span className="ml-1.5 text-[10px] text-muted/60">{cap.desc}</span>
                  </div>
                  {serving ? (
                    <span className="w-[56px] text-right text-[10px] text-bear inline-flex items-center justify-end gap-0.5 shrink-0">
                      <Check className="h-2.5 w-2.5" />服务中
                    </span>
                  ) : declaredCap ? (
                    <span className={`w-[56px] text-right text-[10px] shrink-0 ${plugin.available ? 'text-secondary' : 'text-warning/80'}`}>
                      {plugin.available ? '已适配' : '未就绪'}
                    </span>
                  ) : (
                    <span className="w-[56px] text-right text-[10px] text-muted/30 shrink-0">—</span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}

/** TickFlow 详情: 介绍 + 能力档位表 + Key/可用功能左右两栏。
 *  检测档位集群 (档位徽章 + ? 说明 + 重新检测 + 可用功能悬停) 挂在 API Key 标题行右侧。 */
function TickFlowDetail({ active, matrix }: { active: boolean; matrix?: CapabilityMatrix }) {
  const caps = matrix?.capabilities ?? []
  const tier = matrix?.tickflow_tier
  const { data: tfCaps } = useCapabilities()
  const capEntries = tfCaps ? Object.entries(tfCaps.capabilities) : []
  const invalidate = useInvalidateTierRelated()
  const redetect = useMutation({
    mutationFn: () => api.redetectCapabilities(),
    onSuccess: () => invalidate(),
  })
  // 检测档位集群: API Key 标题行右侧 (检测档位徽章 + 档位说明 + 重检测 + 可用功能悬停)
  const tierCluster = tier ? (
    <div className="flex items-center gap-1.5 shrink-0">
      <span className="text-[10px] text-muted/80">检测档位</span>
      <TierTag label={tier} />
      <TierHelpPopover currentLabel={tfCaps?.label ?? tier} />
      <button
        type="button"
        onClick={() => redetect.mutate()}
        disabled={redetect.isPending}
        title="根据 API Key 重新检测订阅档位"
        className="inline-flex h-5 w-5 items-center justify-center rounded text-muted/60 hover:text-foreground hover:bg-elevated/70 transition-colors duration-150"
      >
        <RefreshCw className={`h-3 w-3 ${redetect.isPending ? 'animate-spin' : ''}`} />
      </button>
      {/* 可用功能: 收进悬停浮层, 不占版面 (能力清单 + 限频)。图标在标题行右侧, 浮层向左展开。
          外层 top-full + pt-1.5: 间隙用内边距做, hover 区与图标无缝衔接 (mt 间隙会断 hover 链);
          不设 pointer-events-none, 否则鼠标移不进浮层、列表无法滚动。 */}
      <div className="relative group/caps shrink-0" aria-label="可用功能">
        <span className="flex h-5 w-5 items-center justify-center rounded text-muted/60 transition-colors group-hover/caps:text-foreground">
          <ListChecks className="h-3 w-3" />
        </span>
        <div className="invisible absolute right-0 top-full z-20 pt-1.5 opacity-0 transition-all duration-150 group-hover/caps:visible group-hover/caps:opacity-100">
          <div className="w-64 rounded-md border border-border bg-surface py-2 pl-3 pr-3.5 shadow-2xl shadow-black/40">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium text-foreground">可用功能</span>
              <span className="text-[10px] font-mono text-muted">{capEntries.length} 项</span>
            </div>
            {capEntries.length > 0 ? (
              <div className="max-h-56 space-y-1 overflow-y-auto border-t border-border/60 pt-1.5">
                {capEntries.map(([cap, lim]) => (
                  <div key={cap} className="flex min-w-0 items-baseline gap-2">
                    <span className="min-w-0 flex-1 truncate text-[11px] text-secondary">
                      {CAP_LABELS[cap]?.name ?? cap}
                    </span>
                    <span className="shrink-0 font-mono text-[10px] text-muted">
                      {lim.rpm ? `${lim.rpm}/min` : lim.subscribe ? `${lim.subscribe} 订阅` : '—'}
                      {lim.batch ? ` · ${lim.batch}/次` : ''}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="border-t border-border/60 pt-1.5 text-[11px] text-muted">
                暂无 — 配置 API Key 后自动检测
              </div>
            )}
            <div className="mt-1.5 border-t border-border/60 pt-1.5 text-[10px] text-muted/70">
              根据 API Key 自动检测
            </div>
          </div>
        </div>
      </div>
    </div>
  ) : null
  return (
    <section className="rounded-card border border-border bg-surface p-6">
      {/* 介绍 */}
      <div className="flex items-start gap-4">
        <div className="h-11 w-11 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
          <Database className="h-5 w-5 text-accent" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-[16px] leading-6 font-semibold text-foreground">TickFlow</h2>
            <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning">第三方</span>
            <AllTiersBadge />
            {active && (
              <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-2 py-1 rounded">
                <Check className="h-2.5 w-2.5" /> 服务中
              </span>
            )}
          </div>
          <p className="text-xs text-secondary mt-1.5 leading-relaxed">
            默认数据源,每个能力所需订阅档位见下表 — 当前档位未解锁的能力不会出现在上方「能力路由」的选项里。
            未单独设置的能力默认由 TickFlow 提供;也可在数据源区接入插件替换任意能力。
          </p>
        </div>
      </div>

      {/* 主体: API Key 配置 (左) | 能力档位表 (右) */}
      <div className="mt-5 pt-5 border-t border-border grid grid-cols-1 lg:grid-cols-[1fr_1.15fr] gap-6 items-start">
        <div className="min-w-0">
          <TickFlowKeySection right={tierCluster} />
        </div>
        <div className="min-w-0">
          {/* 能力档位表: 各能力所需档位 + 当前档位可用性 */}
          {caps.length > 0 && (
            <div className="rounded-lg border border-border/60 bg-elevated/20 divide-y divide-border/50">
              {caps.map(cap => {
                const Icon = CAP_ICON[cap.id] ?? Database
                return (
                  <div key={cap.id} className="flex items-center gap-2.5 px-3 py-1.5">
                    <Icon className="h-3.5 w-3.5 text-secondary shrink-0" />
                    <div className="flex-1 min-w-0 truncate">
                      <span className="text-xs text-foreground">{cap.label}</span>
                      <span className="ml-1.5 text-[10px] text-muted/60">{cap.desc}</span>
                    </div>
                    <TierReqChip tier={cap.tf_tier} currentLabel={tier} />
                    {cap.tf_available ? (
                      <span className="w-[52px] text-right text-[10px] text-bear inline-flex items-center justify-end gap-0.5 shrink-0">
                        <Check className="h-2.5 w-2.5" />可用
                      </span>
                    ) : (
                      <span className="w-[52px] text-right text-[10px] text-warning/80 shrink-0">未解锁</span>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
