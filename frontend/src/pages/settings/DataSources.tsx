import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, Database, Plus, RefreshCw, Zap, FileWarning } from 'lucide-react'
import { api, type DataSourceItem, type PluginDataSourceItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'
import { toast } from '@/components/Toast'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DataSourceEditor } from './DataSourceEditor'
import {
  SettingsPanel,
  SettingsSection,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

const DATASET_LABEL: Record<string, string> = {
  daily: '日K',
  adj_factor: '除权',
  realtime: '实时',
  minute: '分钟',
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
      toast('配置已重新加载', 'success')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteDataSource(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dataSources })
      qc.invalidateQueries({ queryKey: QK.preferences })
      setSelected('tickflow')
      setConfirmDelete(null)
      toast('数据源已删除', 'success')
    },
  })

  const switchProvider = useMutation({
    mutationFn: (name: string) => {
      // tickflow: 5 个数据集全量重置为 tickflow
      if (name === 'tickflow') {
        return api.updateDataProviders({
          daily_data_provider: 'tickflow',
          adj_factor_provider: 'same_as_daily',
          realtime_data_provider: 'tickflow',
          minute_data_provider: 'tickflow',
          financial_data_provider: 'tickflow',
        })
      }
      // 非 tickflow: 按源声明的 datasets 动态切换。支持的数据集切到该源,
      // 不支持的保持 tickflow, 使 preferences 与实际取数路由一致
      // (避免 UI 显示某源、后台却走 tickflow 的假象)。
      const supported = new Set(
        allItems.find(s => s.name === name)?.datasets ?? []
      )
      const pick = (dataset: string) => (supported.has(dataset) ? name : 'tickflow')
      return api.updateDataProviders({
        daily_data_provider: pick('daily'),
        adj_factor_provider: 'same_as_daily', // 除权始终跟随日K
        realtime_data_provider: pick('realtime'),
        minute_data_provider: pick('minute'),
        financial_data_provider: pick('financial'),
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
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

  return (
    <SettingsPanel
      icon={Database}
      title="数据源"
      description="选择行情数据源并管理本地配置，未覆盖的数据集会自动回退到 TickFlow。"
      action={(
        <button
          type="button"
          onClick={() => reload.mutate()}
          disabled={reload.isPending}
          className={settingsSecondaryButtonClass}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${reload.isPending ? 'animate-spin' : ''}`} />
          重新加载
        </button>
      )}
    >
      {/* ===== 顶部: 当前数据源 + 数据源选择 (一个大卡片) ===== */}
      <SettingsSection
        title="可用数据源"
        description="选择要查看、编辑或启用的数据源。"
        unframed
      >
        {sources.data?.config_dir && (
          <div className="mb-3 hidden truncate font-mono text-[10px] text-muted/50 lg:block" title={sources.data.config_dir}>
            {sources.data.config_dir}
          </div>
        )}

        {/* 当前数据源状态 */}
        <div className="flex items-center gap-2 mb-4 px-3 py-2.5 rounded-lg bg-elevated/30">
          <span className="text-[10px] uppercase tracking-widest text-muted">当前</span>
          <span className="h-2 w-2 rounded-full bg-accent animate-pulse" />
          <span className="text-sm font-medium text-foreground">
            {activeName === 'tickflow' ? 'TickFlow' : customList.find(s => s.name === activeName)?.display_name || activeName}
          </span>
        </div>

        {/* 数据源选择 - 横向卡片列表 */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {allItems.map(item => {
            const isActive = activeName === item.name
            const isSelected = selected === item.name
            const plugin = pluginMap.get(item.name)
            const pluginUnavailable = plugin && !plugin.available
            const installing = installMut.isPending && installMut.variables === item.name
            const uninstalling = uninstallMut.isPending && uninstallMut.variables === item.name
            const selectItem = () => {
              if (pluginUnavailable) return
              setSelected(item.name)
              if (customList.some(c => c.name === item.name)) editExisting.mutate(item.name)
            }
            return (
              <div
                key={item.name}
                className={`relative text-left rounded-lg border px-3.5 py-3 transition-all ${
                  pluginUnavailable
                    ? 'border-border/40 bg-elevated/10 opacity-70'
                    : isSelected
                      ? 'border-accent/50 bg-accent/5 ring-1 ring-accent/20'
                      : 'border-border/60 bg-elevated/20 hover:bg-elevated/40'
                }`}
              >
                <div className="mb-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                  <button
                    type="button"
                    onClick={selectItem}
                    disabled={!!pluginUnavailable}
                    aria-pressed={isSelected}
                    className="flex min-w-24 flex-1 items-center gap-2 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed"
                  >
                    <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                      pluginUnavailable ? 'bg-muted/30' : isActive ? 'bg-accent' : 'bg-transparent border border-muted/40'
                    }`} />
                    <span className={`min-w-0 flex-1 truncate text-sm ${isActive ? 'font-medium text-foreground' : 'text-secondary'}`}>
                      {item.display_name}
                    </span>
                    {item.name === 'tickflow' && (
                      <span className="shrink-0 text-[9px] text-muted/50">内置</span>
                    )}
                    {pluginNames.has(item.name) && (
                      <span className="shrink-0 text-[9px] text-muted/50">插件</span>
                    )}
                  </button>
                  {/* 右侧操作区: 插件未安装→安装按钮; 已激活→使用中; 否则→使用/卸载 */}
                  {pluginUnavailable ? (
                    installing ? (
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
                  ) : isActive ? (
                    <span className="inline-flex items-center gap-0.5 text-[9px] text-accent shrink-0">
                      <Check className="h-2.5 w-2.5" /> 使用中
                    </span>
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
                      {uninstalling ? (
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
                      )}
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
                {item.name !== 'tickflow' && item.datasets.length > 0 && (
                  <div className="flex flex-wrap gap-1 ml-3.5">
                    {item.datasets.map(ds => (
                      <span key={ds} className="text-[9px] text-muted/60 bg-elevated/60 px-1 py-0.5 rounded">
                        {DATASET_LABEL[ds] || ds}
                      </span>
                    ))}
                  </div>
                )}
                {item.name === 'tickflow' && (
                  <div className="text-[10px] text-muted/60 ml-3.5">日K · 除权 · 实时 · 分钟K</div>
                )}
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

        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted/50">
          <span>单击编辑</span>
          <span className="text-muted/30">·</span>
          <span>点「使用」切换为当前数据源</span>
          <span className="text-muted/30">·</span>
          <span>未启用的数据集自动回退 TickFlow</span>
        </div>
      </SettingsSection>

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
              active={activeName === 'tickflow'}
              onSwitch={() => switchProvider.mutate('tickflow')}
              switching={switchProvider.isPending}
            />
          ) : selected === '__new__' || customList.some(c => c.name === selected) ? (
            <DataSourceEditor
              key={selected}
              initial={null}
              existingName={selected === '__new__' ? undefined : selected}
              onCancel={() => setSelected('tickflow')}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: QK.dataSources })
                // 强制清除该源的详情缓存, 下次编辑重新拉取最新配置
                if (selected !== '__new__') {
                  qc.removeQueries({ queryKey: ['data-source-detail', selected] })
                }
                if (selected === '__new__') setSelected(activeName === 'tickflow' ? 'tickflow' : activeName)
              }}
              activeName={activeName}
              onActivate={(name) => switchProvider.mutate(name)}
              onDelete={selected !== '__new__' && selectedCustom ? () => setConfirmDelete(selected) : undefined}
            />
          ) : pluginList.find(x => x.name === selected) ? (
            /* 选中插件: 显示只读详情, 不进编辑器 */
            <PluginDetail
              plugin={pluginList.find(x => x.name === selected)!}
              isActive={activeName === selected}
              onSwitch={() => switchProvider.mutate(selected)}
              switching={switchProvider.isPending}
            />
          ) : null}
        </motion.div>
      </AnimatePresence>

      <ConfirmDialog
        open={!!confirmDelete}
        title="删除数据源"
        message={`确认删除「${customList.find(s => s.name === confirmDelete)?.display_name || confirmDelete || ''}」? 该数据源的配置文件将被移除,此操作不可撤销。`}
        confirmText="确认删除"
        danger
        pending={remove.isPending}
        onCancel={() => setConfirmDelete(null)}
        onConfirm={() => { if (confirmDelete) remove.mutate(confirmDelete) }}
      />
    </SettingsPanel>
  )
}

function PluginDetail({ plugin, isActive, onSwitch, switching }: {
  plugin: PluginDataSourceItem
  isActive: boolean
  onSwitch: () => void
  switching: boolean
}) {
  return (
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
      <div className="flex items-center gap-3">
        {isActive ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-accent">
            <Check className="h-3.5 w-3.5" /> 当前使用中
          </span>
        ) : (
          <button
            onClick={onSwitch}
            disabled={switching}
            className="px-3 py-1.5 rounded-btn bg-accent/10 text-accent hover:bg-accent/20 text-xs font-medium transition-colors disabled:opacity-50"
          >
            切换为当前数据源
          </button>
        )}
      </div>
    </section>
  )
}

function TickFlowDetail({ active, onSwitch, switching }: { active: boolean; onSwitch: () => void; switching: boolean }) {
  return (
    <section className="rounded-card border border-border bg-surface p-6">
      <div className="flex items-start gap-4 mb-5">
        <div className="h-11 w-11 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
          <Database className="h-5 w-5 text-accent" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-base font-semibold text-foreground">TickFlow</h2>
            <span className="text-[10px] text-muted/60 uppercase tracking-wider border border-border rounded px-1.5 py-0.5">内置默认</span>
            {active && (
              <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded">
                <Check className="h-2.5 w-2.5" /> 当前使用
              </span>
            )}
          </div>
          <p className="text-xs text-secondary mt-1.5 leading-relaxed">
            项目默认数据源。日K、除权因子、实时行情、分钟K均由 TickFlow 提供,无需额外配置。
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 mb-5">
        {[
          { label: '日K', desc: '历史 + 实时覆写' },
          { label: '除权因子', desc: 'Starter+ 能力' },
          { label: '实时行情', desc: '全市场快照' },
          { label: '分钟K', desc: 'Pro+ 能力' },
        ].map(f => (
          <div key={f.label} className="rounded-lg border border-border/50 bg-elevated/20 px-3 py-2.5">
            <div className="text-xs font-medium text-foreground">{f.label}</div>
            <div className="text-[10px] text-muted mt-0.5">{f.desc}</div>
          </div>
        ))}
      </div>

      {!active && (
        <button
          onClick={onSwitch}
          disabled={switching}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-btn bg-accent text-white text-sm font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors"
        >
          <Zap className="h-3.5 w-3.5" />
          切换为当前数据源
        </button>
      )}
    </section>
  )
}
