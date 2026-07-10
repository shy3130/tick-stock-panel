import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, ChevronDown, ChevronUp, Plus, Save, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { Skeleton } from '@/components/data/Skeleton'
import { api, type AnalysisColumn, type ExtDataConfig, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

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

export function Analysis() {
  const qc = useQueryClient()
  const menus = useQuery({ queryKey: QK.analysisMenus, queryFn: api.analysisMenus })
  const extData = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const configs = extData.data?.items ?? []

  const [showCreate, setShowCreate] = useState(false)
  const [id, setId] = useState('')
  const [label, setLabel] = useState('')
  const [dataSource, setDataSource] = useState('')
  const [template, setTemplate] = useState<'dimension_rank' | 'ranking' | 'table'>('dimension_rank')
  const [dimensionField, setDimensionField] = useState('')
  const [rankField, setRankField] = useState('')
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])
  const [error, setError] = useState('')
  const menuItems = menus.data?.items ?? []

  const activeConfig = configs.find(c => c.id === dataSource) ?? configs[0]
  const fields = activeConfig?.fields ?? []

  const resetForm = () => {
    const cfg = configs[0]
    setId('')
    setLabel('')
    setDataSource(cfg?.id ?? '')
    setTemplate('dimension_rank')
    setDimensionField(firstMatchingField(cfg, ['概念', 'industry', '行业', 'sector']))
    setRankField('')
    setSelectedColumns(cfg?.fields.filter(f => !['symbol', 'code'].includes(f.name)).slice(0, 6).map(f => f.name) ?? [])
    setError('')
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
        visible: true,
        order: menuItems.length + 100,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.analysisMenus })
      setShowCreate(false)
      resetForm()
    },
    onError: (err) => setError(String((err as any)?.message ?? err)),
  })

  const del = useMutation({
    mutationFn: api.analysisMenuDelete,
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.analysisMenus }),
  })

  const reorder = useMutation({
    mutationFn: api.analysisMenuReorder,
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.analysisMenus }),
  })

  const moveMenu = (idx: number, dir: -1 | 1) => {
    const next = [...menuItems]
    const target = idx + dir
    if (target < 0 || target >= next.length) return
    const [item] = next.splice(idx, 1)
    next.splice(target, 0, item)
    reorder.mutate(next.map(m => m.id))
  }

  const numericFields = useMemo(() => fields.filter(f => f.dtype === 'int' || f.dtype === 'float'), [fields])

  return (
    <>
      <PageHeader
        title="扩展分析"
        subtitle="自定义分析菜单 · 动态字段 · 动态列"
        right={
          <button
            onClick={() => { resetForm(); setShowCreate(v => !v) }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            新建菜单
          </button>
        }
      />

      <div className="px-8 py-6 max-w-6xl space-y-6">
        <section className="rounded-2xl border border-border bg-surface p-6 bg-[radial-gradient(circle_at_top_right,rgba(139,92,246,0.14),transparent_38%)]">
          <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-[11px] text-secondary">
            <BarChart3 className="h-3.5 w-3.5" />
            扩展数据 → 分析菜单 → 动态页面
          </div>
          <h2 className="mt-4 text-2xl font-semibold tracking-tight text-foreground">把任意扩展字段配置成一个菜单</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary">
            菜单配置决定使用哪个扩展数据源、哪个字段分组、列表展示哪些列。侧边栏会自动显示可见菜单，列表列严格按配置渲染。
          </p>
        </section>

        {showCreate && (
          <section className="rounded-card border border-border bg-surface p-5 space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">菜单标识</span>
                <input value={id} onChange={e => setId(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))} placeholder="如 concept_hot" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">菜单名称</span>
                <input value={label} onChange={e => setLabel(e.target.value)} placeholder="如 概念热度" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">扩展数据源</span>
                <select
                  value={dataSource || activeConfig?.id || ''}
                  onChange={e => {
                    const cfg = configs.find(c => c.id === e.target.value)
                    setDataSource(e.target.value)
                    setDimensionField(firstMatchingField(cfg, ['概念', 'industry', '行业', 'sector']))
                    setSelectedColumns(cfg?.fields.filter(f => !['symbol', 'code'].includes(f.name)).slice(0, 6).map(f => f.name) ?? [])
                  }}
                  className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground"
                >
                  {configs.map(cfg => <option key={cfg.id} value={cfg.id}>{cfg.label}</option>)}
                </select>
              </label>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">模板</span>
                <select value={template} onChange={e => setTemplate(e.target.value as any)} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
                  <option value="dimension_rank">维度热度榜</option>
                  <option value="ranking">指标排名榜</option>
                  <option value="table">明细表</option>
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">分组字段</span>
                <select value={dimensionField} onChange={e => setDimensionField(e.target.value)} disabled={template !== 'dimension_rank'} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground disabled:opacity-50">
                  <option value="">请选择</option>
                  {fields.map(f => <option key={f.name} value={f.name}>{f.label || f.name}</option>)}
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">排名字段</span>
                <select value={rankField} onChange={e => setRankField(e.target.value)} disabled={template !== 'ranking'} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground disabled:opacity-50">
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
                      onClick={() => setSelectedColumns(cols => active ? cols.filter(c => c !== f.name) : [...cols, f.name])}
                      className={`rounded-full border px-3 py-1 text-[11px] transition-colors ${active ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-elevated/40 text-secondary hover:bg-elevated'}`}
                    >
                      {f.label || f.name}
                    </button>
                  )
                })}
              </div>
            </div>

            {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}

            <div className="flex justify-end gap-2">
              <button onClick={() => setShowCreate(false)} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs">取消</button>
              <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />保存
              </button>
            </div>
          </section>
        )}

        <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {menuItems.map((menu, idx) => (
            <div key={menu.id} className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-medium text-foreground">{menu.label}</h3>
                  <p className="mt-1 text-[11px] text-muted font-mono">{menu.id}</p>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => moveMenu(idx, -1)}
                    disabled={idx === 0 || reorder.isPending}
                    className="p-1 rounded text-muted hover:text-accent hover:bg-accent/10 disabled:opacity-30"
                    title="上移"
                  >
                    <ChevronUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => moveMenu(idx, 1)}
                    disabled={idx === menuItems.length - 1 || reorder.isPending}
                    className="p-1 rounded text-muted hover:text-accent hover:bg-accent/10 disabled:opacity-30"
                    title="下移"
                  >
                    <ChevronDown className="h-3.5 w-3.5" />
                  </button>
                  {menu.builtin ? (
                    <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">默认</span>
                  ) : (
                    <button onClick={() => del.mutate(menu.id)} disabled={del.isPending} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-3 space-y-1 text-[11px] text-secondary">
                <div>数据源：<span className="font-mono text-muted">{menu.data_source}</span></div>
                <div>模板：{menu.template}</div>
                {menu.dimension_field && <div>分组字段：{menu.dimension_field}</div>}
                <div>列表列：{menu.detail_columns.length} 个</div>
              </div>
              <Link to={`/analysis/${menu.id}`} className="mt-4 inline-flex w-full items-center justify-center rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-foreground hover:bg-border/30 transition-colors">
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
            <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted md:col-span-2 xl:col-span-3">暂无分析菜单，点击右上角新建。</div>
          )}
        </section>
      </div>
    </>
  )
}
