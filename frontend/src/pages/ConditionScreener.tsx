import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, BookmarkPlus, ChevronDown, Download, FlaskConical, ListFilter, Loader2, Play, RadioTower, Rows3, Save, SlidersHorizontal, Star, Trash2, Wand2, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { resolveEntryProfile } from '@/lib/aiProfile'
import { useDataStatus } from '@/lib/useSharedQueries'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import {
  api,
  genRuleId,
  type AiExecutionMeta,
  type MonitorRule,
  type ScreenerCondition,
  type ScreenerFieldSpec,
  type ScreenerGroupLogic,
  type ScreenerOrderBy,
  type ScreenerPreset,
  type ScreenerQueryResponse,
  type ScreenerNlUnrecognized,
  type ScreenerScreenRecord,
} from '@/lib/api'
import { areConditionsValid, ConditionBuilder } from '@/components/screener/ConditionBuilder'
import { AdvancedFilterPanel } from '@/components/screener/AdvancedFilterPanel'
import {
  buildScreenerQueryRequest,
  downloadResultCsv,
  effectiveGroupLogic,
  facetWarningText,
  industryFacetDisplay,
  serializeScreenerConditions,
  splitConditionsForAsOf,
  toResultCsv,
  uniqueConditionGroups,
} from '@/lib/screenerResult'
import {
  stageScreenerBacktestHandoff,
  type ScreenerBacktestTarget,
} from '@/lib/screenerBacktestHandoff'

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


function formatElapsed(ms: number): string {
  if (!Number.isFinite(ms)) return '未知耗时'
  return `${ms < 10 ? ms.toFixed(1) : Math.round(ms)} ms`
}

function conditionKey(condition: ScreenerCondition): string {
  return `${condition.field}:${condition.op}:${JSON.stringify(condition.value)}`
}

/** S3 方案桥: 回测/监控引用已保存方案时使用的策略 id（screen:<id>，与后端 screen_bridge 约定一致） */
const screenStrategyId = (id: string) => `screen:${id}`

export function ConditionScreener() {
  const navigate = useNavigate()
  const [fields, setFields] = useState<ScreenerFieldSpec[]>([])
  const [presets, setPresets] = useState<ScreenerPreset[]>([])
  const [metadataLoading, setMetadataLoading] = useState(true)
  const [metadataError, setMetadataError] = useState<string | null>(null)
  const [conditions, setConditions] = useState<ScreenerCondition[]>([])
  const [groupLogic, setGroupLogic] = useState<ScreenerGroupLogic>('and')
  const [unresolved, setUnresolved] = useState<ScreenerNlUnrecognized[]>([])
  const [nlText, setNlText] = useState('')
  const [editMode, setEditMode] = useState<'advanced' | 'list'>('advanced')
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
  const fieldsByName = useMemo(() => new Map(fields.map(field => [field.field, field])), [fields])

  // ── F7: 行预览 / 批量自选 ──
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState('')
  const closePreview = useCallback(() => { setPreviewSymbol(null); setPreviewName('') }, [])
  const [batchMsg, setBatchMsg] = useState('')
  const batchAdd = useWatchlistBatchAdd()

  // ── F8: 日期范围 + 仅最新日字段 ──
  const dataStatus = useDataStatus({ staleTime: 0 })
  const latestDate = dataStatus.data?.enriched?.latest_date ?? ''
  const minDate = dataStatus.data?.enriched?.earliest_date ?? ''
  const maxDate = latestDate
  // asOf 已设且 ≠ 最新日 → 历史日期模式，仅最新日字段不可用
  const historicalAsOf = asOf !== '' && latestDate !== '' && asOf !== latestDate
  const isFieldDisabled = useCallback(
    (spec: ScreenerFieldSpec) => historicalAsOf && spec.availability === 'latest_only',
    [historicalAsOf],
  )
  const { applicable: effectiveConditions, droppedCount } = useMemo(
    () => splitConditionsForAsOf(conditions, fieldsByName, historicalAsOf),
    [conditions, fieldsByName, historicalAsOf],
  )

  // ── F6: 我的方案（服务端存储，无 localStorage 兜底）──
  const qc = useQueryClient()
  const [screenName, setScreenName] = useState('')
  const [screenMsg, setScreenMsg] = useState('')
  const screens = useQuery({ queryKey: QK.screenerScreens, queryFn: api.screenerScreensList })
  const invalidateScreens = useCallback(() => {
    qc.invalidateQueries({ queryKey: QK.screenerScreens })
  }, [qc])
  const saveScreen = useMutation({
    mutationFn: (payload: {
      name: string
      conditions: ScreenerCondition[]
      order_by?: ScreenerOrderBy
      limit: number
      group_logic: ScreenerGroupLogic
    }) => api.screenerScreensCreate(payload),
    onSuccess: () => {
      setScreenName('')
      setScreenMsg('方案已保存')
      invalidateScreens()
      // 方案 CRUD 会同步注册 screen:<id> 策略, 失效策略列表缓存,
      // 避免回测页热缓存不含新方案 (孤儿清理/默认策略会把它换掉)。
      qc.invalidateQueries({ queryKey: QK.screenerStrategies })
    },
    onError: (reason: unknown) => setScreenMsg(`保存失败：${safeError(reason)}`),
  })
  const deleteScreen = useMutation({
    mutationFn: (id: string) => api.screenerScreensDelete(id),
    onSuccess: () => {
      setScreenMsg('方案已删除')
      invalidateScreens()
      qc.invalidateQueries({ queryKey: QK.screenerStrategies })
    },
    onError: (reason: unknown) => setScreenMsg(`删除失败：${safeError(reason)}`),
  })
  const flashScreenMsg = (message: string) => {
    setScreenMsg(message)
    window.setTimeout(() => setScreenMsg(''), 3000)
  }

  // ── S3: 方案 → 回测/监控（策略 id 为 screen:<id>，与后端 screen_bridge 一致）──
  const monitorRules = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList })
  const screenMonitorMap = useMemo(() => {
    const map = new Map<string, MonitorRule>()
    for (const rule of monitorRules.data?.rules ?? []) {
      if (rule.type === 'strategy' && rule.strategy_id?.startsWith('screen:')) {
        map.set(rule.strategy_id, rule)
      }
    }
    return map
  }, [monitorRules.data])
  const [monitorPending, setMonitorPending] = useState<string | null>(null)

  // 为方案创建策略监控规则（幂等：已有同 strategy_id 规则则不重复创建）
  const handleMonitorScreen = async (screen: ScreenerScreenRecord) => {
    const strategyId = screenStrategyId(screen.id)
    if (monitorPending) return
    setMonitorPending(strategyId)
    try {
      const existing = screenMonitorMap.get(strategyId)
        ?? (await api.monitorRulesList()).rules.find(rule => rule.type === 'strategy' && rule.strategy_id === strategyId)
      if (existing) {
        if (existing.enabled) {
          flashScreenMsg('已在监控中')
        } else {
          // 已有规则但未启用 → 复用同一条规则开启，不重复创建
          await api.monitorRuleSave({ ...existing, enabled: true })
          flashScreenMsg('已开启策略监控')
        }
        return
      }
      await api.monitorRuleSave({
        id: genRuleId(),
        name: `方案监控 · ${screen.name}`,
        enabled: true,
        type: 'strategy',
        scope: 'all',
        symbols: [],
        sector: null,
        strategy_id: strategyId,
        direction: 'entry',
        conditions: [],
        logic: 'or',
        cooldown_seconds: 3600,
        severity: 'info',
        message: '',
      })
      flashScreenMsg('已加入策略监控')
    } catch (reason: unknown) {
      flashScreenMsg(`监控创建失败：${safeError(reason)}`)
    } finally {
      setMonitorPending(null)
      qc.invalidateQueries({ queryKey: QK.monitorRules })
    }
  }

  // 回测此方案：方案本身即策略（screen:<id>），不携带当日结果池，由策略在回测区间内自行选股
  const sendScreenToBacktest = useCallback((screen: ScreenerScreenRecord) => {
    stageScreenerBacktestHandoff({
      target: 'strategy',
      symbols: [],
      asOf: null,
      strategyId: screenStrategyId(screen.id),
    })
    // 策略列表缓存可能不含刚保存/更新的方案, 失效后回测页 refetch 命中。
    qc.invalidateQueries({ queryKey: QK.screenerStrategies })
    navigate('/backtest')
  }, [qc, navigate])

  const handleSaveScreen = () => {
    const name = screenName.trim()
    if (!name) { flashScreenMsg('请先输入方案名称'); return }
    if (!builderValid) { flashScreenMsg('条件不完整，无法保存'); return }
    saveScreen.mutate({
      name,
      conditions: serializeScreenerConditions(conditions),
      order_by: orderBy,
      limit,
      group_logic: effectiveGroupLogic(conditions, groupLogic),
    })
  }

  const handleLoadScreen = (screen: ScreenerScreenRecord) => {
    setConditions((screen.conditions ?? []).slice(0, 20))
    setGroupLogic(screen.group_logic === 'or' ? 'or' : 'and')
    setOrderBy(screen.order_by ?? undefined)
    setLimit(Math.min(500, Math.max(1, screen.limit ?? 100)))
    setUnresolved([])
    setResult(null)
    setError(null)
  }

  const handleDeleteScreen = (screen: ScreenerScreenRecord) => {
    if (!window.confirm(`删除方案「${screen.name}」？`)) return
    deleteScreen.mutate(screen.id)
  }

  const handleBatchAdd = () => {
    if (!result?.rows.length) return
    batchAdd.mutate(result.rows.map(row => row.symbol).filter((symbol): symbol is string => typeof symbol === 'string'), {
      onSuccess: (data) => { setBatchMsg(`已添加 ${data.added} 只到自选`); window.setTimeout(() => setBatchMsg(''), 3000) },
      onError: () => { setBatchMsg('添加自选失败'); window.setTimeout(() => setBatchMsg(''), 3000) },
    })
  }

  const handleDownloadCsv = () => {
    if (!result) return
    downloadResultCsv(`条件选股_${result.as_of ?? 'latest'}.csv`, toResultCsv(resultColumns, result.rows))
  }

  // F9: 用示例跑一遍 — gostock strong_momentum，否则首个完整可执行预设
  const runExample = () => {
    const example = presets.find(preset => preset.id === 'strong_momentum')
      ?? presets.find(preset => preset.executable_level === 'full')
    if (!example) { setError('暂无完整可执行的示例预设'); return }
    applyPreset(example)
    // applyPreset 异步 setState 后直接用预设条件执行
    const payload = buildScreenerQueryRequest({
      conditions: example.predicate.conditions ?? [],
      limit: 100,
      asOf: asOf || undefined,
      orderBy: example.predicate.order_by ?? undefined,
      groupLogic: 'and',
    })
    setQueryLoading(true)
    setError(null)
    api.screenerConditionQuery(payload)
      .then(response => setResult(response))
      .catch((reason: unknown) => { setError(safeError(reason)); setResult(null) })
      .finally(() => setQueryLoading(false))
  }

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
  }, [conditions, groupLogic, asOf, orderBy, limit])

  const sortableFields = useMemo(
    () => fields.filter(field => field.sortable && field.availability === 'available'),
    [fields],
  )
  const builderValid = areConditionsValid(conditions, fields)
  // F8: 历史日期下仅最新日字段条件被剔除；剔除后至少一条条件才可执行
  const effectiveValid = areConditionsValid(effectiveConditions, fields)
  const canQuery = !metadataLoading && !metadataError && effectiveValid && unresolved.length === 0
  const activeGroups = useMemo(() => uniqueConditionGroups(conditions), [conditions])
  const showGroupLogicSwitch = activeGroups.length >= 2
  const resolvedGroupLogic = effectiveGroupLogic(conditions, groupLogic)
  const queryStatus = queryLoading
    ? { state: 'live', text: '正在执行选股…' }
    : metadataLoading
      ? { state: 'idle', text: '正在加载字段定义…' }
      : metadataError
        ? { state: 'error', text: '字段定义加载失败，暂不可执行。' }
        : unresolved.length > 0
          ? { state: 'warn', text: '请先移除未识别条件。' }
          : conditions.length === 0
            ? { state: 'idle', text: '添加至少一条条件后即可执行。' }
            : !builderValid
              ? { state: 'warn', text: '请补全每条条件的字段、运算符和值。' }
              : effectiveConditions.length === 0
                ? { state: 'warn', text: '所选条件均为「仅最新日」字段，当前历史日期不可用。' }
                : {
                    state: 'ready',
                    text: showGroupLogicSwitch
                      ? `${effectiveConditions.length} 条条件 · ${activeGroups.length} 组 · ${resolvedGroupLogic === 'or' ? '组间或' : '组间且'}，可执行。`
                      : `${effectiveConditions.length} 条条件已就绪，可执行选股。`,
                  }

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

  // F15: 行业分布 — 读 result.facets.industry（limit 前全量命中）；空 facet 整卡隐藏
  const [industryOpen, setIndustryOpen] = useState(true)
  const industryDist = useMemo(
    () => industryFacetDisplay(result?.facets, result?.total),
    [result],
  )
  const facetWarnings = useMemo(
    () => (result?.facet_warnings ?? []).filter((code): code is string => typeof code === 'string' && code.length > 0),
    [result],
  )

  const runQuery = async () => {
    if (!canQuery || queryLoading) return
    setQueryLoading(true)
    setError(null)
    const payload = buildScreenerQueryRequest({
      conditions: effectiveConditions,
      limit,
      asOf: asOf || undefined,
      orderBy,
      groupLogic,
    })
    try {
      setResult(await api.screenerConditionQuery(payload))
    } catch (reason: unknown) {
      setError(safeError(reason))
      setResult(null)
    } finally {
      setQueryLoading(false)
    }
  }

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
    setGroupLogic('and')
    setOrderBy(preset.predicate.order_by ?? undefined)
    setUnresolved([])
    setResult(null)
    setError(null)
  }

  const sendToBacktest = useCallback((target: ScreenerBacktestTarget) => {
    if (!result) return
    const count = stageScreenerBacktestHandoff({
      target,
      symbols: result.rows.map(row => row.symbol),
      asOf: result.as_of,
    })
    if (count === 0) {
      setError('当前结果没有可带入回测的标的代码。')
      return
    }
    navigate('/backtest')
  }, [navigate, result])

  return (
    <div className="workspace-page">
      <PageHeader title="条件选股" subtitle="结构化条件 · 可保存方案 · 本地数据" />

      <div className="workspace-content space-y-3">
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(20rem,0.9fr)]">
          <section className="panel h-full" aria-labelledby="condition-nl-heading">
            <div className="panel-header flex-wrap items-start sm:items-center">
              <div>
                <div className="section-kicker">Natural Language</div>
                <h2 id="condition-nl-heading" className="section-title flex items-center gap-2">
                  <ListFilter className="h-3.5 w-3.5 text-accent" />
                  自然语言辅助填充
                </h2>
              </div>
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
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
                  className="btn-secondary h-9 self-end sm:self-center"
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

          <section className="panel h-full" aria-labelledby="condition-presets-heading">
            <div className="panel-header">
              <div>
                <div className="section-kicker">Presets</div>
                <h2 id="condition-presets-heading" className="section-title">常用条件</h2>
              </div>
              <span className="text-[11px] text-muted">点击只填入条件</span>
            </div>
            <div className="panel-body flex h-full flex-col">
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

        {/* F6: 我的方案 — 服务端存储（/api/screener/screens），失败直接显示错误，不做本地兜底 */}
        <section className="panel" aria-labelledby="condition-screens-heading">
          <div className="panel-header flex-wrap items-start gap-3 sm:items-center">
            <div>
              <div className="section-kicker">My Screens</div>
              <h2 id="condition-screens-heading" className="section-title flex items-center gap-2">
                <BookmarkPlus className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
                我的方案
              </h2>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor="condition-screen-name">方案名称</label>
              <input
                id="condition-screen-name"
                value={screenName}
                maxLength={40}
                onChange={event => setScreenName(event.target.value)}
                placeholder="输入方案名称"
                className="control h-8 w-44 px-2 text-xs"
              />
              <button
                type="button"
                onClick={handleSaveScreen}
                disabled={saveScreen.isPending || !conditions.length}
                title="保存当前条件、排序与返回条数为方案"
                className="btn-secondary h-8 px-2.5 text-xs disabled:opacity-50"
              >
                {saveScreen.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" aria-hidden="true" />}
                保存当前方案
              </button>
              {screenMsg && <span className="text-[11px] text-accent" aria-live="polite">{screenMsg}</span>}
            </div>
          </div>
          <div className="panel-body">
            {screens.isLoading && <div className="text-xs text-muted">加载方案中…</div>}
            {screens.isError && (
              <div className="text-xs text-danger" role="alert">方案加载失败：{safeError(screens.error)}</div>
            )}
            {screens.data && screens.data.screens.length === 0 && (
              <div className="text-xs text-muted">暂无保存的方案。配置好条件后点「保存当前方案」。</div>
            )}
            {screens.data && screens.data.screens.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {screens.data.screens.map(screen => {
                  const strategyId = screenStrategyId(screen.id)
                  const supported = screen.strategy_supported === true
                  const unsupportedTitle = screen.unsupported_fields?.length
                    ? `含回测面板不支持的字段：${screen.unsupported_fields.join('、')}`
                    : '方案包含回测面板不支持的字段（仅当日面板字段可回测/监控）'
                  const monitored = screenMonitorMap.get(strategyId)?.enabled === true
                  return (
                    <span
                      key={screen.id}
                      className="inline-flex items-center gap-1.5 rounded-input border border-border bg-elevated px-2 py-1.5 text-xs"
                    >
                      <button
                        type="button"
                        onClick={() => handleLoadScreen(screen)}
                        title={`载入「${screen.name}」：${screen.conditions.length} 条条件`}
                        className="font-medium text-foreground hover:text-accent"
                      >
                        {screen.name}
                      </button>
                      <span className="text-[10px] text-muted num">{screen.conditions.length} 条</span>
                      <button
                        type="button"
                        onClick={() => sendScreenToBacktest(screen)}
                        disabled={!supported}
                        title={supported ? `以方案「${screen.name}」作为策略进入策略回测` : unsupportedTitle}
                        aria-label={`回测此方案 ${screen.name}`}
                        className="btn-secondary !h-6 !px-1.5 gap-1 !text-[10px] disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        <FlaskConical className="h-3 w-3" aria-hidden="true" />
                        回测此方案
                      </button>
                      <button
                        type="button"
                        onClick={() => handleMonitorScreen(screen)}
                        disabled={!supported || monitorPending === strategyId}
                        title={supported
                          ? (monitored ? '该方案已在策略监控中' : `为方案「${screen.name}」创建策略监控规则`)
                          : unsupportedTitle}
                        aria-label={`监控此方案 ${screen.name}`}
                        className={`btn-secondary !h-6 !px-1.5 gap-1 !text-[10px] disabled:cursor-not-allowed disabled:opacity-45 ${
                          monitored ? '!border-accent/40 !bg-accent/10 !text-accent' : ''
                        }`}
                      >
                        {monitorPending === strategyId
                          ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                          : <RadioTower className="h-3 w-3" aria-hidden="true" />}
                        {monitored ? '监控中' : '监控此方案'}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteScreen(screen)}
                        aria-label={`删除方案 ${screen.name}`}
                        disabled={deleteScreen.isPending}
                        className="btn-ghost !p-0.5 text-muted hover:bg-danger/10 hover:text-danger"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </span>
                  )
                })}
              </div>
            )}
          </div>
        </section>
        </div>

        <section className="panel" aria-labelledby="condition-builder-heading">
          <div className="panel-header flex-wrap items-start gap-3 sm:items-center">
            <div>
              <div className="section-kicker">Conditions</div>
              <h2 id="condition-builder-heading" className="section-title">筛选条件</h2>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <div className="flex rounded-input border border-border bg-elevated p-0.5" role="tablist" aria-label="条件编辑模式">
                {([['advanced', '高级筛选'], ['list', '逐条添加']] as const).map(([mode, label]) => (
                  <button
                    key={mode}
                    type="button"
                    role="tab"
                    aria-selected={editMode === mode}
                    onClick={() => setEditMode(mode)}
                    className={`h-7 rounded-[3px] px-2.5 text-[11px] font-medium transition-colors ${
                      editMode === mode ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {showGroupLogicSwitch ? (
                <div
                  className="flex rounded-input border border-border bg-elevated p-0.5"
                  role="group"
                  aria-label="组间逻辑"
                >
                  {([
                    ['and', '全部条件都满足'],
                    ['or', '任一分组满足（组内全部条件）'],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      aria-pressed={groupLogic === value}
                      onClick={() => setGroupLogic(value)}
                      title={label}
                      className={`h-7 rounded-[3px] px-2.5 text-[11px] font-medium transition-colors ${
                        groupLogic === value ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full border border-border bg-elevated px-2 py-1 text-[10px] font-medium tracking-wide text-secondary">
                  <SlidersHorizontal className="h-3 w-3 text-accent" aria-hidden="true" />
                  全部条件都满足
                </span>
              )}
              <span className="text-[11px] text-muted num">{conditions.length}/20 条</span>
              {showGroupLogicSwitch && (
                <span className="text-[11px] text-muted">
                  组 {activeGroups.join(' / ')}
                </span>
              )}
            </div>
          </div>
          <div className="sticky top-0 z-[2] border-b border-border bg-surface px-3 py-3" aria-label="执行选项">
            <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
              <div id="condition-query-status" className="flex min-w-0 items-center gap-2" aria-live="polite">
                <span className="status-dot" data-state={queryStatus.state} aria-hidden="true" />
                <div className="min-w-0">
                  <div className="section-kicker">Execution</div>
                  <div className="truncate text-xs text-secondary">{queryStatus.text}</div>
                </div>
              </div>
              <div className="flex min-w-0 flex-1 flex-wrap items-end gap-2.5 xl:justify-end">
                <div className="flex min-w-32 flex-1 flex-col gap-1 text-xs text-muted sm:flex-none">
                  <span id="condition-asof-label">截止日期（可选）</span>
                  <DatePicker
                    value={asOf}
                    onChange={setAsOf}
                    min={minDate}
                    max={maxDate}
                    placeholder="最新日"
                  />
                </div>
                <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs text-muted sm:flex-none">
                  排序字段
                  <select
                    value={orderBy?.field ?? ''}
                    onChange={event => {
                      const field = event.target.value
                      setOrderBy(field ? { field, direction: orderBy?.direction ?? 'desc' } : undefined)
                    }}
                    className="control h-9 min-w-36 w-full"
                  >
                    <option value="">默认顺序</option>
                    {sortableFields.map(field => <option key={field.field} value={field.field}>{field.label}</option>)}
                  </select>
                </label>
                {orderBy && (
                  <label className="flex min-w-20 flex-1 flex-col gap-1 text-xs text-muted sm:flex-none">
                    方向
                    <select value={orderBy.direction} onChange={event => setOrderBy({ ...orderBy, direction: event.target.value as 'asc' | 'desc' })} className="control h-9 w-full">
                      <option value="desc">降序</option>
                      <option value="asc">升序</option>
                    </select>
                  </label>
                )}
                <label className="flex min-w-20 flex-1 flex-col gap-1 text-xs text-muted sm:flex-none">
                  返回条数
                  <input type="number" min={1} max={500} value={limit} onChange={event => setLimit(Math.min(500, Math.max(1, Number(event.target.value) || 1)))} className="control h-9 w-full num" />
                </label>
                <button
                  type="button"
                  onClick={runExample}
                  disabled={queryLoading || metadataLoading || presets.length === 0}
                  title="填入示例预设条件并立即执行一次选股"
                  className="btn-secondary h-9 px-3"
                >
                  <Wand2 className="h-3.5 w-3.5" aria-hidden="true" />
                  用示例跑一遍
                </button>
                <button
                  type="button"
                  onClick={runQuery}
                  disabled={!canQuery || queryLoading}
                  aria-describedby="condition-query-status"
                  className="btn-primary h-9 min-w-32 px-4"
                >
                  {queryLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" aria-hidden="true" />}
                  {queryLoading ? '选股中…' : '执行选股'}
                </button>
                {droppedCount > 0 && (
                  <span className="text-[11px] text-warning" role="status">
                    已忽略 {droppedCount} 条「仅最新日」条件（历史日期不可用）
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="panel-body space-y-3">
            {metadataLoading ? (
              <div className="flex items-center gap-2 text-xs text-muted" aria-live="polite"><Loader2 className="h-3.5 w-3.5 animate-spin" />加载字段定义…</div>
            ) : metadataError ? (
              <div className="text-xs text-danger" role="alert">{metadataError}</div>
            ) : editMode === 'advanced' ? (
              <AdvancedFilterPanel fields={fields} value={conditions} onChange={setConditions} isFieldDisabled={isFieldDisabled} />
            ) : (
              <ConditionBuilder fields={fields} value={conditions} onChange={setConditions} isFieldDisabled={isFieldDisabled} />
            )}
            {fields.length > 0 && !builderValid && conditions.length > 0 && (
              <div className="text-xs text-warning" role="status">请补全每条条件的字段、运算符和值。</div>
            )}
          </div>
        </section>

        {error && <div className="rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger" role="alert" aria-live="assertive">{error}</div>}

        {result && (
          <section className="panel" aria-labelledby="condition-results-heading">
            <div className="panel-header flex-wrap items-start gap-3 sm:items-center">
              <div>
                <div className="section-kicker">Results</div>
                <h2 id="condition-results-heading" className="section-title">选股结果</h2>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-[11px] num" aria-live="polite">
                <span className="inline-flex items-baseline gap-1 rounded-input border border-border bg-elevated px-2 py-1">
                  <span className="text-muted">命中</span>
                  <strong className="text-sm font-semibold text-foreground">{result.total}</strong>
                </span>
                <span className="inline-flex items-center gap-1 rounded-input border border-border bg-elevated px-2 py-1 text-secondary">
                  <Rows3 className="h-3 w-3 text-muted" aria-hidden="true" />
                  返回 {result.rows.length}
                </span>
                <span className="rounded-input border border-border bg-elevated px-2 py-1 text-secondary">{result.as_of ?? '最新'}</span>
                <span className="rounded-input border border-border bg-elevated px-2 py-1 text-secondary">{formatElapsed(result.elapsed_ms)}</span>
                <span className="rounded-input border border-border bg-elevated px-2 py-1 text-secondary">已应用 {result.applied.length} 条</span>
              </div>
              {result.rows.length > 0 && (
                <div className="flex w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto">
                  <span className="text-[11px] text-muted">将当前返回的 {result.rows.length} 只标的带入：</span>
                  <button
                    type="button"
                    onClick={() => sendToBacktest('strategy')}
                    className="btn-secondary h-8 px-2.5 text-xs"
                    title="以当前选股结果作为股票池进入策略回测"
                  >
                    <FlaskConical className="h-3.5 w-3.5" aria-hidden="true" />
                    策略回测
                  </button>
                  <button
                    type="button"
                    onClick={() => sendToBacktest('factor')}
                    className="btn-secondary h-8 px-2.5 text-xs"
                    title="以当前选股结果作为股票池进入因子回测"
                  >
                    <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />
                    因子回测
                  </button>
                  <button
                    type="button"
                    onClick={handleBatchAdd}
                    disabled={batchAdd.isPending}
                    className="btn-secondary h-8 px-2.5 text-xs !border-accent/40 !bg-accent/10 !text-accent disabled:opacity-50"
                    title="将当前返回的标的批量加入自选"
                  >
                    <Star className="h-3 w-3" aria-hidden="true" />
                    {batchAdd.isPending ? '添加中…' : '批量加自选'}
                  </button>
                  <button
                    type="button"
                    onClick={handleDownloadCsv}
                    className="btn-secondary h-8 px-2.5 text-xs"
                    title="导出当前结果列（含条件字段）为 CSV"
                  >
                    <Download className="h-3.5 w-3.5" aria-hidden="true" />
                    导出 CSV
                  </button>
                  {batchMsg && <span className="text-xs text-accent animate-pulse" aria-live="polite">{batchMsg}</span>}
                </div>
              )}
            </div>
            <div className="panel-body !p-0">
              {(industryDist || facetWarnings.length > 0) && (
                <div className="m-3 space-y-2">
                  {facetWarnings.length > 0 && (
                    <div className="rounded-input border border-border/70 bg-elevated/50 px-2.5 py-1.5 text-[11px] text-muted" role="status">
                      {facetWarnings.map(code => facetWarningText(code)).join('；')}
                    </div>
                  )}
                  {industryDist && (
                    <div className="rounded-input border border-border bg-surface p-2.5" data-testid="screener-industry-dist">
                      <button
                        type="button"
                        onClick={() => setIndustryOpen(v => !v)}
                        aria-expanded={industryOpen}
                        className="flex w-full items-center gap-1.5 text-left"
                      >
                        <ChevronDown className={`h-3.5 w-3.5 text-muted transition-transform ${industryOpen ? '' : '-rotate-90'}`} aria-hidden="true" />
                        <span className="text-xs font-medium text-secondary">行业分布</span>
                        <span className="text-[10px] text-muted num">
                          {industryDist.items.length} 个行业 · 覆盖 {industryDist.total - industryDist.missing}/{industryDist.total} 只
                        </span>
                      </button>
                      {industryOpen && (
                        <div className="mt-2 space-y-1">
                          {industryDist.items.slice(0, 12).map(([name, count]) => (
                            <div key={name} className="flex items-center gap-2 text-[11px]">
                              <span className="w-24 shrink-0 truncate text-secondary" title={name}>{name}</span>
                              <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-elevated" aria-hidden="true">
                                <span className="block h-full rounded-full bg-accent/60" style={{ width: `${(count / industryDist.max) * 100}%` }} />
                              </span>
                              <span className="w-24 shrink-0 text-right num text-muted">
                                {count} 只 · {((count / industryDist.total) * 100).toFixed(1)}%
                              </span>
                            </div>
                          ))}
                          {industryDist.items.length > 12 && (
                            <div className="text-[10px] text-muted">
                              其余 {industryDist.items.length - 12} 个行业未展开
                            </div>
                          )}
                          {industryDist.missing > 0 && (
                            <div className="text-[10px] text-muted">另有 {industryDist.missing} 只无行业数据</div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              {result.rows.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted" aria-live="polite">没有符合条件的标的。</div>
              ) : (
                <div className="max-h-[min(60vh,32rem)] overflow-auto">
                  <table className="data-table min-w-full">
                    <caption className="sr-only">条件选股结果</caption>
                    <thead>
                      <tr>
                        {resultColumns.map(column => {
                          const numeric = column.field === 'close'
                            || column.field === 'change_pct'
                            || fieldsByName.get(column.field)?.value_type === 'numeric'
                          return <th key={column.field} scope="col" className={numeric ? 'text-right' : undefined}>{column.label}</th>
                        })}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, index) => {
                        const symbol = typeof row.symbol === 'string' ? row.symbol : null
                        const rowName = typeof row.name === 'string' ? row.name : ''
                        return (
                          <tr
                            key={`${String(row.symbol ?? index)}-${String(row.date ?? index)}`}
                            onClick={() => { if (symbol) { setPreviewSymbol(symbol); setPreviewName(rowName) } }}
                            title={symbol ? `点击查看 ${rowName || symbol} 详情` : undefined}
                          >
                            {resultColumns.map(column => {
                              const cellValue = row[column.field]
                              const numeric = column.field === 'close'
                                || column.field === 'change_pct'
                                || fieldsByName.get(column.field)?.value_type === 'numeric'
                              const tone = column.field === 'change_pct' && typeof cellValue === 'number'
                                ? cellValue > 0 ? 'text-bull' : cellValue < 0 ? 'text-bear' : 'text-secondary'
                                : column.field === 'symbol' || column.field === 'name' ? 'font-medium text-foreground' : 'text-secondary'
                              return (
                                <td key={column.field} className={`${numeric ? 'num text-right' : ''} ${tone}`}>
                                  {formatCell(cellValue)}
                                </td>
                              )
                            })}
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        )}

      <StockPreviewDialog symbol={previewSymbol} name={previewName} onClose={closePreview} />
      </div>
    </div>
  )
}
