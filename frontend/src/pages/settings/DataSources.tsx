import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { AlertTriangle, Check, Database, Plus, RefreshCw, ShieldCheck, Zap, FileWarning } from 'lucide-react'
import { api, type DataSourceItem, type PluginDataSourceItem } from '@/lib/api'
import {
  buildProviderPreferencePatch,
  resolveInitialProviderSelection,
} from '@/lib/data-source-selection'
import { buildDataTrustRows, getDataTrustSummaryLabel, type DataTrustResponse } from '@/lib/data-trust'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'
import { toast } from '@/components/Toast'
import { DataSourceEditor } from './DataSourceEditor'

const DATASET_LABEL: Record<string, string> = {
  instruments: '证券主表',
  daily: '日K',
  adj_factor: '复权因子',
  realtime: '实时',
  minute: '分钟',
  financial: '财务',
}

export function SettingsDataSourcesPanel() {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const sources = useQuery({ queryKey: QK.dataSources, queryFn: api.dataSources })
  const trust = useQuery({ queryKey: QK.dataTrust, queryFn: api.dataTrust })
  const [selected, setSelected] = useState<string>('tickflow') // 当前在右侧编辑的源 name
  const initializedProviderSelection = useRef(false)
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
      const datasets = allItems.find(s => s.name === name)?.datasets ?? []
      return api.updateDataProviders(buildProviderPreferencePatch(name, datasets))
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataTrust })
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
  useEffect(() => {
    const next = resolveInitialProviderSelection({
      current: selected,
      active: activeName,
      preferencesLoaded: prefs.data != null,
      initialized: initializedProviderSelection.current,
    })
    if (next !== selected) setSelected(next)
    if (prefs.data != null) initializedProviderSelection.current = true
  }, [activeName, prefs.data, selected])

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
  const activeDisplayName = allItems.find(s => s.name === activeName)?.display_name || activeName

  const selectedCustom = customList.find(s => s.name === selected)

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

        {/* 当前数据源状态 */}
        <div className="flex items-center gap-2 mb-4 px-3 py-2.5 rounded-lg bg-elevated/30">
          <span className="text-[10px] uppercase tracking-widest text-muted">当前</span>
          <span className="h-2 w-2 rounded-full bg-accent animate-pulse" />
          <span className="text-sm font-medium text-foreground">
            {activeDisplayName}
          </span>
        </div>

        {/* 数据源选择 - 横向卡片列表 */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {allItems.map(item => {
            const isActive = activeName === item.name
            const isSelected = selected === item.name
            const plugin = pluginMap.get(item.name)
            const pluginUnavailable = plugin && !plugin.available
            const dependencyMissing = pluginUnavailable && plugin.status.includes('未安装')
            const installing = installMut.isPending && installMut.variables === item.name
            const uninstalling = uninstallMut.isPending && uninstallMut.variables === item.name
            return (
              <div
                key={item.name}
                onClick={() => {
                  setSelected(item.name)
                  // 只有用户自定义源 (YAML) 才进编辑器; tickflow 和插件不可编辑
                  if (customList.some(c => c.name === item.name)) {
                    editExisting.mutate(item.name)
                  }
                }}
                className={`relative text-left rounded-lg border px-3.5 py-3 transition-all ${
                  pluginUnavailable
                    ? 'border-border/40 bg-elevated/10 opacity-70'
                    : isSelected
                      ? 'border-accent/50 bg-accent/5 ring-1 ring-accent/20 cursor-pointer'
                      : 'border-border/60 bg-elevated/20 hover:bg-elevated/40 cursor-pointer'
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                    pluginUnavailable ? 'bg-muted/30' : isActive ? 'bg-accent' : 'bg-transparent border border-muted/40'
                  }`} />
                  <span className={`text-sm truncate flex-1 ${isActive ? 'font-medium text-foreground' : 'text-secondary'}`}>
                    {item.display_name}
                  </span>
                  {item.name === 'tickflow' && (
                    <span className="text-[9px] text-muted/50 uppercase tracking-wider shrink-0">内置</span>
                  )}
                  {pluginNames.has(item.name) && (
                    <span className="text-[9px] text-muted/50 uppercase tracking-wider shrink-0">插件</span>
                  )}
                  {/* 右侧操作区: 插件未安装→安装按钮; 已激活→使用中; 否则→使用/卸载 */}
                  {dependencyMissing ? (
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
                  ) : pluginUnavailable ? (
                    <span className="text-[9px] text-warning shrink-0">未就绪</span>
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
                {/* 未就绪插件显示真实原因，避免把缺少凭据误报成缺依赖。 */}
                {pluginUnavailable && (
                  <div className="ml-3.5 mt-1 text-[10px] text-warning/80 truncate" title={plugin?.status}>
                    {plugin?.status}
                  </div>
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
          <span>单击编辑</span>
          <span className="text-muted/30">·</span>
          <span>点「使用」切换为当前数据源</span>
          <span className="text-muted/30">·</span>
          <span>各数据集独立选择；切换不会暗改不支持的数据集</span>
        </div>
      </section>

      <DataTrustPanel
        data={trust.data}
        loading={trust.isLoading}
        error={trust.error instanceof Error ? trust.error.message : null}
      />

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

function DataTrustPanel({ data, loading, error }: {
  data?: DataTrustResponse
  loading: boolean
  error: string | null
}) {
  const rows = data ? buildDataTrustRows(data) : []
  const healthy = data?.overall_status === 'ok'
  const unconfigured = data?.overall_status === 'unconfigured'
  const summaryLabel = data ? getDataTrustSummaryLabel(data) : null

  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-2.5">
          {healthy ? (
            <ShieldCheck className="h-4 w-4 text-accent mt-0.5" />
          ) : (
            <AlertTriangle className="h-4 w-4 text-warning mt-0.5" />
          )}
          <div>
            <h2 className="text-sm font-medium text-foreground">数据可信度回执</h2>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">
              这里显示真实来源、覆盖率和数据截止日；拉取失败或校验不通过不会换源伪装成功。
            </p>
          </div>
        </div>
        {data && (
          <span className={`shrink-0 rounded px-2 py-1 text-[10px] ${
            healthy
              ? 'bg-accent/10 text-accent'
              : unconfigured
                ? 'bg-elevated text-muted'
                : 'bg-warning/10 text-warning'
          }`}>
            {summaryLabel}
          </span>
        )}
      </div>

      {loading ? (
        <div className="text-xs text-muted">正在读取回执...</div>
      ) : error ? (
        <div className="rounded-lg border border-danger/20 bg-danger/5 px-3 py-2 text-xs text-danger">
          回执读取失败：{error}
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-border/50 bg-elevated/20 px-3 py-3 text-xs text-muted">
          尚未执行数据同步。完成一次证券主表或日线同步后，这里才会出现可审计结果。
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left">
            <thead>
              <tr className="border-b border-border/60 text-[10px] text-muted">
                <th className="pb-2 pr-3 font-normal">数据集</th>
                <th className="pb-2 pr-3 font-normal">真实来源</th>
                <th className="pb-2 pr-3 font-normal">状态</th>
                <th className="pb-2 pr-3 font-normal">覆盖率</th>
                <th className="pb-2 pr-3 font-normal">行数</th>
                <th className="pb-2 pr-3 font-normal">数据截止日</th>
                <th className="pb-2 font-normal">问题</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={row.dataset} className="border-b border-border/30 last:border-0 text-xs">
                  <td className="py-2.5 pr-3 text-foreground">{row.datasetLabel}</td>
                  <td className="py-2.5 pr-3 font-mono text-secondary">{row.provider}</td>
                  <td className={`py-2.5 pr-3 ${
                    row.status === 'ok' ? 'text-accent' : row.status === 'partial' || row.status === 'empty' ? 'text-warning' : 'text-danger'
                  }`}>
                    {row.statusLabel}
                  </td>
                  <td className="py-2.5 pr-3 text-secondary">{row.coverageLabel}</td>
                  <td className="py-2.5 pr-3 font-mono text-secondary">{row.rowCount.toLocaleString()}</td>
                  <td className="py-2.5 pr-3 font-mono text-secondary">{row.observedEnd || '—'}</td>
                  <td className="py-2.5 text-muted">{row.issueText || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
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
        {!plugin.available ? (
          <div className="w-full rounded-lg border border-warning/20 bg-warning/5 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-xs text-warning">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {plugin.status || '数据源尚未就绪'}
            </div>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">
              {plugin.status.includes('TUSHARE_TOKEN')
                ? '请在后端 .env 中配置 TUSHARE_TOKEN，然后点击上方“重新加载”。'
                : plugin.install_hint || '请先完成依赖安装，再重新加载数据源。'}
            </p>
          </div>
        ) : isActive ? (
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
            <span className="text-[10px] text-muted/60 uppercase tracking-wider border border-border rounded px-1.5 py-0.5">内置可选</span>
            {active && (
              <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded">
                <Check className="h-2.5 w-2.5" /> 当前使用
              </span>
            )}
          </div>
          <p className="text-xs text-secondary mt-1.5 leading-relaxed">
            可选数据源。日K、复权因子、实时行情、分钟K均可由 TickFlow 提供；只有明确选择后才会使用。
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
