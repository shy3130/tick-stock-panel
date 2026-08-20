import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Loader2, Play, Search, Square, XCircle } from 'lucide-react'
import {
  api,
  type OptimizerExperiment,
  type OptimizerScenario,
  type StrategyBacktestRequest,
  type StrategyDetail,
} from '@/lib/api'
import { toast } from '@/components/Toast'
import { EmptyState } from '@/components/EmptyState'
import { InstrumentSearchAdder } from '@/components/instruments/InstrumentSearchInput'
import {
  clearOptimizerExperimentIfCurrent,
  getOptimizerTask,
  startOptimizerExperiment,
  useOptimizerTask,
} from '@/lib/optimizerTask'
import { BacktestRunStatus } from '@/components/backtest/BacktestRunStatus'
import { MetricExplainer } from './components/MetricExplainer'
import { RUNS_KEY } from './RunHistoryPanel'

const OBJECTIVES = [
  { value: 'risk_adjusted', label: '风险调整收益' },
  { value: 'sharpe', label: '夏普' },
  { value: 'calmar', label: '卡玛' },
  { value: 'total_return', label: '累计收益' },
] as const
const HOLDINGS = [5, 10, 20]
const CHECK = 'h-3.5 w-3.5 rounded border-border accent-accent'

function formatMetric(value: unknown, key?: string): string {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  if (key && /(return|drawdown|rate|pnl|win)/.test(key)) return `${number > 0 ? '+' : ''}${(number * 100).toFixed(2)}%`
  return number.toFixed(3)
}

function statusMeta(status: OptimizerExperiment['status'] | null) {
  switch (status) {
    case 'pending':
    case 'running': return { label: status === 'pending' ? '等待执行' : '运行中', cls: 'text-accent', Icon: Loader2 }
    case 'completed': return { label: '已完成', cls: 'text-bull', Icon: CheckCircle2 }
    case 'cancelled': return { label: '已取消', cls: 'text-secondary', Icon: Square }
    case 'failed': return { label: '失败', cls: 'text-danger', Icon: XCircle }
    default: return { label: '未启动', cls: 'text-muted', Icon: Search }
  }
}

interface StrategySearchPanelProps {
  onUseScenario?: (strategyId: string) => void
  /** 场景已成功固化为 Run 后回调（用于切换到运行历史 tab） */
  onScenarioRunComplete?: () => void
}

export function StrategySearchPanel({ onUseScenario, onScenarioRunComplete }: StrategySearchPanelProps) {
  const [selected, setSelected] = useState<string[]>([])
  const [includeAllA, setIncludeAllA] = useState(true)
  const [boards, setBoards] = useState<string[]>(['main', 'gem', 'star', 'bj'])
  const [industryTopN, setIndustryTopN] = useState(0)
  const [pickedIndustries, setPickedIndustries] = useState<string[]>([])
  const [symbolsText, setSymbolsText] = useState('')
  const [perSymbol, setPerSymbol] = useState(false)
  const [holdings, setHoldings] = useState<number[]>([5, 10, 20])
  const [includeCloseT, setIncludeCloseT] = useState(false)
  const [includeCombos, setIncludeCombos] = useState(true)
  const [years, setYears] = useState(8)
  const [objective, setObjective] = useState<(typeof OBJECTIVES)[number]['value']>('risk_adjusted')
  const [minTrades, setMinTrades] = useState(10)
  const [maxScenarios, setMaxScenarios] = useState(120)
  const [paramGrid, setParamGrid] = useState<Record<string, Record<string, any[]>>>({})
  const task = useOptimizerTask()
  const experimentId = task.experimentId
  const taskRevision = task.revision
  const [loaded, setLoaded] = useState<OptimizerExperiment | null>(null)
  const experiment = loaded?.experiment_id === experimentId ? loaded : null
  const [error, setError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const pollingVersion = useRef(0)
  const queryClient = useQueryClient()
  const [runningScenario, setRunningScenario] = useState<string | null>(null)

  const strategies = useQuery({ queryKey: ['optimizer-strategies'], queryFn: api.strategyList, staleTime: 30_000 })
  const universes = useQuery({ queryKey: ['optimizer-universes'], queryFn: api.optimizerUniverses, staleTime: 60_000 })
  const strategyItems = (strategies.data?.strategies ?? []) as StrategyDetail[]

  useEffect(() => {
    if (selected.length > 0 || strategyItems.length === 0) return
    setSelected(strategyItems.filter(item => item.source === 'builtin').slice(0, 4).map(item => item.id))
  }, [strategyItems, selected.length])

  useEffect(() => {
    if (!experimentId) return
    const version = ++pollingVersion.current
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await api.optimizerGet(experimentId)
        if (getOptimizerTask().revision !== taskRevision) return
        if (next == null) {
          if (clearOptimizerExperimentIfCurrent(experimentId, taskRevision)) {
            setLoaded(null)
            setError('上次寻优实验已不可用，已清除恢复记录')
          }
          return
        }
        setLoaded(next)
        setError(null)
        if (next.status === 'running' || next.status === 'pending') {
          timer = window.setTimeout(poll, 1500)
        }
      } catch (cause) {
        if (version !== pollingVersion.current) return
        setError(cause instanceof Error ? cause.message : '读取寻优实验失败')
      }
    }
    void poll()
    return () => {
      pollingVersion.current += 1
      if (timer) window.clearTimeout(timer)
    }
  }, [experimentId, taskRevision])

  // 从已加载实验恢复 param_grid 与策略选择（experiment id 打开场景）
  useEffect(() => {
    if (!experiment) return
    if (experiment.param_grid && Object.keys(experiment.param_grid).length > 0) {
      setParamGrid(experiment.param_grid)
    }
    const used = Array.from(new Set(
      (experiment.scenarios ?? [])
        .map(r => r.strategy_id)
        .filter((id: string) => !id.startsWith('combo:'))
    ))
    if (used.length > 0 && selected.length === 0) {
      setSelected(used.slice(0, 8))
    }
  }, [experiment?.experiment_id])

  // 清理不再选中的策略的参数网格
  useEffect(() => {
    setParamGrid(prev => {
      const next: Record<string, Record<string, any[]>> = {}
      for (const sid of selected) if (prev[sid]) next[sid] = prev[sid]
      return next
    })
  }, [selected])

  const symbols = useMemo(
    () => symbolsText.split(',').map(item => item.trim()).filter(Boolean),
    [symbolsText],
  )
  const estimated = useMemo(() => {
    const universeCount = Number(includeAllA)
      + boards.length
      + (pickedIndustries.length || industryTopN)
      + (symbols.length > 0 && !perSymbol ? 1 : 0)
      + (perSymbol ? symbols.length : 0)
    const matchingCount = includeCloseT ? 2 : 1
    const comboCount = includeCombos && selected.length >= 2
      ? Math.min(8, selected.length * (selected.length - 1) / 2)
      : 0
    return Math.max(0, (selected.length + comboCount) * universeCount * holdings.length * matchingCount)
  }, [includeAllA, boards.length, pickedIndustries.length, industryTopN, symbols.length, perSymbol, selected.length, holdings.length, includeCloseT, includeCombos])

  const canRun = selected.length > 0
    && (includeAllA || boards.length > 0 || pickedIndustries.length > 0 || industryTopN > 0 || symbols.length > 0)
    && holdings.length > 0
    && !task.isLaunching
    && experiment?.status !== 'running'
    && experiment?.status !== 'pending'

  const launch = async () => {
    setError(null)
    try {
      await startOptimizerExperiment({
        strategy_ids: selected,
        symbols: symbols.length ? symbols : null,
        include_all_a: includeAllA,
        boards,
        industries: pickedIndustries,
        industry_top_n: pickedIndustries.length ? 0 : industryTopN,
        per_symbol: perSymbol && symbols.length > 0,
        holding_days: holdings,
        matchings: includeCloseT ? ['open_t+1', 'close_t'] : ['open_t+1'],
        years,
        objective,
        min_trades: minTrades,
        max_scenarios: maxScenarios,
        include_combos: includeCombos,
        param_grid: Object.keys(paramGrid).length ? paramGrid : undefined,
      })
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '启动寻优失败'
      setError(message)
      toast(message)
    }
  }

  const cancel = async () => {
    if (!experimentId) return
    setCancelling(true)
    try {
      await api.optimizerCancel(experimentId)
      toast('已请求取消寻优实验', 'success')
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '取消失败'
      setError(message)
    } finally {
      setCancelling(false)
    }
  }

  const experimentRangeReady = Boolean(experiment?.start && experiment?.end)

  const scenarioRunState = (row: OptimizerScenario): { disabled: boolean; title: string } => {
    if (!experimentRangeReady) {
      return { disabled: true, title: '寻优实验区间不可得，无法构造运行请求' }
    }
    if (!(row.holding_days >= 1) || (row.matching !== 'close_t' && row.matching !== 'open_t+1')) {
      return { disabled: true, title: '场景参数不完整（持仓周期或成交口径无效），无法构造运行请求' }
    }
    return {
      disabled: runningScenario !== null,
      title: `按寻优区间 ${experiment?.start} → ${experiment?.end}、${row.holding_days} 日持仓、${row.matching} 口径运行并持久化为 Run（股票池按回测默认全市场，费用/仓位等用默认值）`,
    }
  }

  /** 把寻优场景固化为一次持久化 Run：POST /strategy/run 本身会写运行历史，不写策略池 */
  const runScenario = async (row: OptimizerScenario) => {
    if (runningScenario || row.strategy_id.startsWith('combo:')) return
    if (!experiment?.start || !experiment.end) return
    if (!(row.holding_days >= 1) || (row.matching !== 'close_t' && row.matching !== 'open_t+1')) return
    setRunningScenario(row.scenario_id)
    try {
      const request: StrategyBacktestRequest = {
        strategy_id: row.strategy_id,
        symbols: null,
        start: experiment.start,
        end: experiment.end,
        matching: row.matching,
        holding_days: row.holding_days,
        mode: 'position',
      }
      const result = await api.strategyBacktestRun(request)
      if (result?.error) {
        toast(`场景运行失败：${result.error}`)
        return
      }
      void queryClient.invalidateQueries({ queryKey: RUNS_KEY })
      toast(`已固化为 Run：${row.strategy_label || row.strategy_id}（${row.holding_days}日 · ${row.matching}）`, 'success')
      onScenarioRunComplete?.()
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : '场景运行失败')
    } finally {
      setRunningScenario(null)
    }
  }

  const status = statusMeta(experiment?.status ?? null)
  const StatusIcon = status.Icon
  const recommended = new Set(experiment?.recommended_ids ?? [])
  const ranked = experiment?.scenarios ?? []
  const pbo = experiment?.diagnostics?.pbo?.pbo
  const dsr = experiment?.diagnostics?.dsr

  const toggle = <T,>(list: T[], value: T, setter: (next: T[]) => void) => {
    setter(list.includes(value) ? list.filter(item => item !== value) : [...list, value])
  }

  return (
    <div className="h-full min-h-0 min-w-0 grid grid-cols-1 xl:grid-cols-[20rem_minmax(0,1fr)] gap-3">
      <section className="panel flex flex-col min-h-0 xl:overflow-y-auto">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Search</div>
            <h2 className="section-title">策略寻优</h2>
          </div>
        </div>
        <div className="panel-body space-y-3">
          <p className="text-[11px] leading-4 text-muted">
            最近 {years} 年冻结窗口，训练期打分、留出期确认。不宣称全局最优，也不写入策略池。
          </p>

          <div>
            <div className="mb-1.5 text-xs font-medium text-secondary">策略</div>
            <div className="max-h-40 overflow-y-auto space-y-1 rounded-input border border-border p-2">
              {strategyItems.map(item => (
                <label key={item.id} className="flex items-center gap-2 text-[11px] text-secondary">
                  <input type="checkbox" className={CHECK} checked={selected.includes(item.id)} onChange={() => toggle(selected, item.id, setSelected)} />
                  <span className="truncate">{item.name}</span>
                </label>
              ))}
            </div>
            <label className="mt-2 flex items-center gap-2 text-[11px] text-secondary">
              <input type="checkbox" className={CHECK} checked={includeCombos} onChange={event => setIncludeCombos(event.target.checked)} />
              包含两两并集叠加（最多 8 组，不写入策略池）
            </label>
          </div>

          {/* F16: 参数候选编辑器（每个已选策略独立 ≤2 参数、≤5 值、积≤8） */}
          {selected.length > 0 && (
            <div className="space-y-2">
              <div className="text-[11px] font-medium text-secondary">参数候选（可选，展开编辑）</div>
              {selected.map(sid => {
                const def = strategyItems.find(s => s.id === sid)
                const pdefs = (def?.params ?? []).filter(p => p && p.id)
                if (pdefs.length === 0) return null
                const cur = paramGrid[sid] || {}
                const entries = Object.entries(cur)
                const toLen = (v: unknown): number => (Array.isArray(v) ? v.length : 0)
                const prod = entries.reduce((acc, [, v]) => acc * toLen(v), 1) || 1
                const over = entries.length > 2 || entries.some(([, v]) => toLen(v) > 5) || prod > 8

                return (
                  <div key={sid} className="rounded border border-border p-2 text-[11px] bg-elevated/30">
                    <div className="font-medium mb-1 truncate">{def?.name || sid}</div>
                    {entries.map(([pname, vals], idx) => (
                      <div key={idx} className="flex items-center gap-1.5 mb-1">
                        <select
                          className="control text-xs w-28"
                          value={pname}
                          onChange={e => {
                            const nextName = e.target.value
                            setParamGrid(prev => {
                              const s = { ...(prev[sid] || {}) }
                              const v = s[pname]; delete s[pname]
                              if (nextName) s[nextName] = v || []
                              return { ...prev, [sid]: s }
                            })
                          }}
                        >
                          {pdefs.map(pd => (
                            <option key={pd.id} value={pd.id}>{pd.label || pd.id}</option>
                          ))}
                        </select>
                        <input
                          className="control flex-1 text-xs"
                          value={Array.isArray(vals) ? (vals as unknown as (string | number | boolean)[]).join(',') : ''}
                          onChange={e => {
                            const arr = e.target.value.split(',').map(x => x.trim()).filter(Boolean)
                            setParamGrid(prev => ({
                              ...prev,
                              [sid]: { ...(prev[sid] || {}), [pname]: arr },
                            }))
                          }}
                        />
                        <button
                          type="button"
                          className="text-danger"
                          onClick={() => setParamGrid(prev => {
                            const s = { ...(prev[sid] || {}) }
                            delete s[pname]
                            return { ...prev, [sid]: s }
                          })}
                        >×</button>
                      </div>
                    ))}
                    {entries.length < 2 && (
                      <button
                        type="button"
                        className="text-[10px] text-accent"
                        onClick={() => {
                          const used = new Set(entries.map(([k]) => k))
                          const avail = pdefs.find(pd => !used.has(pd.id))
                          if (!avail) return
                          setParamGrid(prev => ({
                            ...prev,
                            [sid]: { ...(prev[sid] || {}), [avail.id]: [] },
                          }))
                        }}
                      >+ 添加参数</button>
                    )}
                    {over && <div className="text-[10px] text-danger mt-0.5">参数/取值/组合超出限制（≤2参、≤5值、积≤8）</div>}
                  </div>
                )
              })}
            </div>
          )}

          <div>
            <div className="mb-1.5 text-xs font-medium text-secondary">股票池</div>
            <label className="flex items-center gap-2 text-[11px] text-secondary">
              <input type="checkbox" className={CHECK} checked={includeAllA} onChange={event => setIncludeAllA(event.target.checked)} />
              全 A
            </label>
            <div className="mt-1 flex flex-wrap gap-2">
              {(universes.data?.boards ?? []).map(board => (
                <label key={board.id} className="flex items-center gap-1.5 text-[11px] text-secondary">
                  <input type="checkbox" className={CHECK} checked={boards.includes(board.id)} onChange={() => toggle(boards, board.id, setBoards)} />
                  {board.label}
                </label>
              ))}
            </div>
            <label className="mt-2 block text-[11px] text-secondary">
              自动纳入成员最多的行业
              <select className="control mt-1 w-full text-xs" value={industryTopN} onChange={event => setIndustryTopN(Number(event.target.value))}>
                <option value={0}>不自动纳入</option>
                <option value={4}>前 4 个行业</option>
                <option value={8}>前 8 个行业</option>
              </select>
            </label>
            {!!universes.data?.industries?.length && (
              <div className="mt-2 max-h-28 overflow-y-auto space-y-1 rounded-input border border-border p-2">
                {universes.data.industries.slice(0, 16).map(item => (
                  <label key={item.id} className="flex items-center justify-between gap-2 text-[11px] text-secondary">
                    <span className="flex items-center gap-2 min-w-0">
                      <input type="checkbox" className={CHECK} checked={pickedIndustries.includes(item.id)} onChange={() => toggle(pickedIndustries, item.id, setPickedIndustries)} />
                      <span className="truncate">{item.label}</span>
                    </span>
                    <span className="font-mono text-muted">{item.count}</span>
                  </label>
                ))}
              </div>
            )}
            <div className="mt-2">
              <InstrumentSearchAdder
                onAdd={(result) => setSymbolsText(current => {
                  const symbol = result.symbol
                  const existing = current.split(',').map(v => v.trim()).filter(Boolean)
                  return existing.includes(symbol) ? current : existing.length ? `${current},${symbol}` : symbol
                })}
              />
              <textarea className="control mt-2 w-full text-xs min-h-16" value={symbolsText} onChange={event => setSymbolsText(event.target.value)} placeholder="自定义标的，逗号分隔" />
              <label className="mt-1 flex items-center gap-2 text-[11px] text-secondary">
                <input type="checkbox" className={CHECK} checked={perSymbol} onChange={event => setPerSymbol(event.target.checked)} />
                按个股展开（最多 8 只）
              </label>
            </div>
          </div>

          <div>
            <div className="mb-1.5 text-xs font-medium text-secondary">周期与目标</div>
            <div className="flex flex-wrap gap-2">
              {HOLDINGS.map(day => (
                <label key={day} className="flex items-center gap-1.5 text-[11px] text-secondary">
                  <input type="checkbox" className={CHECK} checked={holdings.includes(day)} onChange={() => toggle(holdings, day, setHoldings)} />
                  {day} 日
                </label>
              ))}
              <label className="flex items-center gap-1.5 text-[11px] text-secondary">
                <input type="checkbox" className={CHECK} checked={includeCloseT} onChange={event => setIncludeCloseT(event.target.checked)} />
                含收盘成交
              </label>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <label className="text-[11px] text-secondary">
                年数
                <input className="control mt-1 w-full text-xs" type="number" min={1} max={15} value={years} onChange={event => setYears(Number(event.target.value) || 8)} />
              </label>
              <label className="text-[11px] text-secondary">
                目标
                <select className="control mt-1 w-full text-xs" value={objective} onChange={event => setObjective(event.target.value as typeof objective)}>
                  {OBJECTIVES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
                </select>
              </label>
              <label className="text-[11px] text-secondary">
                最少成交
                <input className="control mt-1 w-full text-xs" type="number" min={1} value={minTrades} onChange={event => setMinTrades(Number(event.target.value) || 10)} />
              </label>
              <label className="text-[11px] text-secondary">
                场景上限
                <input className="control mt-1 w-full text-xs" type="number" min={1} max={240} value={maxScenarios} onChange={event => setMaxScenarios(Number(event.target.value) || 120)} />
              </label>
            </div>
          </div>

          <div className="rounded-input border border-border bg-elevated/40 px-3 py-2 text-[11px] text-secondary">
            预估 {estimated} 个场景{estimated > maxScenarios ? `，将抽样至 ${maxScenarios}` : ''}
          </div>

          {error && <div className="rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">{error}</div>}

          <div className="flex gap-2">
            <button type="button" className="btn-primary flex-1" disabled={!canRun} onClick={() => void launch()}>
              {task.isLaunching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              开始寻优
            </button>
            <button type="button" className="btn-ghost" disabled={!experimentId || cancelling} onClick={() => void cancel()}>
              取消
            </button>
          </div>
        </div>
      </section>

      <section className="panel flex flex-col min-h-0 overflow-hidden">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Result</div>
            <h2 className="section-title">训练 / 留出对照</h2>
          </div>
          <div className={`inline-flex items-center gap-1.5 text-[11px] ${status.cls}`}>
            <StatusIcon className={`h-3.5 w-3.5 ${experiment?.status === 'running' ? 'animate-spin' : ''}`} />
            {status.label}
            {experiment && <span className="text-muted">{experiment.completed}/{experiment.total}</span>}
          </div>
        </div>
        <div className="panel-body min-h-0 overflow-y-auto space-y-3">
          {(task.isLaunching || experiment?.status === 'pending' || experiment?.status === 'running') && (
            <BacktestRunStatus
              status={task.isLaunching && !experiment ? 'pending' : (experiment?.status === 'pending' ? 'pending' : 'running')}
              title={task.isLaunching && !experiment ? '正在创建寻优实验' : '策略寻优运行中'}
              runtime={experiment?.runtime}
              completed={experiment?.completed}
              total={experiment?.total}
              startedAt={experiment?.created_at}
              extras={experiment ? [
                { label: '请求', value: String(experiment.requested_count) },
                { label: '场景', value: String(experiment.scenario_count) },
                ...(experiment.truncated ? [{ label: '抽样', value: '已截断' }] : []),
              ] : []}
              onCancel={() => { void cancel() }}
              cancelling={cancelling}
            />
          )}
          {!experiment && !task.isLaunching && (
            <EmptyState title="尚未运行寻优" hint="选择策略、股票池和持仓周期后，系统会在冻结的训练窗打分，再把 Top 候选拿到留出窗确认。" />
          )}

          {experiment && (
            <>
              <div className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-4 text-secondary">
                <span className="inline-flex items-center gap-1 font-medium text-warning"><AlertTriangle className="h-3.5 w-3.5" />方法论</span>
                训练 {experiment.start} → {experiment.train_end}，留出 {experiment.holdout_start} → {experiment.end}。
                排序只用训练期；推荐 = 留出收益为正且成交数达标。全市场/板块/行业池无法证明历史时点成分。
                {experiment.truncated && ' 场景已抽样，不是穷举。'}
              </div>

              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label={<span className="inline-flex items-center gap-1">Deflated Sharpe<MetricExplainer term="dsr" /></span>} value={formatMetric(dsr)} hint="多重检验后仍为正的概率" />
                <Stat label={<span className="inline-flex items-center gap-1">PBO<MetricExplainer term="pbo" /></span>} value={pbo == null ? '—' : formatMetric(pbo)} hint="过拟合概率，越低越好" />
                <Stat label="留出通过" value={String(experiment.recommended_ids.length)} />
              </div>

              {experiment.ensemble && (
                <div className="rounded-btn border border-border bg-elevated/30 p-3 text-[11px] text-secondary">
                  <div className="font-medium text-foreground">留出期等权组合</div>
                  <div className="mt-1">收益 {formatMetric(experiment.ensemble.total_return, 'total_return')} · 日频夏普 {formatMetric(experiment.ensemble.daily_sharpe)}</div>
                  <div className="mt-1 text-muted">{experiment.ensemble.note}</div>
                </div>
              )}

              <div className="data-table-scroll">
                <table className="data-table min-w-[52rem]">
                  <thead>
                    <tr>
                      <th>排名</th>
                      <th>策略</th>
                      <th>参数</th>
                      <th>股票池</th>
                      <th>周期</th>
                      <th>训练收益</th>
                      <th>训练夏普</th>
                      <th>留出收益</th>
                      <th>留出夏普</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {ranked.map(row => (
                      <tr key={row.scenario_id} className={recommended.has(row.scenario_id) ? 'bg-bull/5' : undefined}>
                        <td className="font-mono num">{row.rank}</td>
                        <td>{row.strategy_label || row.strategy_id}</td>
                        <td className="font-mono text-[10px] text-secondary">
                          {row.params && Object.keys(row.params).length
                            ? Object.entries(row.params).map(([k, v]) => `${k}=${v}`).join(' ')
                            : '—'}
                        </td>
                        <td>{row.universe_label}</td>
                        <td className="font-mono">{row.holding_days}d</td>
                        <td className="font-mono num">{formatMetric(row.train_stats.total_return, 'total_return')}</td>
                        <td className="font-mono num">{formatMetric(row.train_stats.sharpe)}</td>
                        <td className="font-mono num">{formatMetric(row.holdout_stats?.total_return, 'total_return')}</td>
                        <td className="font-mono num">{formatMetric(row.holdout_stats?.sharpe)}</td>
                        <td>
                          {row.admitted && <span className="text-[10px] text-bull">留出通过</span>}
                          {onUseScenario && !row.strategy_id.startsWith('combo:') && (
                            <button type="button" className="ml-2 text-[10px] text-accent" onClick={() => onUseScenario(row.strategy_id)}>
                              回填
                            </button>
                          )}
                          {!row.strategy_id.startsWith('combo:') && (() => {
                            const runState = scenarioRunState(row)
                            return (
                              <button
                                type="button"
                                className="ml-1.5 inline-flex items-center gap-0.5 text-[10px] text-accent disabled:cursor-not-allowed disabled:text-muted"
                                disabled={runState.disabled}
                                title={runState.title}
                                onClick={() => { void runScenario(row) }}
                              >
                                {runningScenario === row.scenario_id && <Loader2 className="h-3 w-3 animate-spin" />}
                                运行为Run
                              </button>
                            )
                          })()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {ranked.some(row => row.phases.length > 0) && (
                <PhaseTable rows={ranked.filter(row => recommended.has(row.scenario_id) || row.rank <= 3)} />
              )}
            </>
          )}
        </div>
      </section>
    </div>
  )
}

function Stat({ label, value, hint }: { label: ReactNode; value: string; hint?: string }) {
  return (
    <div className="rounded-btn border border-border bg-elevated/40 px-3 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="metric-value mt-1 !text-sm">{value}</div>
      {hint && <div className="mt-1 text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

function PhaseTable({ rows }: { rows: OptimizerScenario[] }) {
  const phases = rows[0]?.phases ?? []
  if (phases.length === 0) return null
  return (
    <div>
      <div className="mb-1 text-xs font-medium text-secondary">分阶段收益（只报告，不参与排序）</div>
      <div className="data-table-scroll">
        <table className="data-table min-w-[28rem]">
          <thead>
            <tr>
              <th>场景</th>
              {phases.map(phase => <th key={phase.id}>{phase.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.scenario_id}>
                <td>{row.strategy_label || row.strategy_id} · {row.universe_label} · {row.holding_days}d</td>
                {row.phases.map(phase => (
                  <td key={phase.id} className="font-mono num">{formatMetric(phase.total_return, 'total_return')}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

