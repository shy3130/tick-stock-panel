import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ListFilter, X } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { resolveEntryProfile } from '@/lib/aiProfile'
import {
  api,
  type AiExecutionMeta,
  type ScreenerCondition,
  type ScreenerFieldSpec,
  type ScreenerOrderBy,
  type ScreenerPreset,
  type ScreenerQueryResponse,
  type ScreenerNlUnrecognized,
} from '@/lib/api'
import { areConditionsValid, ConditionBuilder } from '@/components/screener/ConditionBuilder'

const BASE_COLUMNS = [
  { field: 'symbol', label: '代码' },
  { field: 'name', label: '名称' },
  { field: 'date', label: '日期' },
  { field: 'close', label: '收盘' },
  { field: 'change_pct', label: '涨跌幅' },
] as const

function safeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return (message.replace(/<[^>]*>/g, '').trim() || '请求失败').slice(0, 300)
}

function formatCell(value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'number') return Number.isFinite(value) ? value.toFixed(4).replace(/\.?0+$/, '') : '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.join(', ')
  return String(value)
}

function conditionKey(condition: ScreenerCondition): string {
  return `${condition.field}:${condition.op}:${JSON.stringify(condition.value)}`
}

export function ConditionScreener() {
  const [fields, setFields] = useState<ScreenerFieldSpec[]>([])
  const [presets, setPresets] = useState<ScreenerPreset[]>([])
  const [metadataLoading, setMetadataLoading] = useState(true)
  const [metadataError, setMetadataError] = useState<string | null>(null)
  const [conditions, setConditions] = useState<ScreenerCondition[]>([])
  const [unresolved, setUnresolved] = useState<ScreenerNlUnrecognized[]>([])
  const [nlText, setNlText] = useState('')
  const [asOf, setAsOf] = useState('')
  const [orderBy, setOrderBy] = useState<ScreenerOrderBy | undefined>()
  const [limit, setLimit] = useState(100)
  const [nlLoading, setNlLoading] = useState(false)
  const [queryLoading, setQueryLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ScreenerQueryResponse | null>(null)
  // P3: nl_screener 入口 profile 选择 + 执行元信息(ai_meta 全部 optional,旧响应兼容)
  const [profileId, setProfileId] = useState<string>()
  const [nlMeta, setNlMeta] = useState<AiExecutionMeta | null>(null)
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })

  useEffect(() => {
    let active = true
    setMetadataLoading(true)
    Promise.all([api.screenerFields(), api.screenerNlPresets()])
      .then(([fieldResponse, presetResponse]) => {
        if (!active) return
        setFields(fieldResponse.fields ?? [])
        setPresets(presetResponse.presets ?? [])
      })
      .catch((reason: unknown) => {
        if (active) setMetadataError(safeError(reason))
      })
      .finally(() => {
        if (active) setMetadataLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    setResult(null)
  }, [conditions, asOf, orderBy, limit])

  const fieldsByName = useMemo(() => new Map(fields.map(field => [field.field, field])), [fields])
  const sortableFields = useMemo(
    () => fields.filter(field => field.sortable && field.availability === 'available'),
    [fields],
  )
  const builderValid = areConditionsValid(conditions, fields)
  const canQuery = !metadataLoading && !metadataError && builderValid && unresolved.length === 0

  const resultColumns = useMemo(() => {
    const keys: string[] = BASE_COLUMNS.map(column => column.field)
    for (const condition of conditions) keys.push(condition.field)
    if (orderBy) keys.push(orderBy.field)
    const seen = new Set<string>()
    return keys.filter(field => {
      if (seen.has(field)) return false
      seen.add(field)
      return true
    }).map(field => ({
      field,
      label: BASE_COLUMNS.find(column => column.field === field)?.label
        ?? fieldsByName.get(field)?.label
        ?? field,
    }))
  }, [conditions, fieldsByName, orderBy])

  const parseNaturalLanguage = async () => {
    const text = nlText.trim()
    if (!text || nlLoading) return
    setNlLoading(true)
    setError(null)
    setNlMeta(null)
    try {
      const resolvedProfileId =
        resolveEntryProfile('nl_screener', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '') || profileId
      const parsed = await api.screenerNlParse(text, resolvedProfileId || undefined)
      setNlMeta(parsed.ai_meta ?? null)
      const recognized = (parsed.recognized ?? []).filter(condition => condition.field && condition.op)
      setConditions(previous => {
        const existing = new Set(previous.map(conditionKey))
        return [...previous, ...recognized.filter(condition => !existing.has(conditionKey(condition)))].slice(0, 20)
      })
      setUnresolved(previous => [...previous, ...(parsed.unrecognized ?? [])])
      setNlText('')
    } catch (reason: unknown) {
      setError(safeError(reason))
    } finally {
      setNlLoading(false)
    }
  }

  const applyPreset = (preset: ScreenerPreset) => {
    if (preset.executable_level === 'unsupported') return
    setConditions((preset.predicate.conditions ?? []).slice(0, 20))
    setOrderBy(preset.predicate.order_by ?? undefined)
    setUnresolved([])
    setResult(null)
    setError(null)
  }

  const runQuery = async () => {
    if (!canQuery || queryLoading) return
    setQueryLoading(true)
    setError(null)
    const payload = {
      conditions,
      limit: Math.min(500, Math.max(1, limit)),
      ...(asOf ? { as_of: asOf } : {}),
      ...(orderBy ? { order_by: orderBy } : {}),
    }
    try {
      setResult(await api.screenerConditionQuery(payload))
    } catch (reason: unknown) {
      setError(safeError(reason))
      setResult(null)
    } finally {
      setQueryLoading(false)
    }
  }

  return (
    <div className="workspace-page">
      <PageHeader title="条件选股" subtitle="结构化条件 · 本地数据" />

      <div className="workspace-content space-y-3">
        <section className="panel" aria-labelledby="condition-nl-heading">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Natural Language</div>
              <h2 id="condition-nl-heading" className="section-title flex items-center gap-2">
                <ListFilter className="h-3.5 w-3.5 text-accent" />
                自然语言辅助填充
              </h2>
            </div>
            <div className="flex items-center gap-2">
              <AiProviderSelector entry="nl_screener" value={profileId} onChange={setProfileId} compact />
              <span className="text-[11px] text-muted">仅解析填充，不会自动执行</span>
            </div>
          </div>
          <div className="panel-body space-y-3">
            <div className="flex flex-col gap-2 sm:flex-row">
              <label className="sr-only" htmlFor="condition-nl-input">自然语言条件</label>
              <textarea
                id="condition-nl-input"
                value={nlText}
                maxLength={500}
                onChange={event => setNlText(event.target.value)}
                placeholder="例如：换手率大于 3%，量比大于 2，排除 ST"
                rows={2}
                className="control min-h-16 flex-1 resize-y !h-auto px-3 py-2"
              />
              <button
                type="button"
                onClick={parseNaturalLanguage}
                disabled={!nlText.trim() || nlLoading || metadataLoading}
                className="btn-primary h-8 self-end sm:self-center"
              >
                {nlLoading && <Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />}
                解析填充
              </button>
            </div>
            <div className="flex items-center justify-between gap-2 text-right text-[11px] text-muted">
              <AiExecutionMetaBadge meta={nlMeta} />
              <span className="ml-auto num">{nlText.length}/500</span>
            </div>
            {unresolved.length > 0 && (
              <div className="space-y-1 rounded-input border border-warning/30 bg-warning/5 p-2 text-xs" aria-live="polite">
                <div className="font-medium text-warning">有未识别条件，确认或移除后才能执行：</div>
                {unresolved.map((item, index) => (
                  <div key={`${item.raw}-${index}`} className="flex items-start gap-2 text-secondary">
                    <span className="min-w-0 flex-1">“{item.raw}” — {item.reason}</span>
                    <button
                      type="button"
                      onClick={() => setUnresolved(previous => previous.filter((_, itemIndex) => itemIndex !== index))}
                      aria-label={`移除未识别条件 ${item.raw}`}
                      className="btn-ghost shrink-0 !p-0.5"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="panel" aria-labelledby="condition-presets-heading">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Presets</div>
              <h2 id="condition-presets-heading" className="section-title">常用条件</h2>
            </div>
            <span className="text-[11px] text-muted">点击只填入条件</span>
          </div>
          <div className="panel-body">
            <div className="flex flex-wrap gap-2">
              {presets.map(preset => {
                const unsupported = preset.executable_level === 'unsupported'
                return (
                  <button
                    key={preset.id}
                    type="button"
                    disabled={unsupported}
                    onClick={() => applyPreset(preset)}
                    title={preset.description}
                    className="btn-secondary disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    {preset.name}
                    {preset.executable_level === 'needs_fundamental' && (
                      <span className="ml-1.5 rounded-full bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">基本面</span>
                    )}
                    {unsupported && <span className="ml-1.5 text-[10px] text-muted">（暂不支持）</span>}
                  </button>
                )
              })}
              {!metadataLoading && presets.length === 0 && <span className="text-xs text-muted">暂无可用条件</span>}
            </div>
          </div>
        </section>

        <section className="panel" aria-labelledby="condition-builder-heading">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Conditions</div>
              <h2 id="condition-builder-heading" className="section-title">筛选条件</h2>
            </div>
            <span className="text-[11px] text-muted num">{conditions.length}/20 条</span>
          </div>
          <div className="panel-body space-y-3">
            {metadataLoading ? (
              <div className="flex items-center gap-2 text-xs text-muted" aria-live="polite"><Loader2 className="h-3.5 w-3.5 animate-spin" />加载字段定义…</div>
            ) : metadataError ? (
              <div className="text-xs text-danger" role="alert">{metadataError}</div>
            ) : (
              <ConditionBuilder fields={fields} value={conditions} onChange={setConditions} />
            )}
            {fields.length > 0 && !builderValid && conditions.length > 0 && (
              <div className="text-xs text-warning" role="status">请补全每条条件的字段、运算符和值。</div>
            )}
          </div>
        </section>

        <section className="workspace-toolbar !mb-0 flex-wrap items-end gap-3" aria-label="执行选项">
          <label className="flex flex-col gap-1 text-xs text-muted">
            截止日期（可选）
            <input type="date" value={asOf} onChange={event => setAsOf(event.target.value)} className="control w-auto" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            排序字段
            <select
              value={orderBy?.field ?? ''}
              onChange={event => {
                const field = event.target.value
                setOrderBy(field ? { field, direction: orderBy?.direction ?? 'desc' } : undefined)
              }}
              className="control min-w-36 w-auto"
            >
              <option value="">默认顺序</option>
              {sortableFields.map(field => <option key={field.field} value={field.field}>{field.label}</option>)}
            </select>
          </label>
          {orderBy && (
            <label className="flex flex-col gap-1 text-xs text-muted">
              方向
              <select value={orderBy.direction} onChange={event => setOrderBy({ ...orderBy, direction: event.target.value as 'asc' | 'desc' })} className="control w-auto">
                <option value="desc">降序</option>
                <option value="asc">升序</option>
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 text-xs text-muted">
            返回条数
            <input type="number" min={1} max={500} value={limit} onChange={event => setLimit(Math.min(500, Math.max(1, Number(event.target.value) || 1)))} className="control w-24 num" />
          </label>
          <button
            type="button"
            onClick={runQuery}
            disabled={!canQuery || queryLoading}
            className="btn-primary"
          >
            {queryLoading && <Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />}
            {queryLoading ? '选股中…' : '执行选股'}
          </button>
          <div className="basis-full text-[11px] text-muted" aria-live="polite">
            {queryLoading ? '正在执行选股…' : unresolved.length > 0 ? '请先移除未识别条件。' : conditions.length === 0 ? '至少添加一条条件。' : !builderValid ? '请补全条件值。' : '条件已就绪。'}
          </div>
        </section>

        {error && <div className="rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger" role="alert" aria-live="assertive">{error}</div>}

        {result && (
          <section className="panel" aria-labelledby="condition-results-heading">
            <div className="panel-header">
              <div>
                <div className="section-kicker">Results</div>
                <h2 id="condition-results-heading" className="section-title">选股结果</h2>
              </div>
              <div className="text-[11px] text-muted num" aria-live="polite">
                共 {result.total} 条 · {result.rows.length} 条已返回 · {result.as_of ?? '最新'} · {result.elapsed_ms}ms · 已应用 {result.applied.length} 条
              </div>
            </div>
            <div className="panel-body !p-0">
              {result.rows.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted" aria-live="polite">没有符合条件的标的。</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="data-table min-w-full">
                    <caption className="sr-only">条件选股结果</caption>
                    <thead>
                      <tr>
                        {resultColumns.map(column => <th key={column.field} scope="col">{column.label}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, index) => (
                        <tr key={`${String(row.symbol ?? index)}-${String(row.date ?? index)}`}>
                          {resultColumns.map(column => <td key={column.field} className="text-secondary">{formatCell(row[column.field])}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
