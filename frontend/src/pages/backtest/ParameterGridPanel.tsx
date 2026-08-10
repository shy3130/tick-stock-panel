import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, BarChart3, CheckCircle2, FlaskConical, Loader2, Play, Square, XCircle } from 'lucide-react'
import {
  api,
  type ParameterGridExperiment,
  type ParameterGridScenario,
  type StrategyDetail,
  type StrategyParamDef,
} from '@/lib/api'
import { toast } from '@/components/Toast'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { REGIME_STATE_LABELS, type RegimeState } from '@/lib/regime'

const INPUT_CLS = `w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs
  focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth`

const TODAY = new Date().toISOString().slice(0, 10)
const threeMonthsAgo = () => {
  const date = new Date()
  date.setMonth(date.getMonth() - 3)
  return date.toISOString().slice(0, 10)
}
const THREE_MONTHS_AGO = threeMonthsAgo()

const OBJECTIVES = [
  { value: 'risk_adjusted', label: '风险调整收益', hint: '夏普、卡玛与回撤综合' },
  { value: 'sharpe', label: '夏普比率', hint: '单位波动收益' },
  { value: 'calmar', label: '卡玛比率', hint: '收益与最大回撤之比' },
  { value: 'total_return', label: '累计收益', hint: '不考虑波动与回撤' },
] as const

const REGIME_STATES: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']
type Objective = typeof OBJECTIVES[number]['value']
type GridDrafts = Record<string, string>

function numericDefault(param: StrategyParamDef, detail: StrategyDetail): number | null {
  const raw = detail.params_defaults[param.id] ?? param.default
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
}

function parseGridValues(raw: string, param: StrategyParamDef): { values: number[]; error: string | null } {
  const tokens = raw.split(',').map(value => value.trim()).filter(Boolean)
  if (tokens.length === 0) return { values: [], error: null }

  const values: number[] = []
  for (const token of tokens) {
    const value = Number(token)
    if (!Number.isFinite(value)) return { values: [], error: `${param.label} 含非数值“${token}”` }
    if (param.type === 'int' && !Number.isInteger(value)) return { values: [], error: `${param.label} 仅允许整数` }
    if (param.min != null && value < param.min) return { values: [], error: `${param.label} 不得小于 ${param.min}` }
    if (param.max != null && value > param.max) return { values: [], error: `${param.label} 不得大于 ${param.max}` }
    values.push(value)
  }
  return { values: [...new Set(values)].sort((a, b) => a - b), error: null }
}

function formatMetric(value: unknown, key?: string): string {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  if (key && /(return|drawdown|rate|pnl|win)/.test(key)) return `${number > 0 ? '+' : ''}${(number * 100).toFixed(2)}%`
  return number.toFixed(3)
}

function statusMeta(status: ParameterGridExperiment['status'] | null) {
  switch (status) {
    case 'pending': return { label: '等待执行', cls: 'border-amber-400/30 bg-amber-400/10 text-amber-400', Icon: Loader2 }
    case 'running': return { label: '运行中', cls: 'border-accent/30 bg-accent/10 text-accent', Icon: Loader2 }
    case 'completed': return { label: '已完成', cls: 'border-bull/30 bg-bull/10 text-bull', Icon: CheckCircle2 }
    case 'cancelled': return { label: '已取消', cls: 'border-muted/30 bg-muted/10 text-secondary', Icon: Square }
    case 'failed': return { label: '失败', cls: 'border-danger/30 bg-danger/10 text-danger', Icon: XCircle }
    default: return { label: '未启动', cls: 'border-border bg-base text-muted', Icon: FlaskConical }
  }
}

function Stat({ label, value, valueClass = 'text-foreground' }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-btn border border-border bg-surface px-3 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`mt-1 font-mono text-sm font-semibold num ${valueClass}`}>{value}</div>
    </div>
  )
}

function ScenarioParams({ params }: { params: Record<string, number> }) {
  const items = Object.entries(params)
  if (items.length === 0) return <span className="text-muted">基线参数</span>
  return (
    <span className="font-mono text-[10px] text-secondary">
      {items.map(([key, value]) => `${key}=${value}`).join(' · ')}
    </span>
  )
}

export function ParameterGridPanel() {
  const [strategyId, setStrategyId] = useState('')
  const [gridDrafts, setGridDrafts] = useState<GridDrafts>({})
  const [symbols, setSymbols] = useState('')
  const [start, setStart] = useState(THREE_MONTHS_AGO)
  const [end, setEnd] = useState(TODAY)
  const [objective, setObjective] = useState<Objective>('risk_adjusted')
  const [maxScenarios, setMaxScenarios] = useState('24')
  const [matching, setMatching] = useState<'close_t' | 'open_t+1'>('open_t+1')
  const [holdingDays, setHoldingDays] = useState('5')
  const [regimeEnabled, setRegimeEnabled] = useState(false)
  const [regimeStates, setRegimeStates] = useState<RegimeState[]>(['strong', 'lean_strong'])
  const [regimeMinScore, setRegimeMinScore] = useState('')
  const [experimentId, setExperimentId] = useState<string | null>(null)
  const [experiment, setExperiment] = useState<ParameterGridExperiment | null>(null)
  const [launching, setLaunching] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const strategies = useQuery({
    queryKey: ['parameter-grid-strategies'],
    queryFn: api.strategyList,
    staleTime: 30_000,
  })
  const detail = useQuery({
    queryKey: ['parameter-grid-strategy-detail', strategyId],
    queryFn: () => api.strategyGet(strategyId),
    enabled: Boolean(strategyId),
  })

  const numericParams = useMemo(() => (detail.data?.params ?? []).filter(param => (
    (param.type === 'int' || param.type === 'float') && param.min != null && param.max != null
  )), [detail.data])

  useEffect(() => {
    if (!detail.data) return
    setGridDrafts(Object.fromEntries(numericParams.map(param => {
      const value = numericDefault(param, detail.data!)
      return [param.id, value == null ? '' : String(value)]
    })))
  }, [detail.data, numericParams])

  const parsedGrid = useMemo(() => {
    const grid: Record<string, number[]> = {}
    let validationError: string | null = null
    for (const param of numericParams) {
      const parsed = parseGridValues(gridDrafts[param.id] ?? '', param)
      if (parsed.error && !validationError) validationError = parsed.error
      if (parsed.values.length > 0) grid[param.id] = parsed.values
    }
    return { grid, validationError }
  }, [gridDrafts, numericParams])

  const requestedScenarioCount = useMemo(() => {
    const axes = Object.values(parsedGrid.grid)
    return axes.length === 0 ? 0 : axes.reduce((count, values) => count * values.length, 1)
  }, [parsedGrid.grid])

  const baseParams = useMemo(() => {
    if (!detail.data) return null
    const params: Record<string, number> = {}
    for (const param of numericParams) {
      const value = numericDefault(param, detail.data)
      if (value != null) params[param.id] = value
    }
    return params
  }, [detail.data, numericParams])

  const isActive = experimentId != null && (
    experiment == null || experiment.status === 'pending' || experiment.status === 'running'
  )

  useEffect(() => {
    if (!experimentId || !isActive) return
    let disposed = false
    const refresh = async () => {
      try {
        const next = await api.parameterGridGet(experimentId)
        if (!disposed) {
          setExperiment(next)
          setError(null)
        }
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : '读取实验进度失败')
      }
    }
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 1_500)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [experimentId, isActive])

  const launch = async () => {
    if (!strategyId || !detail.data) {
      setError('请先选择并等待策略配置加载完成')
      return
    }
    if (parsedGrid.validationError) {
      setError(parsedGrid.validationError)
      return
    }
    if (requestedScenarioCount === 0) {
      setError('至少为一个参数填写候选值，逗号分隔多个数值即可')
      return
    }
    const max = Number(maxScenarios)
    const days = Number(holdingDays)
    if (!Number.isInteger(max) || max < 1 || max > 36) {
      setError('最大场景数必须是 1 到 36 的整数')
      return
    }
    if (!Number.isInteger(days) || days < 1) {
      setError('持有天数必须是不小于 1 的整数')
      return
    }
    if (start && end && start > end) {
      setError('开始日期不能晚于结束日期')
      return
    }
    const score = regimeMinScore === '' ? undefined : Number(regimeMinScore)
    if (score != null && (!Number.isFinite(score) || score < 0 || score > 100)) {
      setError('最低综合分应在 0 到 100 之间')
      return
    }

    setLaunching(true)
    setError(null)
    try {
      const launched = await api.parameterGridLaunch({
        strategy_id: strategyId,
        symbols: symbols ? symbols.split(',').map(symbol => symbol.trim()).filter(Boolean) : null,
        start: start || null,
        end: end || null,
        params: baseParams,
        grid: parsedGrid.grid,
        objective,
        max_scenarios: max,
        matching,
        holding_days: days,
        regime_filter: regimeEnabled ? {
          states: regimeStates.length > 0 ? regimeStates : undefined,
          min_score: score,
        } : null,
      })
      setExperimentId(launched.experiment_id)
      setExperiment(null)
      if (launched.truncated) toast(`请求 ${launched.requested_count ?? requestedScenarioCount} 个组合，已按 ${launched.scenario_count} 个上限截断`, 'success')
      else toast(`已启动 ${launched.scenario_count} 个本地历史场景`, 'success')
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '启动参数网格实验失败'
      setError(message)
      toast(message)
    } finally {
      setLaunching(false)
    }
  }

  const cancel = async () => {
    if (!experimentId || !isActive) return
    setCancelling(true)
    try {
      await api.parameterGridCancel(experimentId)
      const updated = await api.parameterGridGet(experimentId)
      setExperiment(updated)
      toast('已请求取消参数网格实验', 'success')
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '取消实验失败'
      setError(message)
      toast(message)
    } finally {
      setCancelling(false)
    }
  }

  const status = statusMeta(experiment?.status ?? null)
  const StatusIcon = status.Icon
  const progress = experiment?.total ? Math.min(100, Math.round((experiment.completed / experiment.total) * 100)) : 0
  const rankedScenarios = useMemo(() => [...(experiment?.scenarios ?? [])].sort((left, right) => {
    const leftRank = left.rank || Number.MAX_SAFE_INTEGER
    const rightRank = right.rank || Number.MAX_SAFE_INTEGER
    return leftRank - rightRank || left.scenario_id.localeCompare(right.scenario_id)
  }), [experiment?.scenarios])
  const bestScenario = experiment?.best_scenario_id
    ? experiment.scenarios.find(scenario => scenario.scenario_id === experiment.best_scenario_id) ?? null
    : null
  const robustness = experiment?.robustness
  const robustnessBootstrap = robustness?.bootstrap as Record<string, unknown> | undefined
  const robustnessPermutation = robustness?.mc_permutation as Record<string, unknown> | undefined

  return (
    <div className="h-full min-h-0 overflow-hidden rounded-card border border-border bg-surface/80 grid grid-cols-1 xl:grid-cols-[20rem_minmax(0,1fr)]">
      <section className="space-y-3 border-b border-border bg-base/25 px-3 py-3 xl:overflow-y-auto xl:border-b-0 xl:border-r">
        <div className="border-b border-border/70 pb-2">
          <div className="text-xs font-semibold text-foreground">参数网格寻优</div>
          <p className="mt-0.5 text-[10px] leading-4 text-muted">仅运行本地 DuckDB 历史数据实验；不生成荐股或下单指令。</p>
        </div>

        <div>
          <label htmlFor="parameter-grid-strategy" className="mb-1.5 block text-xs font-medium text-secondary">策略</label>
          <select
            id="parameter-grid-strategy"
            value={strategyId}
            onChange={event => {
              setStrategyId(event.target.value)
              setExperimentId(null)
              setExperiment(null)
              setError(null)
            }}
            className={INPUT_CLS}
            disabled={strategies.isLoading}
          >
            <option value="">{strategies.isLoading ? '正在加载策略…' : '选择策略'}</option>
            {(strategies.data?.strategies ?? []).map(strategy => (
              <option key={strategy.id} value={strategy.id}>{strategy.name}</option>
            ))}
          </select>
          {strategies.isError && <p className="mt-1 text-[11px] text-danger">策略列表加载失败，请稍后重试。</p>}
          {detail.data?.description && <p className="mt-1 text-[11px] leading-4 text-muted">{detail.data.description}</p>}
        </div>

        {strategyId && detail.isLoading && (
          <div className="flex items-center gap-2 rounded-btn border border-border bg-surface px-2.5 py-2 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取可优化参数…
          </div>
        )}
        {detail.isError && <div className="rounded-btn border border-danger/30 bg-danger/10 px-2.5 py-2 text-xs text-danger">策略参数读取失败，无法启动实验。</div>}

        {detail.data && (
          <div className="space-y-2 rounded-btn border border-border bg-surface p-2.5">
            <div className="flex items-center justify-between gap-2">
              <label className="text-xs font-medium text-foreground">候选参数网格</label>
              <span className="text-[10px] text-muted">逗号分隔数值</span>
            </div>
            {numericParams.length === 0 && <p className="text-[11px] leading-4 text-muted">该策略没有带范围的数值参数，不能进行参数网格寻优。</p>}
            {numericParams.map(param => (
              <label key={param.id} className="block">
                <span className="mb-1 flex items-baseline justify-between gap-2 text-[11px] text-secondary">
                  <span>{param.label}</span>
                  <span className="font-mono text-[10px] text-muted">{param.min}–{param.max}{param.type === 'int' ? ' · 整数' : ''}</span>
                </span>
                <input
                  type="text"
                  inputMode="decimal"
                  value={gridDrafts[param.id] ?? ''}
                  onChange={event => setGridDrafts(previous => ({ ...previous, [param.id]: event.target.value }))}
                  placeholder={`例如 ${numericDefault(param, detail.data) ?? ''}`}
                  className={`${INPUT_CLS} font-mono`}
                  aria-label={`${param.label} 候选值`}
                />
              </label>
            ))}
            {parsedGrid.validationError && <p className="text-[11px] text-danger">{parsedGrid.validationError}</p>}
            {requestedScenarioCount > 0 && (
              <p className="text-[11px] text-secondary">请求 <span className="font-mono num text-foreground">{requestedScenarioCount}</span> 个组合；服务端默认上限 24，硬上限 36。</p>
            )}
          </div>
        )}

        <div>
          <label htmlFor="parameter-grid-symbols" className="mb-1.5 block text-xs font-medium text-secondary">标的（可选）</label>
          <input id="parameter-grid-symbols" value={symbols} onChange={event => setSymbols(event.target.value)} placeholder="逗号分隔；留空=全市场" className={`${INPUT_CLS} font-mono`} />
        </div>

        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="text-xs font-medium text-foreground">历史区间</div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <div>
              <label className="mb-1 block text-[11px] text-secondary">开始</label>
              <DatePicker value={start} onChange={setStart} max={end || undefined} placeholder="默认区间" className="w-full" buttonClassName="w-full justify-start" align="left" />
            </div>
            <div>
              <label className="mb-1 block text-[11px] text-secondary">结束</label>
              <DatePicker value={end} onChange={setEnd} min={start || undefined} className="w-full" buttonClassName="w-full justify-start" />
            </div>
          </div>
          <div className="mt-2 flex rounded-input bg-base/60 p-0.5">
            <button type="button" onClick={() => { setStart(threeMonthsAgo()); setEnd(TODAY) }} className="flex-1 rounded-btn px-2 py-1 text-[10px] font-medium text-muted transition-colors hover:bg-elevated hover:text-secondary">近3月</button>
            <button type="button" onClick={() => { const date = new Date(); date.setMonth(date.getMonth() - 6); setStart(date.toISOString().slice(0, 10)); setEnd(TODAY) }} className="flex-1 rounded-btn px-2 py-1 text-[10px] font-medium text-muted transition-colors hover:bg-elevated hover:text-secondary">近6月</button>
            <button type="button" onClick={() => { const date = new Date(); date.setFullYear(date.getFullYear() - 1); setStart(date.toISOString().slice(0, 10)); setEnd(TODAY) }} className="flex-1 rounded-btn px-2 py-1 text-[10px] font-medium text-muted transition-colors hover:bg-elevated hover:text-secondary">近1年</button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">优化目标</span>
            <select value={objective} onChange={event => setObjective(event.target.value as Objective)} className={INPUT_CLS}>
              {OBJECTIVES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
            <span className="mt-1 block text-[10px] leading-3 text-muted">{OBJECTIVES.find(item => item.value === objective)?.hint}</span>
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">最大场景数</span>
            <input type="number" min={1} max={36} step={1} value={maxScenarios} onChange={event => setMaxScenarios(event.target.value)} className={INPUT_CLS} />
            <span className="mt-1 block text-[10px] leading-3 text-muted">默认 24，硬上限 36</span>
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">成交匹配</span>
            <select value={matching} onChange={event => setMatching(event.target.value as typeof matching)} className={INPUT_CLS}>
              <option value="open_t+1">次日开盘</option>
              <option value="close_t">当日收盘</option>
            </select>
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">持有天数</span>
            <input type="number" min={1} step={1} value={holdingDays} onChange={event => setHoldingDays(event.target.value)} className={INPUT_CLS} />
          </label>
        </div>

        <div className="rounded-btn border border-border bg-surface p-2.5">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={regimeEnabled} onChange={event => setRegimeEnabled(event.target.checked)} className="h-3.5 w-3.5 rounded border-border accent-accent" />
            <span className="text-xs font-medium text-secondary">按市场环境过滤（可选）</span>
          </label>
          {regimeEnabled && (
            <div className="mt-2 space-y-2">
              <div className="flex flex-wrap gap-1">
                {REGIME_STATES.map(state => (
                  <button
                    key={state}
                    type="button"
                    onClick={() => setRegimeStates(previous => previous.includes(state) ? previous.filter(item => item !== state) : [...previous, state])}
                    className={`rounded-btn border px-2 py-1 text-[10px] transition-colors ${regimeStates.includes(state) ? 'border-accent/40 bg-accent/10 text-foreground' : 'border-border text-muted hover:text-secondary'}`}
                  >
                    {REGIME_STATE_LABELS[state]}
                  </button>
                ))}
              </div>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">最低综合分（可选）</span>
                <input type="number" min={0} max={100} step={5} value={regimeMinScore} onChange={event => setRegimeMinScore(event.target.value)} placeholder="不限" className={INPUT_CLS} />
              </label>
            </div>
          )}
        </div>

        <button
          type="button"
          onClick={() => { void launch() }}
          disabled={launching || isActive || !strategyId || detail.isLoading || numericParams.length === 0}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-sm font-medium transition-colors duration-150 ease-smooth hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {launching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          {launching ? '正在启动…' : isActive ? '实验运行中' : '启动本地历史实验'}
        </button>
      </section>

      <section className="min-w-0 space-y-3 bg-base/15 px-3 py-3 xl:overflow-y-auto">
        <div className="rounded-card border border-amber-400/25 bg-amber-400/5 p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            <p className="text-[11px] leading-4 text-secondary"><strong className="text-amber-400">研究边界：</strong>本面板只读取本地 DuckDB 历史数据并保存实验结果，不提供买卖建议、不连接下单。网格搜索会放大数据挖掘偏差；请用未参与寻优的样本独立验证，不把单次最佳结果视为可执行结论。</p>
          </div>
        </div>

        {error && <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{error}</div>}

        {!experiment && !launching && (
          <EmptyState icon={BarChart3} title="配置网格并启动历史实验" hint="选择策略后填写一个或多个候选数值。服务端以受限组合上限运行，并在超出上限时明确标记截断。" />
        )}

        {(launching || experiment) && (
          <div className="space-y-3">
            <div className="rounded-card border border-border bg-surface p-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${status.cls}`}>
                  <StatusIcon className={`h-3 w-3 ${experiment?.status === 'pending' || experiment?.status === 'running' ? 'animate-spin' : ''}`} />
                  {launching && !experiment ? '正在创建实验' : status.label}
                </div>
                {experiment && <span className="text-[11px] text-muted">目标：{OBJECTIVES.find(item => item.value === experiment.objective)?.label ?? experiment.objective}</span>}
                {experiment?.truncated && <span className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[10px] font-medium text-amber-400">已截断</span>}
                {experiment && <span className="ml-auto font-mono text-[10px] text-muted">{experiment.experiment_id}</span>}
              </div>

              {experiment && (
                <>
                  <div className="mt-3 flex items-center justify-between gap-3 text-xs text-secondary">
                    <span>已完成 <b className="font-mono text-foreground num">{experiment.completed}</b> / {experiment.total}</span>
                    <span className="font-mono text-accent num">{progress}%</span>
                  </div>
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-base">
                    <div className="h-full rounded-full bg-accent transition-all duration-300 ease-out" style={{ width: `${progress}%` }} />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted">
                    <span>请求组合 {experiment.requested_count}</span>
                    <span>实际场景 {experiment.scenario_count}</span>
                    <span>本次上限 {experiment.max_scenarios}</span>
                    {experiment.truncated && <span className="text-amber-400">超限组合未执行</span>}
                  </div>
                </>
              )}

              {isActive && (
                <button type="button" onClick={() => { void cancel() }} disabled={cancelling} className="mt-3 inline-flex items-center gap-1.5 rounded-btn border border-danger/40 bg-danger/10 px-2.5 py-1.5 text-xs text-danger transition-colors hover:bg-danger/20 disabled:opacity-50">
                  {cancelling ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3 fill-current" />}
                  {cancelling ? '正在取消…' : '取消实验'}
                </button>
              )}
            </div>

            {experiment?.status === 'failed' && (
              <div role="alert" className="rounded-card border border-danger/30 bg-danger/10 p-3 text-xs text-danger">实验执行失败。请检查策略参数范围、日期覆盖和本地历史数据后重新发起。</div>
            )}
            {experiment?.status === 'cancelled' && (
              <div className="rounded-card border border-border bg-surface p-3 text-xs text-secondary">实验已取消；下方仅保留已完成场景，未完成场景不应作为比较依据。</div>
            )}

            {bestScenario && (
              <div className="rounded-card border border-accent/30 bg-accent/[0.04] p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold text-foreground">当前最佳场景</div>
                    <ScenarioParams params={bestScenario.params} />
                  </div>
                  <div className="text-right">
                    <div className="text-[10px] text-muted">排名 / 得分</div>
                    <div className="font-mono text-sm font-semibold text-accent num">#{bestScenario.rank} · {formatMetric(bestScenario.score)}</div>
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Stat label="累计收益" value={formatMetric(bestScenario.stats.total_return, 'total_return')} valueClass={Number(bestScenario.stats.total_return) >= 0 ? 'text-bull' : 'text-bear'} />
                  <Stat label="夏普" value={formatMetric(bestScenario.stats.sharpe)} />
                  <Stat label="卡玛" value={formatMetric(bestScenario.stats.calmar)} />
                  <Stat label="最大回撤" value={formatMetric(bestScenario.stats.max_drawdown, 'max_drawdown')} valueClass="text-bear" />
                </div>
              </div>
            )}

            {robustness && (
              <div className="rounded-card border border-border bg-surface p-3">
                <div className="flex items-baseline justify-between gap-2">
                  <div className="text-xs font-semibold text-foreground">最佳场景稳健性</div>
                  <div className="text-[10px] text-muted">仅为历史后处理，不构成预测</div>
                </div>
                <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {robustnessBootstrap && <Stat label="Bootstrap 夏普 95% 区间" value={`${formatMetric(robustnessBootstrap.ci_low)} ~ ${formatMetric(robustnessBootstrap.ci_high)}`} />}
                  {robustnessPermutation && <Stat label="置换检验 p 值" value={formatMetric(robustnessPermutation.p_value)} />}
                </div>
                {Array.isArray(robustness.exit_breakdown) && robustness.exit_breakdown.length > 0 && (
                  <div className="mt-3 overflow-x-auto">
                    <table className="w-full min-w-[28rem] text-left text-[11px]">
                      <caption className="mb-1 text-left text-[10px] text-muted">退出原因分布</caption>
                      <thead className="border-b border-border text-muted"><tr><th className="px-2 py-1.5 font-medium">原因</th><th className="px-2 py-1.5 font-medium">笔数</th><th className="px-2 py-1.5 font-medium">胜率</th><th className="px-2 py-1.5 font-medium">平均收益</th></tr></thead>
                      <tbody>{(robustness.exit_breakdown as Array<Record<string, unknown>>).map((row, index) => <tr key={`${String(row.exit_reason)}-${index}`} className="border-b border-border/50 text-secondary"><td className="px-2 py-1.5">{String(row.exit_reason ?? '—')}</td><td className="px-2 py-1.5 font-mono num">{String(row.n ?? '—')}</td><td className="px-2 py-1.5 font-mono num">{formatMetric(row.win_rate, 'win_rate')}</td><td className="px-2 py-1.5 font-mono num">{formatMetric(row.avg_pnl_pct, 'pnl')}</td></tr>)}</tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {rankedScenarios.length > 0 && (
              <div className="overflow-hidden rounded-card border border-border bg-surface">
                <div className="flex items-baseline justify-between gap-2 border-b border-border px-3 py-2.5">
                  <div className="text-xs font-semibold text-foreground">场景排名</div>
                  <div className="text-[10px] text-muted">按目标得分降序；无得分或出错场景置后</div>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[48rem] text-left text-[11px]">
                    <thead className="border-b border-border bg-base/30 text-muted">
                      <tr><th className="px-3 py-2 font-medium">排名</th><th className="px-3 py-2 font-medium">参数</th><th className="px-3 py-2 text-right font-medium">得分</th><th className="px-3 py-2 text-right font-medium">累计收益</th><th className="px-3 py-2 text-right font-medium">夏普</th><th className="px-3 py-2 text-right font-medium">最大回撤</th><th className="px-3 py-2 text-right font-medium">耗时</th><th className="px-3 py-2 font-medium">状态</th></tr>
                    </thead>
                    <tbody>
                      {rankedScenarios.map((scenario: ParameterGridScenario) => (
                        <tr key={scenario.scenario_id} className={`border-b border-border/50 last:border-0 ${scenario.scenario_id === experiment?.best_scenario_id ? 'bg-accent/[0.04]' : ''}`}>
                          <td className="px-3 py-2 font-mono text-secondary num">{scenario.rank > 0 ? `#${scenario.rank}` : '—'}</td>
                          <td className="px-3 py-2"><ScenarioParams params={scenario.params} /></td>
                          <td className="px-3 py-2 text-right font-mono text-foreground num">{formatMetric(scenario.score)}</td>
                          <td className={`px-3 py-2 text-right font-mono num ${Number(scenario.stats.total_return) >= 0 ? 'text-bull' : 'text-bear'}`}>{formatMetric(scenario.stats.total_return, 'total_return')}</td>
                          <td className="px-3 py-2 text-right font-mono text-secondary num">{formatMetric(scenario.stats.sharpe)}</td>
                          <td className="px-3 py-2 text-right font-mono text-bear num">{formatMetric(scenario.stats.max_drawdown, 'max_drawdown')}</td>
                          <td className="px-3 py-2 text-right font-mono text-secondary num">{scenario.elapsed_ms ? `${Math.round(scenario.elapsed_ms)} ms` : '—'}</td>
                          <td className="px-3 py-2">{scenario.error ? <span className="text-danger">{scenario.error}</span> : <span className="text-bull">完成</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
