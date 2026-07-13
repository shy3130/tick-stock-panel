import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, Check, ExternalLink, Pencil, Plus, Save, Trash2, X } from 'lucide-react'
import { api, type AnalysisColumn, type AnalysisMenu, type ExtDataConfig, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Skeleton } from '@/components/data/Skeleton'
import {
  SettingsPanel,
  SettingsSection,
  settingsControlClass,
  settingsIconButtonClass,
  settingsPrimaryButtonClass,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

function dtypeToColumnType(dtype: string): AnalysisColumn['type'] {
  return dtype === 'int' || dtype === 'float' ? 'number' : 'string'
}

function buildColumn(field: ExtDataField): AnalysisColumn {
  return {
    field: field.name,
    label: field.label || field.name,
    type: dtypeToColumnType(field.dtype),
    precision: field.dtype === 'float' ? 2 : null,
    sortable: field.dtype === 'int' || field.dtype === 'float',
    visible: true,
  }
}

function firstMatchingField(config: ExtDataConfig | undefined, keywords: string[]) {
  if (!config) return ''
  for (const keyword of keywords) {
    const lower = keyword.toLowerCase()
    const matched = config.fields.find(f => f.name.toLowerCase().includes(lower) || f.label.toLowerCase().includes(lower))
    if (matched) return matched.name
  }
  return config.fields.find(f => !['symbol', 'code'].includes(f.name) && f.dtype === 'string')?.name ?? ''
}

export function SettingsExtPagesPanel() {
  const qc = useQueryClient()
  const menus = useQuery({ queryKey: QK.analysisMenus, queryFn: api.analysisMenus })
  const extData = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const configs = extData.data?.items ?? []
  const menuItems = menus.data?.items ?? []

  const [showForm, setShowForm] = useState(false)
  const [editingMenu, setEditingMenu] = useState<AnalysisMenu | null>(null)
  const [id, setId] = useState('')
  const [label, setLabel] = useState('')
  const [dataSource, setDataSource] = useState('')
  const [template, setTemplate] = useState<'dimension_rank' | 'ranking' | 'table'>('dimension_rank')
  const [dimensionField, setDimensionField] = useState('')
  const [rankField, setRankField] = useState('')
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])
  const [error, setError] = useState('')
  const [confirmDeleteMenu, setConfirmDeleteMenu] = useState<AnalysisMenu | null>(null)

  const activeConfig = configs.find(c => c.id === dataSource) ?? configs[0]
  const fields = activeConfig?.fields ?? []
  const numericFields = useMemo(() => fields.filter(f => f.dtype === 'int' || f.dtype === 'float'), [fields])

  const resetForm = () => {
    const cfg = configs[0]
    setEditingMenu(null)
    setId('')
    setLabel('')
    setDataSource(cfg?.id ?? '')
    setTemplate('dimension_rank')
    setDimensionField(firstMatchingField(cfg, ['概念', 'industry', '行业', 'sector']))
    setRankField('')
    setSelectedColumns(cfg?.fields.filter(f => !['symbol', 'code'].includes(f.name)).slice(0, 6).map(f => f.name) ?? [])
    setError('')
  }

  const editMenu = (menu: AnalysisMenu) => {
    const cfg = configs.find(c => c.id === menu.data_source)
    setEditingMenu(menu)
    setId(menu.id)
    setLabel(menu.label)
    setDataSource(menu.data_source)
    setTemplate(menu.template)
    setDimensionField(menu.dimension_field ?? firstMatchingField(cfg, ['概念', 'industry', '行业', 'sector']))
    setRankField(menu.rank_field ?? '')
    setSelectedColumns(menu.detail_columns.map(c => c.field))
    setError('')
    setShowForm(true)
  }

  const save = useMutation({
    mutationFn: () => {
      const cfg = activeConfig
      if (!cfg) throw new Error('请选择扩展数据源')
      if (!id.trim()) throw new Error('请输入菜单标识')
      if (!label.trim()) throw new Error('请输入菜单名称')
      if (template === 'dimension_rank' && !dimensionField) throw new Error('请选择分组字段')
      if (template === 'ranking' && !rankField) throw new Error('请选择排名字段')

      const detailColumns = selectedColumns
        .map(name => cfg.fields.find(f => f.name === name))
        .filter(Boolean)
        .map(f => buildColumn(f as ExtDataField))
      const groupColumns: AnalysisColumn[] = template === 'dimension_rank'
        ? [
            { field: '__dimension', label: cfg.fields.find(f => f.name === dimensionField)?.label || '分组', type: 'string', visible: true },
            { field: '__count', label: '股票数', type: 'number', sortable: true, visible: true },
            ...detailColumns.filter(c => c.type === 'number').slice(0, 2).map(c => ({ ...c, label: `平均${c.label || c.field}`, aggregate: 'avg' as const })),
          ]
        : []

      return api.analysisMenuSave(id.trim(), {
        label: label.trim(),
        icon: template === 'dimension_rank' ? 'tags' : 'chart',
        data_source: cfg.id,
        template,
        dimension_field: template === 'dimension_rank' ? dimensionField : null,
        rank_field: template === 'ranking' ? rankField : null,
        group_columns: groupColumns,
        detail_columns: detailColumns,
        default_sort: template === 'ranking' && rankField ? { field: rankField, order: 'desc' } : null,
        visible: editingMenu?.visible ?? true,
        order: editingMenu?.order ?? menuItems.length + 100,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.analysisMenus })
      setShowForm(false)
      resetForm()
    },
    onError: (err) => setError(String((err as any)?.message ?? err)),
  })

  const del = useMutation({
    mutationFn: api.analysisMenuDelete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.analysisMenus })
      setConfirmDeleteMenu(null)
    },
  })

  return (
    <SettingsPanel
      icon={BarChart3}
      title="扩展页面"
      description="将扩展数据源配置成工作台分析菜单，并设置页面模板、分组字段和列表列。"
      width="wide"
      action={(
        <button
          type="button"
          onClick={() => { resetForm(); setShowForm(true) }}
          className={settingsPrimaryButtonClass}
        >
          <Plus className="h-3.5 w-3.5" />
          新建页面
        </button>
      )}
    >

      {showForm && (
        <SettingsSection
          title={editingMenu ? '编辑扩展页面' : '新建扩展页面'}
          description="菜单标识保存后不可在此处直接修改，如需更换标识请新建页面。"
          contentClassName="space-y-4"
          action={(
            <button
              type="button"
              onClick={() => { setShowForm(false); setError('') }}
              className={settingsIconButtonClass}
              aria-label="关闭编辑表单"
              title="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        >

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">菜单标识</span>
              <input
                value={id}
                disabled={!!editingMenu}
                onChange={e => setId(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
                placeholder="如 concept_hot"
                className={settingsControlClass}
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">菜单名称</span>
              <input value={label} onChange={e => setLabel(e.target.value)} placeholder="如 概念热度" className={settingsControlClass} />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">扩展数据源</span>
              <select
                value={dataSource || activeConfig?.id || ''}
                onChange={e => {
                  const cfg = configs.find(c => c.id === e.target.value)
                  setDataSource(e.target.value)
                  setDimensionField(firstMatchingField(cfg, ['概念', 'industry', '行业', 'sector']))
                  setRankField('')
                  setSelectedColumns(cfg?.fields.filter(f => !['symbol', 'code'].includes(f.name)).slice(0, 6).map(f => f.name) ?? [])
                }}
                className={settingsControlClass}
              >
                {configs.map(cfg => <option key={cfg.id} value={cfg.id}>{cfg.label}</option>)}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">模板</span>
              <select value={template} onChange={e => setTemplate(e.target.value as any)} className={settingsControlClass}>
                <option value="dimension_rank">维度热度榜</option>
                <option value="ranking">指标排名榜</option>
                <option value="table">明细表</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">分组字段</span>
              <select value={dimensionField} onChange={e => setDimensionField(e.target.value)} disabled={template !== 'dimension_rank'} className={settingsControlClass}>
                <option value="">请选择</option>
                {fields.map(f => <option key={f.name} value={f.name}>{f.label || f.name}</option>)}
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">排名字段</span>
              <select value={rankField} onChange={e => setRankField(e.target.value)} disabled={template !== 'ranking'} className={settingsControlClass}>
                <option value="">请选择</option>
                {numericFields.map(f => <option key={f.name} value={f.name}>{f.label || f.name}</option>)}
              </select>
            </label>
          </div>

          <div>
            <div className="text-[11px] text-muted mb-2">列表列配置</div>
            <div className="flex flex-wrap gap-2">
              {fields.filter(f => !['symbol', 'code'].includes(f.name)).map(f => {
                const active = selectedColumns.includes(f.name)
                return (
                  <button
                    key={f.name}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setSelectedColumns(cols => active ? cols.filter(c => c !== f.name) : [...cols, f.name])}
                    className={`inline-flex min-h-8 items-center gap-1.5 rounded-btn border px-2.5 py-1 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 ${active ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-elevated/40 text-secondary hover:bg-elevated'}`}
                  >
                    {active && <Check className="h-3 w-3" />}
                    {f.label || f.name}
                  </button>
                )
              })}
            </div>
          </div>

          {error && <div role="alert" className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}

          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" onClick={() => { setShowForm(false); setError('') }} className={settingsSecondaryButtonClass}>取消</button>
            <button type="button" onClick={() => save.mutate()} disabled={save.isPending} className={settingsPrimaryButtonClass}>
              <Save className="h-3.5 w-3.5" />保存
            </button>
          </div>
        </SettingsSection>
      )}

      <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {menuItems.map(menu => (
          <div key={menu.id} className="rounded-card border border-border bg-surface p-4">
            <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <h3 className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{menu.label}</h3>
                  {menu.builtin && <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">默认</span>}
                  {!menu.visible && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">已隐藏</span>}
                </div>
                <p className="mt-1 break-all font-mono text-[11px] text-muted">{menu.id}</p>
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1">
                <button type="button" onClick={() => editMenu(menu)} className={settingsIconButtonClass} title="编辑" aria-label={`编辑${menu.label}`}>
                  <Pencil className="h-3.5 w-3.5" />
                </button>
                {!menu.builtin && (
                  <button type="button" onClick={() => setConfirmDeleteMenu(menu)} disabled={del.isPending} className={`${settingsIconButtonClass} hover:text-danger`} title="删除" aria-label={`删除${menu.label}`}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>
            <div className="mt-3 space-y-1 text-[11px] text-secondary">
              <div className="break-all">数据源：<span className="font-mono text-muted">{menu.data_source}</span></div>
              <div>模板：{menu.template}</div>
              {menu.dimension_field && <div className="break-all">分组字段：{menu.dimension_field}</div>}
              <div>列表列：{menu.detail_columns.length} 个</div>
            </div>
            <Link to={`/analysis/${menu.id}`} className="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-foreground hover:bg-border/30 transition-colors">
              <ExternalLink className="h-3.5 w-3.5" />
              打开分析页
            </Link>
          </div>
        ))}
        {menus.isLoading &&
          Array.from({ length: 3 }).map((_, i) => (
            <div key={`sk-${i}`} className="rounded-card border border-border bg-surface p-4 space-y-3">
              <Skeleton w="w-1/2" h="h-4" />
              <Skeleton w="w-1/3" h="h-3" />
              <Skeleton h="h-8" rounded="rounded-btn" />
            </div>
          ))}
        {!menus.isLoading && menuItems.length === 0 && (
          <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted md:col-span-2 xl:col-span-3">暂无扩展页面，点击右上角新建。</div>
        )}
      </section>

      <ConfirmDialog
        open={!!confirmDeleteMenu}
        title={`确认删除 ${confirmDeleteMenu?.label ?? '扩展页面'}?`}
        message="该扩展页面会从左侧分析菜单中移除，已保存的菜单配置不可恢复。"
        confirmText="确认删除"
        danger
        pending={del.isPending}
        onCancel={() => setConfirmDeleteMenu(null)}
        onConfirm={() => { if (confirmDeleteMenu) del.mutate(confirmDeleteMenu.id) }}
      />
    </SettingsPanel>
  )
}
