import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, Database, Eye, EyeOff, KeyRound, Plus, RefreshCw, Zap, FileWarning, Puzzle, AlertCircle, CheckCircle2, Loader2, Save, Trash2 } from 'lucide-react'
import { api, type DataSourceItem, type PluginDataSourceItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilities, usePreferences } from '@/lib/useSharedQueries'
import { TIER_RANK, tierRank, tierStyle } from '@/lib/capability-labels'
import { toast } from '@/components/Toast'
import { DataSourceEditor } from './DataSourceEditor'
import { TickFlowKeyConfig } from './Keys'

const DATASET_LABEL: Record<string, string> = {
  daily: '日K',
  adj_factor: '除权',
  realtime: '实时',
  minute: '分钟',
  financial: '财务',
  auction: '竞价',
}

/** 数据集 → 路由偏好字段 + 默认值 + 展示标签 (financial 无后端路由消费方, 仅展示不参与切换) */
/** 能力卡片定义: 数据集 + 说明 (路由选择嵌入每张卡片) */
const CAPABILITY_CARDS = [
  { dataset: 'daily', label: '日K', desc: '历史 + 实时覆写' },
  { dataset: 'adj_factor', label: '除权因子', desc: '复权计算' },
  { dataset: 'realtime', label: '实时行情', desc: '全市场快照' },
  { dataset: 'minute', label: '分钟K', desc: '分时图 · 回测' },
] as const

/** 数据集 → 路由偏好字段 + 默认值 (financial 无后端路由消费方, 仅展示) */
const DATASET_ROUTE: Record<string, {
  field: 'daily_data_provider' | 'adj_factor_provider' | 'minute_data_provider' | 'realtime_data_provider'
  def: string
}> = {
  daily: { field: 'daily_data_provider', def: 'tickflow' },
  adj_factor: { field: 'adj_factor_provider', def: 'same_as_daily' },
  minute: { field: 'minute_data_provider', def: 'tickflow' },
  realtime: { field: 'realtime_data_provider', def: 'tickflow' },
}

/** 各能力在 TickFlow 需要的最低订阅档位 (对照 tiers.yaml: 日K 全档位可用,
 *  全市场实时/除权因子需 Starter+, 分钟K需 Pro+, 财务需 Expert+) */
const TICKFLOW_TIER_REQ: Record<string, string> = {
  daily: 'none',
  adj_factor: 'starter',
  realtime: 'starter',
  minute: 'pro',
  financial: 'expert',
}

/** TickFlow 档位要求徽标: 按所需档位配色(与左侧菜单/Key 页一致), 当前档位不足时琥珀描边提示 */
function TierReqChip({ tier, currentLabel }: { tier: string; currentLabel?: string }) {
  const text = tier === 'none' ? '全档位' : `${tier}+`
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

/** 卡片内静态数据集标签: 只展示该源适配了哪些数据集 (路由选择在下方能力卡片) */
function DatasetChipRow({ datasets }: { datasets: string[] }) {
  if (datasets.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-1 ml-3.5 mt-1">
      <span className="text-[9px] font-medium text-accent bg-accent/10 px-1 py-0.5 rounded">已适配</span>
      {datasets.map(ds => (
        <span key={ds} className="text-[9px] text-muted/60 bg-elevated/60 px-1 py-0.5 rounded">
          {DATASET_LABEL[ds] || ds}
        </span>
      ))}
    </div>
  )
}

/** 按能力路由网格: 每个数据源详情下展示一张能力卡,卡片上以可点标签列出
 *  所有具备该能力的数据源 — 点谁,该数据集就立刻由谁提供,可跨源自由组合。
 *  TickFlow 详情含全部能力; 其他源只列自己参与的能力。 */
function SourceCapabilityGrid({ sourceName, sourceDisplay, datasets, candidatesOf, providerOf, pending, onSelect, anyCustom, onReset }: {
  sourceName: string
  sourceDisplay: string
  datasets: string[]
  /** 该数据集的所有候选提供方 (含 TickFlow), 按推荐顺序 */
  candidatesOf: (dataset: string) => { name: string; display: string }[]
  /** 该数据集当前的原始路由偏好值 (adj_factor 可能是 same_as_daily) */
  providerOf: (dataset: string) => string
  pending?: boolean
  onSelect: (dataset: string, provider: string) => void
  anyCustom?: boolean
  onReset?: () => void
}) {
  const isDefault = sourceName === 'tickflow'
  const dsList = isDefault ? [...datasets, 'financial'] : datasets
  const caps = useCapabilities()
  if (dsList.length === 0) return null

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] text-muted">
          {isDefault
            ? '每个能力可单独选择提供方 — 点标签即刻切换,未单独设置的由 TickFlow 提供'
            : `${sourceDisplay} 参与的能力 — 每个能力都可单独选择由哪个数据源提供`}
        </span>
        {isDefault && anyCustom && (
          <button
            onClick={onReset}
            disabled={pending}
            className="text-[11px] text-muted/60 hover:text-accent transition-colors disabled:opacity-50"
          >
            恢复默认
          </button>
        )}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5 mb-2">
        {dsList.map(ds => {
          const route = DATASET_ROUTE[ds]
          const meta = CAPABILITY_CARDS.find(c => c.dataset === ds)
          const label = meta?.label || DATASET_LABEL[ds] || ds
          const desc = meta?.desc || ''
          if (!route) {
            return (
              <div key={ds} className="rounded-lg border border-border/50 bg-elevated/20 px-3 py-2.5" title="该数据集暂不支持切换数据源">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-foreground truncate">{label}</div>
                    {desc && <div className="text-[10px] text-muted mt-0.5">{desc}</div>}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {isDefault && <TierReqChip tier={TICKFLOW_TIER_REQ[ds]} currentLabel={caps.data?.label} />}
                    <span className="shrink-0 px-1.5 py-0.5 rounded text-[10px] text-muted/50 bg-elevated/60">固定</span>
                  </div>
                </div>
              </div>
            )
          }
          const raw = providerOf(ds)
          const candidates = candidatesOf(ds)
          return (
            <div key={ds} className="rounded-lg border border-border/50 bg-elevated/20 px-3 py-2.5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-xs font-medium text-foreground">{label}</div>
                  {desc && <div className="text-[10px] text-muted mt-0.5">{desc}</div>}
                </div>
                {/* 右上角: TickFlow 所需档位 (当前提供方由下方高亮标签指示) */}
                {isDefault && <TierReqChip tier={TICKFLOW_TIER_REQ[ds]} currentLabel={caps.data?.label} />}
              </div>
              {/* 提供方标签: 点谁该数据集就由谁提供,当前项高亮 */}
              <div className="flex flex-wrap gap-1 mt-2 pt-2 border-t border-border/50">
                {ds === 'adj_factor' && (
                  <button
                    type="button"
                    disabled={pending}
                    onClick={() => onSelect(ds, 'same_as_daily')}
                    title="除权因子跟随日K数据源"
                    className={tagCls(raw === 'same_as_daily')}
                  >
                    跟随日K
                  </button>
                )}
                {candidates.map(c => (
                  <button
                    key={c.name}
                    type="button"
                    disabled={pending}
                    onClick={() => onSelect(ds, c.name)}
                    title={`「${label}」由 ${c.display} 提供`}
                    className={tagCls(raw === c.name)}
                  >
                    {c.display}
                  </button>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 提供方标签样式: 当前项高亮(accent), 其余弱化可点 */
function tagCls(active: boolean) {
  return `px-1.5 py-0.5 rounded text-[10px] transition-colors select-none disabled:opacity-50 cursor-pointer ${
    active
      ? 'bg-accent/15 text-accent font-medium'
      : 'bg-elevated/60 text-muted/70 hover:bg-accent/15 hover:text-accent'
  }`
}


/** 详情组件所需的路由上下文(由面板构造) */
interface RouteCtx {
  /** 数据集 → 候选提供方列表 (含 TickFlow) */
  candidatesOf: (dataset: string) => { name: string; display: string }[]
  /** 数据集 → 当前原始路由偏好值 (adj_factor 可能是 same_as_daily) */
  providerOf: (dataset: string) => string
  displayOf: (name?: string) => string
  pending: boolean
  onSelect: (dataset: string, provider: string) => void
  anyCustom: boolean
  onReset: () => void
}

/** 插件 API Key 配置区 (布局对齐 TickFlowKeyConfig: 状态 + 输入 + 保存并检测)。
 *  先探后存: 后端用候选 Key 实探一次, 无效不落盘; secrets.json 优先于 .env。 */
function PluginKeyConfig({ plugin }: { plugin: PluginDataSourceItem }) {
  const qc = useQueryClient()
  const [keyInput, setKeyInput] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [saved, setSaved] = useState(false)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.dataSources })
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
    <section className="rounded-card border border-border bg-surface p-6">
      <div className="flex items-center gap-2.5 mb-3">
        <KeyRound className="h-4 w-4 text-secondary" />
        <h3 className="text-sm font-medium text-foreground">API Key</h3>
        <span className="text-[10px] text-muted/50 uppercase tracking-wider">{plugin.api_key_env}</span>
      </div>
      <p className="text-xs text-secondary leading-relaxed mb-4">
        Key 保存为本地文件(secrets.json, 优先级高于 .env),不会上传任何第三方。保存前会先用该 Key
        实探一次数据接口,无效则不落盘。
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
                {save.data?.ok && save.data.api_key_masked && (
                  <span className="font-mono text-xs text-secondary truncate">{save.data.api_key_masked}</span>
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
          className="w-full h-10 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all"
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
    </section>
  )
}

export function SettingsDataSourcesPanel() {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const sources = useQuery({ queryKey: QK.dataSources, queryFn: api.dataSources })
  const [selected, setSelected] = useState<string>('tickflow') // 当前在右侧编辑的源 name
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const reload = useMutation({
    mutationFn: api.reloadDataSources,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dataSources })
      // 重载可能改变数据集声明 → 能力与实时模式随之变化
      qc.invalidateQueries({ queryKey: QK.capabilities })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
      toast('配置已重新加载', 'success')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteDataSource(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dataSources })
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
      setSelected('tickflow')
      setConfirmDelete(null)
      toast('数据源已删除', 'success')
    },
  })

  const switchProvider = useMutation({
    mutationFn: async (name: string) => {
      // tickflow: 全量重置为默认路由
      if (name === 'tickflow') {
        return api.updateDataProviders({
          daily_data_provider: 'tickflow',
          adj_factor_provider: 'same_as_daily',
          realtime_data_provider: 'tickflow',
          minute_data_provider: 'tickflow',
          financial_data_provider: 'tickflow',
        })
      }
      // 非 tickflow: 该源适配了哪些数据集就接管哪些, 其余回退默认
      const supported = new Set(
        allItems.find(s => s.name === name)?.datasets ?? []
      )
      const pick = (dataset: string) =>
        supported.has(dataset) ? name : (dataset === 'adj_factor' ? 'same_as_daily' : 'tickflow')
      return api.updateDataProviders({
        daily_data_provider: pick('daily'),
        adj_factor_provider: pick('adj_factor'),
        realtime_data_provider: pick('realtime'),
        minute_data_provider: pick('minute'),
        financial_data_provider: 'tickflow',
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      // 切换会改变实时行情 provider → 模式(none/watchlist/full_market)立即刷新
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
      toast('数据源已切换', 'success')
    },
  })

  const editExisting = useMutation({
    mutationFn: (name: string) => api.dataSource(name),
    onSuccess: (_data, name) => setSelected(name),
  })

  const installMut = useMutation({
    mutationFn: (name: string) => api.installPlugin(name),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.dataSources })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
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
      qc.invalidateQueries({ queryKey: QK.dataSources })
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
      if (data.uninstall_ok) {
        toast(data.uninstall_message || '已卸载', 'success')
      } else {
        toast(data.uninstall_message || '卸载失败', 'error')
      }
    },
    onError: (e: Error) => toast(`卸载失败: ${e.message}`, 'error'),
  })

  const builtin: DataSourceItem[] = sources.data?.builtin ?? []
  const pluginList: PluginDataSourceItem[] = sources.data?.plugins ?? []
  const customList: DataSourceItem[] = sources.data?.custom ?? []
  const errors = sources.data?.errors ?? []
  const activeName = prefs.data?.daily_data_provider || 'tickflow'

  // 插件 name → 状态 (供卡片渲染时判断 available/installing 等)
  const pluginMap = new Map(pluginList.map(p => [p.name, p]))
  const pluginNames = new Set(pluginList.map(p => p.name))

  // 顶部数据源选择列表 (内置 + 所有插件 + 自定义 + 新增)
  const pluginItems: DataSourceItem[] = pluginList.map(p => ({
    name: p.name, display_name: p.display_name, datasets: p.datasets,
  }))
  const allItems = [
    ...builtin,
    ...pluginItems,
    ...customList,
  ]

  const selectedCustom = customList.find(s => s.name === selected)

  // ===== 各数据集当前的有效提供方 (除权 same_as_daily = 跟随日K) =====
  // 用于"服务中"徽标: 改单个能力路由只影响对应数据集, 不再产生"当前数据源被切换"的表现
  const dailyPref = prefs.data?.daily_data_provider || 'tickflow'
  const adjPref = prefs.data?.adj_factor_provider || 'same_as_daily'
  const effProvider: Record<string, string> = {
    daily: dailyPref,
    adj_factor: adjPref === 'same_as_daily' ? dailyPref : adjPref,
    minute: prefs.data?.minute_data_provider || 'tickflow',
    realtime: prefs.data?.realtime_data_provider || 'tickflow',
  }
  const servingDatasets = (name: string) =>
    Object.entries(effProvider).filter(([, v]) => v === name).map(([k]) => k)
  const servingLabels = (name: string) =>
    servingDatasets(name).map(k => DATASET_LABEL[k] || k)

  const displayOf = (name?: string) =>
    name === 'tickflow' ? 'TickFlow'
      : name === 'same_as_daily' ? '跟随日K'
        : allItems.find(s => s.name === name)?.display_name || name || ''

  const invalidateRouting = () => {
    qc.invalidateQueries({ queryKey: QK.preferences })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }

  // 单个能力的提供方切换: 点标签即刻生效 (same_as_daily 仅除权有效 = 跟随日K)
  const routeMut = useMutation({
    mutationFn: ({ dataset, provider }: { dataset: string; provider: string }) => {
      const route = DATASET_ROUTE[dataset]
      if (!route) throw new Error(`数据集 ${dataset} 不支持切换数据源`)
      return api.updateDataProviders({ [route.field]: provider })
    },
    onSuccess: (_d, v) => {
      invalidateRouting()
      const label = DATASET_LABEL[v.dataset] || v.dataset
      toast(`「${label}」已切换为 ${displayOf(v.provider)}`, 'success')
    },
    onError: (e: Error) => toast(`路由切换失败: ${e.message}`, 'error'),
  })

  // 恢复默认: 路由全部回 TickFlow
  const resetRouteMut = useMutation({
    mutationFn: () => api.updateDataProviders({
      daily_data_provider: 'tickflow',
      adj_factor_provider: 'same_as_daily',
      minute_data_provider: 'tickflow',
      realtime_data_provider: 'tickflow',
    }),
    onSuccess: () => {
      invalidateRouting()
      toast('数据集路由已恢复默认(TickFlow)', 'success')
    },
    onError: (e: Error) => toast(`恢复失败: ${e.message}`, 'error'),
  })

  const anyCustomRouting = Object.values(effProvider).some(v => v !== 'tickflow') || adjPref !== 'same_as_daily'

  // 某数据集的全部候选提供方 (TickFlow 恒在首位, 其余按声明该数据集的数据源列出)
  const candidatesOf = (dataset: string) => {
    const list = [{ name: 'tickflow', display: 'TickFlow' }]
    for (const item of allItems) {
      if (item.name !== 'tickflow' && item.datasets.includes(dataset)) {
        list.push({ name: item.name, display: item.display_name || item.name })
      }
    }
    return list
  }

  // 数据集 → 当前原始路由偏好值 (adj_factor 保留 same_as_daily 以驱动"跟随日K"标签态)
  const providerOf = (dataset: string) => {
    switch (dataset) {
      case 'daily': return dailyPref
      case 'adj_factor': return adjPref
      case 'minute': return prefs.data?.minute_data_provider || 'tickflow'
      case 'realtime': return prefs.data?.realtime_data_provider || 'tickflow'
      default: return 'tickflow'
    }
  }

  // 传给各数据源详情的路由上下文(能力卡标签选择 + 当前提供方展示)
  const routeCtx: RouteCtx = {
    candidatesOf,
    providerOf,
    displayOf,
    pending: routeMut.isPending,
    onSelect: (dataset, provider) => routeMut.mutate({ dataset, provider }),
    anyCustom: anyCustomRouting,
    onReset: () => resetRouteMut.mutate(),
  }

  return (
    <div className="space-y-5 max-w-5xl">
      {/* ===== 顶部: 当前数据源 + 数据源选择 (一个大卡片) ===== */}
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <Database className="h-4 w-4 text-secondary" />
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
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn text-xs text-muted hover:text-foreground hover:bg-elevated transition-colors disabled:opacity-50"
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

        {/* 数据源选择 - 横向卡片列表 */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {allItems.map(item => {
            const serving = servingLabels(item.name)
            const isSelected = selected === item.name
            const plugin = pluginMap.get(item.name)
            const pluginUnavailable = plugin && !plugin.available
            const installing = installMut.isPending && installMut.variables === item.name
            const uninstalling = uninstallMut.isPending && uninstallMut.variables === item.name
            return (
              <div
                key={item.name}
                onClick={() => {
                  // 未就绪插件仅当支持界面配 Key 时可点开(进详情配置); 其余不可选
                  if (pluginUnavailable && !plugin?.api_key_env) return
                  setSelected(item.name)
                  // 只有用户自定义源 (YAML) 才进编辑器; tickflow 和插件不可编辑
                  if (customList.some(c => c.name === item.name)) {
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
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                      pluginUnavailable ? 'bg-muted/30' : serving.length > 0 ? 'bg-accent' : 'bg-transparent border border-muted/40'
                    }`}
                  />
                  <span className={`text-sm truncate flex-1 ${serving.length > 0 ? 'font-medium text-foreground' : 'text-secondary'}`}>
                    {item.display_name}
                  </span>
                  {serving.length > 0 && (
                    <span
                      className="shrink-0 inline-flex items-center gap-0.5 text-[9px] text-accent"
                      title={`正在提供: ${serving.join(' · ')}`}
                    >
                      <Check className="h-2.5 w-2.5" /> 服务中
                    </span>
                  )}
                  {item.name === 'tickflow' && (
                    <>
                      <span className="shrink-0 rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">第三方</span>
                      <AllTiersBadge size="text-[9px]" />
                    </>
                  )}
                  {pluginNames.has(item.name) && (
                    <span className="shrink-0 rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">第三方</span>
                  )}
                  {/* 右侧操作区: 插件未安装→安装按钮(runtime=none 无依赖可装,显示配置提示); 否则→使用/卸载 */}
                  {pluginUnavailable ? (
                    plugin?.runtime === 'none' ? (
                      <span
                        className="text-[10px] text-muted/50 shrink-0 cursor-help"
                        title={plugin?.status || '未配置凭据'}
                      >
                        点击配置 Key
                      </span>
                    ) : installing ? (
                      <span className="inline-flex items-center gap-1 text-[9px] text-accent shrink-0">
                        <RefreshCw className="h-2.5 w-2.5 animate-spin" /> 安装中...
                      </span>
                    ) : (
                      <button
                        onClick={(e) => { e.stopPropagation(); installMut.mutate(item.name) }}
                        disabled={installMut.isPending}
                        className="shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors disabled:opacity-50"
                      >
                        <Zap className="h-2.5 w-2.5" /> 安装
                      </button>
                    )
                  ) : plugin ? (
                    /* 已安装插件: 使用 + 卸载 */
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={(e) => { e.stopPropagation(); switchProvider.mutate(item.name) }}
                        disabled={switchProvider.isPending}
                        className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors disabled:opacity-50"
                      >
                        使用
                      </button>
                      {plugin?.runtime !== 'none' && (uninstalling ? (
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
                      ))}
                    </div>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); switchProvider.mutate(item.name) }}
                      disabled={switchProvider.isPending}
                      className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors disabled:opacity-50"
                    >
                      使用
                    </button>
                  )}
                </div>
                {/* 数据集标签(静态): 该源适配的数据集, 路由选择在下方能力卡片 */}
                <DatasetChipRow datasets={item.name === 'tickflow' ? [...item.datasets, 'financial'] : item.datasets} />
                {/* 未安装插件显示安装命令提示 */}
                {pluginUnavailable && plugin?.install_hint && (
                  <div className="ml-3.5 mt-1 text-[10px] text-muted/40 font-mono truncate">{plugin.install_hint}</div>
                )}
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

        <div className="mt-3 flex items-center gap-3 text-[10px] text-muted/50">
          <span>单击查看各源能力</span>
          <span className="text-muted/30">·</span>
          <span>能力卡片上点标签,单独选择每个数据集的提供方</span>
          <span className="text-muted/30">·</span>
          <span>点「使用」一键套用该源全部能力</span>
          <span className="text-muted/30">·</span>
          <span>未单独设置的由 TickFlow 提供</span>
        </div>

      </section>

      {/* ===== 下方: 编辑区 ===== */}
      <AnimatePresence mode="wait">
        <motion.div
          key={selected}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.15 }}
        >
          {selected === 'tickflow' ? (
            <TickFlowDetail
              active={servingDatasets('tickflow').length > 0}
              onSwitch={() => switchProvider.mutate('tickflow')}
              switching={switchProvider.isPending}
              route={routeCtx}
            />
          ) : selected === '__new__' || customList.some(c => c.name === selected) ? (
            selected === '__new__' ? (
              <DataSourceEditor
                key={selected}
                initial={null}
                existingName={undefined}
                onCancel={() => setSelected('tickflow')}
                onSaved={() => {
                  qc.invalidateQueries({ queryKey: QK.dataSources })
                  // 数据集声明变化 → 能力增广与实时模式立即刷新
                  qc.invalidateQueries({ queryKey: QK.preferences })
                  qc.invalidateQueries({ queryKey: QK.capabilities })
                  qc.invalidateQueries({ queryKey: QK.quoteStatus })
                  setSelected('tickflow')
                }}
                activeName={activeName}
                onActivate={(name) => switchProvider.mutate(name)}
              />
            ) : (
              /* 自定义源详情: 能力卡片(路由) + 编辑器 */
              <div className="space-y-5">
                <section className="rounded-card border border-border bg-surface p-6">
                  <div className="flex items-center gap-2.5 mb-4">
                    <Database className="h-4 w-4 text-secondary" />
                    <h3 className="text-sm font-medium text-foreground">
                      {selectedCustom?.display_name || selected} · 数据集能力与路由
                    </h3>
                  </div>
                  <SourceCapabilityGrid
                    sourceName={selected}
                    sourceDisplay={selectedCustom?.display_name || selected}
                    datasets={selectedCustom?.datasets || []}
                    candidatesOf={routeCtx.candidatesOf}
                    providerOf={routeCtx.providerOf}
                    pending={routeCtx.pending}
                    onSelect={routeCtx.onSelect}
                  />
                </section>
                <DataSourceEditor
                  key={selected}
                  initial={null}
                  existingName={selected}
                  onCancel={() => setSelected('tickflow')}
                  onSaved={() => {
                    qc.invalidateQueries({ queryKey: QK.dataSources })
                    // 数据集声明变化 → 能力增广与实时模式立即刷新
                    qc.invalidateQueries({ queryKey: QK.preferences })
                    qc.invalidateQueries({ queryKey: QK.capabilities })
                    qc.invalidateQueries({ queryKey: QK.quoteStatus })
                    // 强制清除该源的详情缓存, 下次编辑重新拉取最新配置
                    qc.removeQueries({ queryKey: ['data-source-detail', selected] })
                  }}
                  activeName={activeName}
                  onActivate={(name) => switchProvider.mutate(name)}
                  onDelete={selectedCustom ? () => setConfirmDelete(selected) : undefined}
                />
              </div>
            )
          ) : pluginList.find(x => x.name === selected) ? (
            /* 选中插件: 信息 + 能力卡片(路由) + Key 配置 */
            <PluginDetail
              plugin={pluginList.find(x => x.name === selected)!}
              isActive={servingDatasets(selected).length > 0}
              onSwitch={() => switchProvider.mutate(selected)}
              switching={switchProvider.isPending}
              route={routeCtx}
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

function PluginDetail({ plugin, isActive, onSwitch, switching, route }: {
  plugin: PluginDataSourceItem
  isActive: boolean
  onSwitch: () => void
  switching: boolean
  route: RouteCtx
}) {
  return (
    <div className="space-y-5">
      <section className="rounded-card border border-border bg-surface p-6">
        <div className="flex items-start gap-4 mb-5">
          <div className="h-11 w-11 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
            <Zap className="h-5 w-5 text-accent" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="text-base font-semibold text-foreground">{plugin.display_name}</h3>
              <span className="text-[10px] text-muted/50 uppercase tracking-wider">插件 · {plugin.runtime}</span>
            </div>
            {plugin.description && <p className="text-xs text-secondary leading-relaxed">{plugin.description}</p>}
          </div>
        </div>

        {/* 本源参与的能力: 标签选择提供方, 即刻生效 */}
        <SourceCapabilityGrid
          sourceName={plugin.name}
          sourceDisplay={plugin.display_name}
          datasets={plugin.datasets}
          candidatesOf={route.candidatesOf}
          providerOf={route.providerOf}
          pending={route.pending}
          onSelect={route.onSelect}
        />

        <div className="flex items-center gap-3 mt-4">
          {isActive ? (
            <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-2 py-1 rounded">
              <Check className="h-2.5 w-2.5" /> 服务中
            </span>
          ) : plugin.available ? (
            <button
              onClick={onSwitch}
              disabled={switching}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 transition-colors disabled:opacity-50"
            >
              <Zap className="h-3.5 w-3.5" />
              整体切换为该源
            </button>
          ) : (
            <span className="text-xs text-muted">{plugin.status}</span>
          )}
        </div>
      </section>

      {/* API Key 配置 (声明了 api_key_env 的插件) */}
      {plugin.api_key_env && <PluginKeyConfig plugin={plugin} />}
    </div>
  )
}

function TickFlowDetail({ active, onSwitch, switching, route }: {
  active: boolean
  onSwitch: () => void
  switching: boolean
  route: RouteCtx
}) {
  return (
    <div className="space-y-5">
      <section className="rounded-card border border-border bg-surface p-6">
        <div className="flex items-start gap-4 mb-5">
          <div className="h-11 w-11 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
            <Database className="h-5 w-5 text-accent" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-base font-semibold text-foreground">TickFlow</h2>
              <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning">第三方</span>
              <AllTiersBadge />
              {active && (
                <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-2 py-1 rounded">
                  <Check className="h-2.5 w-2.5" /> 服务中
                </span>
              )}
            </div>
            <p className="text-xs text-secondary mt-1.5 leading-relaxed">
              默认数据源,具备全部能力(日K · 除权 · 分钟 · 实时 · 财务)。在下方每个能力卡片上点标签,可单独选择该数据集由哪个数据源提供。
            </p>
          </div>
        </div>

        {/* 全部能力的提供方标签选择 + 恢复默认 */}
        <SourceCapabilityGrid
          sourceName="tickflow"
          sourceDisplay="TickFlow"
          datasets={['daily', 'adj_factor', 'realtime', 'minute']}
          candidatesOf={route.candidatesOf}
          providerOf={route.providerOf}
          pending={route.pending}
          onSelect={route.onSelect}
          anyCustom={route.anyCustom}
          onReset={route.onReset}
        />

        {!active && (
          <button
            onClick={onSwitch}
            disabled={switching}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 transition-colors disabled:opacity-50 mt-3"
          >
            <Zap className="h-3.5 w-3.5" />
            切换为当前数据源
          </button>
        )}
      </section>

      {/* TickFlow API Key 配置 + 订阅档位 + 可用功能 (原 account tab 内容) */}
      <TickFlowKeyConfig />
    </div>
  )
}
