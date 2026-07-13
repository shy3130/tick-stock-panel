import { useMemo, useState } from 'react'
import type { StrategyDetail, StrategyParamDef } from '@/lib/api'

/** 参数扫描配置的共享逻辑与 UI — 优化器与 walk-forward 复用。 */

export const INPUT_CLS =
  'w-full px-2.5 py-1.5 rounded-input bg-surface border border-border text-xs focus:outline-none focus:border-accent'

// 可选优化目标 (对齐后端 VALID_OBJECTIVES) + 中文标签
export const OBJECTIVES: { id: string; label: string }[] = [
  { id: 'sortino', label: '索提诺比率' },
  { id: 'sharpe', label: '夏普比率' },
  { id: 'calmar', label: 'Calmar 比率' },
  { id: 'total_return', label: '总收益' },
  { id: 'annual_return', label: '年化收益' },
  { id: 'win_rate', label: '胜率' },
  { id: 'profit_factor', label: '盈亏比' },
  { id: 'max_drawdown', label: '最大回撤(越小越好)' },
  { id: 'mc_maxdd_p95', label: '蒙卡回撤P95(越小越好)' },
  { id: 'avg_holding_days', label: '平均持仓天数' },
]

export const GRID_MAX_COMBINATIONS = 2000

export interface Sweep {
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

/** 某参数候选值个数 (与后端整数计数一致)。 */
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
  const nSteps = Math.round((hi - lo) / step)
  const last = lo + nSteps * step
  if (last > hi + 1e-9) return `${p.label}: 步长 ${step} 不整除区间, 末值 ${last.toFixed(4)} 超出 max ${hi}`
  return null
}

/** 管理策略选择 + 各参数扫描配置, 派生组合数 / 校验 / param_grid。 */
export function useParamSweep(strategies: StrategyDetail[], onStrategyChange?: () => void) {
  const [strategyId, setStrategyId] = useState<string>('')
  const [sweeps, setSweeps] = useState<Record<string, Sweep>>({})

  const selected = strategies.find(s => s.id === strategyId)
  const params = selected?.params ?? []

  const selectStrategy = (id: string) => {
    setStrategyId(id)
    onStrategyChange?.()
    const s = strategies.find(x => x.id === id)
    const init: Record<string, Sweep> = {}
    for (const p of s?.params ?? []) init[p.id] = defaultSweep(p)
    setSweeps(init)
  }

  const updateSweep = (pid: string, patch: Partial<Sweep>) =>
    setSweeps(prev => ({ ...prev, [pid]: { ...prev[pid], ...patch } }))

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

  return { strategyId, selected, selectStrategy, params, sweeps, updateSweep, combos, gridError, buildGrid }
}

/** 策略选择器。 */
export function StrategySelect({ strategies, value, onChange }: {
  strategies: StrategyDetail[]
  value: string
  onChange: (id: string) => void
}) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} className={INPUT_CLS}>
      <option value="">选择策略…</option>
      {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
    </select>
  )
}

/** 可扫参数列表 (勾选 + min/max/step)。 */
export function SweepParamList({ params, sweeps, updateSweep }: {
  params: StrategyParamDef[]
  sweeps: Record<string, Sweep>
  updateSweep: (pid: string, patch: Partial<Sweep>) => void
}) {
  if (!params.length) return null
  return (
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
  )
}

/** 组合数 / 校验提示 (含上限与网格错误告警)。 */
export function CombosHint({ show, combos, gridError }: { show: boolean; combos: number; gridError?: string | null }) {
  if (!show) return null
  const bad = combos > GRID_MAX_COMBINATIONS || !!gridError
  return (
    <div className={`text-xs ${bad ? 'text-red-400' : 'text-secondary'}`}>
      {gridError
        ? gridError
        : combos === 0
          ? '请至少勾选一个参数'
          : `共 ${combos} 组参数组合${combos > GRID_MAX_COMBINATIONS ? ` — 超过上限 ${GRID_MAX_COMBINATIONS}, 请增大 step 或缩小范围` : ''}`}
    </div>
  )
}
