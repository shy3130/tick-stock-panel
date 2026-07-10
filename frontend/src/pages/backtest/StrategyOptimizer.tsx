import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Play, Square, Trophy } from 'lucide-react'
import { api, type StrategyDetail, type StrategyParamDef } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import {
  startOptimize,
  stopOptimize,
  clearOptimize,
  tryReconnectOptimize,
  useOptimizerTask,
} from '@/lib/optimizerTask'
import { buildDefaultOverrides } from '@/lib/strategyOverrides'

const INPUT_CLS = 'w-full px-2.5 py-1.5 rounded-input bg-surface border border-border text-xs focus:outline-none focus:border-accent'

// 可选优化目标 (对齐后端 VALID_OBJECTIVES) + 中文标签 + 是否越小越好
const OBJECTIVES: { id: string; label: string; min?: boolean }[] = [
  { id: 'sortino', label: '索提诺比率' },
  { id: 'sharpe', label: '夏普比率' },
  { id: 'calmar', label: 'Calmar 比率' },
  { id: 'total_return', label: '总收益' },
  { id: 'annual_return', label: '年化收益' },
  { id: 'win_rate', label: '胜率' },
  { id: 'profit_factor', label: '盈亏比' },
  { id: 'max_drawdown', label: '最大回撤(越小越好)' },
  { id: 'mc_maxdd_p95', label: '蒙卡回撤P95(越小越好)' },
  { id: 'avg_holding_days', label: '平均持仓天数', min: true },
]

// 单个可扫参数的网格配置
interface Sweep {
  enabled: boolean
  min: string
  max: string
  step: string
}

function defaultSweep(p: StrategyParamDef): Sweep {
  return {
    enabled: false,
    min: String(p.min ?? p.default ?? 0),
    max: String(p.max ?? p.default ?? 1),
    step: String(p.step ?? (p.type === 'int' ? 1 : 0.01)),
  }
}

/** 从 sweep 配置估算某参数候选值个数 (与后端整数计数一致) */
function candidateCount(p: StrategyParamDef, s: Sweep): number {
  if (p.type === 'bool') return 2
  if (p.type === 'select') return p.options?.length ?? 1
  const lo = Number(s.min), hi = Number(s.max), step = Number(s.step)
  if (!(step > 0) || hi < lo) return 0
  return Math.round((hi - lo) / step) + 1
}

/** 校验某数值参数的 sweep 是否会被后端拒绝 (与后端 _candidates_for 同口径)。
 * 后端按 lo+i*step 生成 (i=0..round((hi-lo)/step)), 任一值超出 [min,max] 即报错。 */
function sweepError(p: StrategyParamDef, s: Sweep): string | null {
  if (p.type === 'bool' || p.type === 'select') return null
  const lo = Number(s.min), hi = Number(s.max), step = Number(s.step)
  if (Number.isNaN(lo) || Number.isNaN(hi) || Number.isNaN(step)) return `${p.label}: 范围/步长非法`
  if (!(step > 0)) return `${p.label}: 步长必须为正`
  if (hi < lo) return `${p.label}: max < min`
  if (p.min != null && lo < p.min - 1e-9) return `${p.label}: min 小于允许下限 ${p.min}`
  if (p.max != null && hi > p.max + 1e-9) return `${p.label}: max 超出允许上限 ${p.max}`
  // 后端生成的末值 lo + round((hi-lo)/step)*step 若 > max, 会被拒
  const nSteps = Math.round((hi - lo) / step)
  const last = lo + nSteps * step
  if (last > hi + 1e-9) return `${p.label}: 步长 ${step} 不整除区间, 末值 ${last.toFixed(4)} 超出 max ${hi}`
  return null
}

const TODAY = new Date().toISOString().slice(0, 10)
const ONE_YEAR_AGO = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10)

export function StrategyOptimizer() {
  const task = useOptimizerTask()
  const { data: stratData } = useQuery({ queryKey: ['strategies'], queryFn: api.strategyList })
  const strategies: StrategyDetail[] = stratData?.strategies ?? []

  const [strategyId, setStrategyId] = useState<string>('')
  const [objective, setObjective] = useState('sortino')
  const [start, setStart] = useState(ONE_YEAR_AGO)
  const [end, setEnd] = useState(TODAY)
  const [mode, setMode] = useState<'position' | 'full'>('position')
  const [sweeps, setSweeps] = useState<Record<string, Sweep>>({})

  const selected = strategies.find(s => s.id === strategyId)
  const params = selected?.params ?? []

  // 刷新/切页后: 恢复未完成的优化任务
  useEffect(() => {
    tryReconnectOptimize()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 切策略: 若有任务在跑, 先真正取消 (关 SSE + 后端 cancel + 清 localStorage), 不能静默丢。
  const onSelectStrategy = (id: string) => {
    if (task?.isPending) stopOptimize()
    else clearOptimize()
    setStrategyId(id)
    const s = strategies.find(x => x.id === id)
    const init: Record<string, Sweep> = {}
    for (const p of s?.params ?? []) init[p.id] = defaultSweep(p)
    setSweeps(init)
  }

  const updateSweep = (pid: string, patch: Partial<Sweep>) =>
    setSweeps(prev => ({ ...prev, [pid]: { ...prev[pid], ...patch } }))

  // 组合数预估
  const combos = useMemo(() => {
    const enabled = params.filter(p => sweeps[p.id]?.enabled)
    if (!enabled.length) return 0
    return enabled.reduce((acc, p) => acc * candidateCount(p, sweeps[p.id]), 1)
  }, [params, sweeps])

  // 网格合法性 (与后端展开同口径): 步长不整除/越界会被后端拒, 前端提前拦。
  const gridError = useMemo(() => {
    for (const p of params) {
      if (!sweeps[p.id]?.enabled) continue
      const err = sweepError(p, sweeps[p.id])
      if (err) return err
    }
    return null
  }, [params, sweeps])

  const buildGrid = (): Record<string, any> => {
    const grid: Record<string, any> = {}
    for (const p of params) {
      const s = sweeps[p.id]
      if (!s?.enabled) continue
      if (p.type === 'bool') grid[p.id] = [true, false]
      else if (p.type === 'select') grid[p.id] = p.options ?? []
      else grid[p.id] = { min: Number(s.min), max: Number(s.max), step: Number(s.step) }
    }
    return grid
  }

  const canRun = strategyId && combos > 0 && combos <= 2000 && !gridError && !task?.isPending

  const onRun = () => {
    if (!canRun) return
    clearOptimize()
    startOptimize({
      strategy_id: strategyId,
      param_grid: buildGrid(),
      objective,
      // 未扫描参数固定为策略当前默认值; overrides 让 basic_filter/信号/风控按当前策略参与,
      // 保证优化的就是用户实际回测的策略 (而非被剥离配置的裸策略)。
      params: selected?.params_defaults,
      overrides: selected ? buildDefaultOverrides(selected) : undefined,
      start,
      end,
      mode,
    })
  }

  const result = task?.result
  const progress = task?.progress

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[320px_1fr]">
      {/* ── 配置面板 ── */}
      <div className="space-y-3 rounded-card border border-border bg-surface p-4">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">策略</label>
          <select value={strategyId} onChange={e => onSelectStrategy(e.target.value)} className={INPUT_CLS}>
            <option value="">选择策略…</option>
            {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">优化目标</label>
          <select value={objective} onChange={e => setObjective(e.target.value)} className={INPUT_CLS}>
            {OBJECTIVES.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">起始</label>
            <DatePicker value={start} onChange={setStart} />
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">结束</label>
            <DatePicker value={end} onChange={setEnd} />
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">模式</label>
          <select value={mode} onChange={e => setMode(e.target.value as any)} className={INPUT_CLS}>
            <option value="position">组合仓位</option>
            <option value="full">全量独立</option>
          </select>
        </div>

        {/* 可扫参数 */}
        {params.length > 0 && (
          <div>
            <div className="mb-1.5 text-xs font-medium text-secondary">扫描参数 (勾选后设范围)</div>
            <div className="space-y-2">
              {params.map(p => {
                const s = sweeps[p.id] ?? defaultSweep(p)
                const numeric = p.type === 'float' || p.type === 'int'
                return (
                  <div key={p.id} className="rounded-input border border-border/60 p-2">
                    <label className="flex items-center gap-2 text-xs">
                      <input type="checkbox" checked={s.enabled} onChange={e => updateSweep(p.id, { enabled: e.target.checked })} />
                      <span className="font-medium text-foreground">{p.label}</span>
                      <span className="text-secondary">({p.type})</span>
                    </label>
                    {s.enabled && numeric && (
                      <div className="mt-2 grid grid-cols-3 gap-1.5">
                        <input type="number" value={s.min} onChange={e => updateSweep(p.id, { min: e.target.value })} placeholder="min" className={INPUT_CLS} />
                        <input type="number" value={s.max} onChange={e => updateSweep(p.id, { max: e.target.value })} placeholder="max" className={INPUT_CLS} />
                        <input type="number" value={s.step} onChange={e => updateSweep(p.id, { step: e.target.value })} placeholder="step" className={INPUT_CLS} />
                      </div>
                    )}
                    {s.enabled && !numeric && (
                      <div className="mt-1 text-[11px] text-secondary">
                        {p.type === 'bool' ? '扫描 [是 / 否]' : `扫描全部选项 (${p.options?.length ?? 0})`}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* 组合数 / 校验提示 */}
        {strategyId && (
          <div className={`text-xs ${(combos > 2000 || gridError) ? 'text-red-400' : 'text-secondary'}`}>
            {gridError
              ? gridError
              : combos === 0
                ? '请至少勾选一个参数'
                : `共 ${combos} 组参数组合${combos > 2000 ? ' — 超过上限 2000, 请增大 step 或缩小范围' : ''}`}
          </div>
        )}

        {task?.isPending ? (
          <button onClick={stopOptimize} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-red-500/90 px-3 py-2 text-xs font-medium text-white hover:bg-red-500">
            <Square className="h-3.5 w-3.5" /> 停止
          </button>
        ) : (
          <button onClick={onRun} disabled={!canRun} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed">
            <Play className="h-3.5 w-3.5" /> 开始优化
          </button>
        )}
      </div>

      {/* ── 结果面板 ── */}
      <div className="min-h-[300px] rounded-card border border-border bg-surface p-4">
        {task?.error && (
          <div className="mb-3 rounded-input border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">{task.error}</div>
        )}

        {task?.isPending && progress && (
          <div className="mb-4">
            <div className="mb-1 flex justify-between text-xs text-secondary">
              <span>进度 {progress.done}/{progress.total}</span>
              <span>当前最优: {progress.best_score != null ? progress.best_score.toFixed(3) : '—'}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
              <div className="h-full bg-accent transition-all" style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </div>
        )}

        {!result && !task?.isPending && (
          <EmptyState title="参数优化" hint="选择策略、勾选要扫描的参数与优化目标，网格搜索会并行回测所有组合并按目标排序。" />
        )}

        {result && (
          <div className="space-y-4">
            {/* 最优参数 */}
            {result.best_params && (
              <div className="rounded-card border border-accent/30 bg-accent/5 p-3">
                <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-accent">
                  <Trophy className="h-3.5 w-3.5" /> 最优参数 · {result.objective} = {result.best_score}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(result.best_params).map(([k, v]) => (
                    <span key={k} className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px]">{k}: {String(v)}</span>
                  ))}
                </div>
              </div>
            )}

            <div className="text-xs text-secondary">
              {result.n_completed}/{result.n_combinations} 组完成 · 耗时 {(result.elapsed_ms / 1000).toFixed(1)}s
            </div>

            {/* 排名表 */}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-secondary">
                    <th className="px-2 py-1.5 text-left">#</th>
                    <th className="px-2 py-1.5 text-left">参数</th>
                    <th className="px-2 py-1.5 text-right">{result.objective}</th>
                    <th className="px-2 py-1.5 text-right">夏普</th>
                    <th className="px-2 py-1.5 text-right">索提诺</th>
                    <th className="px-2 py-1.5 text-right">总收益</th>
                    <th className="px-2 py-1.5 text-right">最大回撤</th>
                    <th className="px-2 py-1.5 text-right">胜率</th>
                    <th className="px-2 py-1.5 text-right">交易数</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.slice(0, 50).map(r => (
                    <tr key={r.rank} className="border-b border-border/40 hover:bg-elevated/50">
                      <td className="px-2 py-1.5 text-secondary">{r.rank}</td>
                      <td className="px-2 py-1.5">
                        {r.error
                          ? <span className="text-red-400">失败: {r.error.slice(0, 40)}</span>
                          : <span className="text-foreground">{Object.entries(r.params).map(([k, v]) => `${k}=${v}`).join(', ')}</span>}
                      </td>
                      <td className="px-2 py-1.5 text-right font-medium">{r.objective_raw != null ? r.objective_raw.toFixed(3) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.sharpe ?? '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.sortino ?? '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.total_return != null ? fmtPct(r.stats.total_return) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.max_drawdown != null ? fmtPct(r.stats.max_drawdown) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.win_rate != null ? fmtPct(r.stats.win_rate) : '—'}</td>
                      <td className="px-2 py-1.5 text-right">{r.stats?.n_trades ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {result.results.length > 50 && (
                <div className="mt-2 text-center text-[11px] text-secondary">
                  仅显示前 50 组 · 共 {result.results.length} 组
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
