import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Play, Square, TrendingDown, GitBranch, CheckCircle2 } from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { toast } from '@/components/Toast'
import {
  startWalkForward,
  stopWalkForward,
  clearWalkForward,
  tryReconnectWalkForward,
  useWalkForwardTask,
} from '@/lib/walkforwardTask'
import { buildDefaultOverrides, applyStrategyParams } from '@/lib/strategyOverrides'
import {
  INPUT_CLS,
  PRIMARY_OBJECTIVES,
  GRID_MAX_COMBINATIONS,
  useParamSweep,
  StrategySelect,
  SweepParamList,
  CombosHint,
} from './components/paramSweep'

const TODAY = new Date().toISOString().slice(0, 10)
const THREE_YEARS_AGO = new Date(Date.now() - 3 * 365 * 864e5).toISOString().slice(0, 10)

function Stat({ label, value, hint, color }: { label: string; value: string; hint?: string; color?: string }) {
  return (
    <div className="rounded-input border border-border bg-elevated/40 p-2.5">
      <div className="text-[11px] text-secondary">{label}</div>
      <div className="mt-0.5 text-sm font-semibold" style={color ? { color } : undefined}>{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-secondary">{hint}</div>}
    </div>
  )
}

/** 把 NaN/Infinity 归一为 null (走 ?! '—' 的既有降级路径), 防止 toFixed 渲染出 "NaN"。 */
function fin(v: number | null | undefined): number | null {
  return v != null && Number.isFinite(v) ? v : null
}

/** OOS 拼接净值曲线 (逐折复利) — walk-forward 核心产出的极简 SVG 折线。 */
function OosEquityChart({ curve }: { curve: { fold: number; date: string; value: number }[] }) {
  // 过滤非有限值: 零成交折等边界可能产出 NaN/Infinity, 混入会让坐标全 NaN → SVG 空白。
  const vals = curve.map(p => p.value).filter(v => Number.isFinite(v))
  if (!vals.length) return null
  const W = 600, H = 120, pad = 8
  const lo = Math.min(1, ...vals), hi = Math.max(1, ...vals)
  const span = hi - lo || 1
  // 起点补一个 value=1 基准, 让曲线从 1.0 起步
  const pts = [1, ...vals]
  const x = (i: number) => pad + (i / (pts.length - 1 || 1)) * (W - 2 * pad)
  const y = (v: number) => pad + (1 - (v - lo) / span) * (H - 2 * pad)
  const d = pts.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const last = vals[vals.length - 1]
  const up = last >= 1
  return (
    <div>
      <div className="mb-1 text-xs font-medium text-secondary">OOS 拼接净值 (逐折复利)</div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none" style={{ height: 120 }}>
        <line x1={pad} y1={y(1)} x2={W - pad} y2={y(1)} stroke="currentColor" strokeWidth="0.5" className="text-border" strokeDasharray="3 3" />
        <path d={d} fill="none" stroke={up ? '#34d399' : '#f87171'} strokeWidth="1.5" />
      </svg>
      <div className="mt-0.5 text-[10px] text-secondary">终值 {last.toFixed(4)} · {curve.length} 折</div>
    </div>
  )
}

export function StrategyWalkForward() {
  const task = useWalkForwardTask()
  const { data: stratData } = useQuery({ queryKey: ['strategies'], queryFn: () => api.strategyList() })
  const strategies: StrategyDetail[] = stratData?.strategies ?? []

  // 切策略: 有任务在跑时先真正取消 (关 SSE + 后端 cancel + 清 localStorage), 不能静默丢
  const sweep = useParamSweep(strategies, () => {
    if (task?.isPending) stopWalkForward()
    else clearWalkForward()
  })
  const [objective, setObjective] = useState('sortino')
  const [start, setStart] = useState(THREE_YEARS_AGO)
  const [end, setEnd] = useState(TODAY)
  const [mode, setMode] = useState<'position' | 'full'>('position')
  const [trainDays, setTrainDays] = useState('252')
  const [testDays, setTestDays] = useState('63')
  const [stepDays, setStepDays] = useState('63')
  const [applying, setApplying] = useState(false)

  // 折预览: 运行前实时显示折数/窗口切分 (不回测)
  const foldPreview = useQuery({
    queryKey: ['wf-preview', start, end, trainDays, testDays, stepDays],
    queryFn: () => api.walkforwardPreview(start, end, Number(trainDays), Number(testDays), Number(stepDays)),
    enabled: !!sweep.strategyId && start < end && Number(trainDays) > 0 && Number(testDays) > 0 && Number(stepDays) > 0,
    retry: false,
    staleTime: 30_000,
  })

  useEffect(() => {
    tryReconnectWalkForward()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const canRun = sweep.strategyId && sweep.combos > 0 && sweep.combos <= GRID_MAX_COMBINATIONS
    && !sweep.gridError
    && Number(trainDays) > 0 && Number(testDays) > 0 && Number(stepDays) > 0 && !task?.isPending

  const onRun = () => {
    if (!canRun) return
    clearWalkForward()
    startWalkForward({
      strategy_id: sweep.strategyId,
      param_grid: sweep.buildGrid(),
      objective,
      train_days: Number(trainDays),
      test_days: Number(testDays),
      step_days: Number(stepDays),
      // 未扫描参数固定为策略当前默认值; overrides 让 basic_filter/信号/风控按当前策略参与,
      // 否则 walk-forward 优化的策略与用户实际回测的不一致 (同 PR #82 优化器修复)。
      params: sweep.selected?.params_defaults,
      overrides: sweep.selected ? buildDefaultOverrides(sweep.selected) : undefined,
      start,
      end,
      mode,
    })
  }

  /** 采用最新折参数 → 策略配置 override 通道 (带来源折确认)。 */
  const onApplyLatestFold = async () => {
    const fold = result?.folds[result.folds.length - 1]
    if (!fold?.best_params || !sweep.selected || applying) return
    if (!window.confirm(`将第 ${fold.index + 1} 折训练参数应用到策略「${sweep.selected.name}」?\n来源折测试期: ${fold.test_start} ~ ${fold.test_end}\n(写入策略配置, 可在策略页查看/还原)`)) return
    setApplying(true)
    try {
      await applyStrategyParams(sweep.selected, fold.best_params)
      toast('已应用到策略配置 (策略页可还原)', 'success')
    } catch (e) {
      toast(`应用失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setApplying(false)
    }
  }

  const result = task?.result
  const progress = task?.progress
  const summary = result?.summary
  // 汇总里的可空数值先归一: 零成交折等边界可能产出 NaN/Infinity,
  // 直接 toFixed 会渲染出 "NaN"; 这里把非有限值转 null, 下游统一走 '—' 降级。
  const compounded = fin(summary?.compounded_oos_return)
  const degradation = fin(summary?.degradation)
  const avgIs = fin(summary?.avg_is_objective)
  const avgOos = fin(summary?.avg_oos_objective)

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[320px_1fr]">
      {/* ── 配置面板 ── */}
      <div className="space-y-3 rounded-card border border-border bg-surface p-4">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">策略</label>
          <StrategySelect strategies={strategies} value={sweep.strategyId} onChange={sweep.selectStrategy} />
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">优化目标</label>
          <select value={objective} onChange={e => setObjective(e.target.value)} className={INPUT_CLS}>
            {PRIMARY_OBJECTIVES.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
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

        {/* 滚动窗口 */}
        <div className="grid grid-cols-3 gap-1.5">
          <div>
            <label className="mb-1 block text-[11px] text-secondary">训练(天)</label>
            <input type="number" min={1} value={trainDays} onChange={e => setTrainDays(e.target.value)} className={INPUT_CLS} />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-secondary">测试(天)</label>
            <input type="number" min={1} value={testDays} onChange={e => setTestDays(e.target.value)} className={INPUT_CLS} />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-secondary">步进(天)</label>
            <input type="number" min={1} value={stepDays} onChange={e => setStepDays(e.target.value)} className={INPUT_CLS} />
          </div>
        </div>

        {foldPreview.data && foldPreview.data.folds.length > 0 && (
          <div className="rounded-input border border-border/60 bg-elevated/30 p-2">
            <div className="mb-1 text-[11px] font-medium text-secondary">折预览 (共 {foldPreview.data.n_folds} 折)</div>
            <div className="max-h-28 space-y-0.5 overflow-y-auto">
              {foldPreview.data.folds.map(f => (
                <div key={f.index} className="flex justify-between text-[10px] text-muted">
                  <span>#{f.index + 1} 训 {f.train_start}~{f.train_end}</span>
                  <span>测 {f.test_start}~{f.test_end}</span>
                </div>
              ))}
            </div>
            {(foldPreview.data.warnings ?? []).map(w => (
              <div key={w} className="mt-1 text-[10px] text-amber-400">{w}</div>
            ))}
          </div>
        )}
        {foldPreview.error && (
          <div className="text-[11px] text-red-400">折切分无效: {(foldPreview.error as Error)?.message ?? '请检查区间与窗口参数'}</div>
        )}

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">模式</label>
          <select value={mode} onChange={e => setMode(e.target.value as any)} className={INPUT_CLS}>
            <option value="position">组合仓位</option>
            <option value="full">全量独立</option>
          </select>
        </div>

        <SweepParamList params={sweep.params} sweeps={sweep.sweeps} updateSweep={sweep.updateSweep} hiddenLabels={sweep.hiddenParamLabels} />
        <CombosHint show={!!sweep.strategyId} combos={sweep.combos} gridError={sweep.gridError} />
        <div className="text-[11px] text-secondary">每折跑 {sweep.combos || 0} 组优化 × N 折，耗时较长</div>

        {task?.isPending ? (
          <button onClick={stopWalkForward} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-red-500/90 px-3 py-2 text-xs font-medium text-white hover:bg-red-500">
            <Square className="h-3.5 w-3.5" /> 停止
          </button>
        ) : (
          <button onClick={onRun} disabled={!canRun} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed">
            <Play className="h-3.5 w-3.5" /> 开始滚动样本外检验
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
              <span>第 {progress.done}/{progress.total} 折</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
              <div className="h-full bg-accent transition-all" style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </div>
        )}

        {!result && !task?.isPending && (
          <EmptyState
            title="滚动样本外检验"
            hint="每折在训练区间网格优化选最优参数，再在紧邻的测试区间做样本外(OOS)验证。样本内漂亮、样本外崩溃即过拟合。"
          />
        )}

        {result && result.n_folds === 0 && (
          <EmptyState title="未产生有效折"
            hint={`计划 ${result.n_planned_folds} 折, 但 ${result.n_skipped} 折因训练区间未优化出参数或 OOS 回测失败被跳过。请检查数据范围或放宽参数网格。`} />
        )}

        {result && summary && result.n_folds > 0 && (
          <div className="space-y-4">
            {/* 汇总卡 — 可空数值已在上游用 fin() 归一, NaN/Infinity 走 '—' 而非渲染 "NaN" */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="OOS 复利收益" value={fmtPct(compounded)}
                color={compounded != null && compounded >= 0 ? '#34d399' : '#f87171'} />
              <Stat label="IS→OOS 退化"
                value={degradation != null ? degradation.toFixed(3) : '—'}
                hint={degradation != null && degradation > 0 ? '样本外退化=过拟合' : '样本外未退化'}
                color={degradation != null && degradation > 0 ? '#f87171' : '#34d399'} />
              <Stat label="一致性" value={fmtPct(summary.consistency)} hint="OOS 盈利折占比" />
              <Stat label="有效折" value={result.n_skipped > 0 ? `${result.n_folds} (跳过${result.n_skipped})` : String(result.n_folds)} />
            </div>

            <div className="text-xs text-secondary">
              IS 目标均值 {avgIs != null ? avgIs.toFixed(3) : '—'} · OOS 目标均值 {avgOos != null ? avgOos.toFixed(3) : '—'} · 耗时 {(result.elapsed_ms / 1000).toFixed(1)}s
            </div>

            {/* OOS 拼接净值曲线 (walk-forward 核心产出) */}
            <OosEquityChart curve={summary.oos_equity_curve} />

            {/* 参数漂移轨迹: 各折最优参数逐参数序列 + 收敛徽章 */}
            <ParamDrift folds={result.folds} />

            {/* 采用最新折参数: 滚动检验的标准实践 — 最后一折训练参数上实盘 */}
            {result.folds.length > 0 && result.folds[result.folds.length - 1].best_params && (
              <div className="flex items-center justify-between gap-3 rounded-card border border-border/60 bg-elevated/30 p-3">
                <div className="min-w-0 text-[11px] text-secondary">
                  <div className="font-medium text-foreground">采用最新折参数</div>
                  <div className="mt-0.5 truncate">
                    第 {result.folds[result.folds.length - 1].index + 1} 折 (测试期 {result.folds[result.folds.length - 1].test_start} ~ {result.folds[result.folds.length - 1].test_end}) 训练所得:
                    {' '}{Object.entries(result.folds[result.folds.length - 1].best_params!).map(([k, v]) => `${k}=${String(v)}`).join(', ')}
                  </div>
                </div>
                <button
                  onClick={() => onApplyLatestFold()}
                  disabled={applying}
                  className="shrink-0 inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-40"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" /> {applying ? '应用中…' : '应用到策略'}
                </button>
              </div>
            )}

            {/* 跳过折透明展示: 无效折不进聚合但不静默消失 */}
            {result.skipped?.length > 0 && (
              <div className="rounded-card border border-border/60 p-3">
                <div className="mb-1.5 text-xs font-medium text-secondary">跳过的折 ({result.skipped.length})</div>
                <div className="max-h-32 space-y-0.5 overflow-y-auto">
                  {result.skipped.map((s, i) => (
                    <div key={i} className="flex justify-between gap-2 text-[10px] text-muted">
                      <span>#{s.index + 1} 测试 {s.test_start}~{s.test_end}</span>
                      <span className="text-amber-400/80">{s.reason}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 每折表 */}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-secondary">
                    <th className="px-2 py-1.5 text-left">折</th>
                    <th className="px-2 py-1.5 text-left">测试区间</th>
                    <th className="px-2 py-1.5 text-left">最优参数</th>
                    <th className="px-2 py-1.5 text-right">IS 目标</th>
                    <th className="px-2 py-1.5 text-right">OOS 目标</th>
                    <th className="px-2 py-1.5 text-right">OOS 收益</th>
                  </tr>
                </thead>
                <tbody>
                  {result.folds.map(f => {
                    const is = f.is_score
                    const oos = f.oos_objective
                    // 用后端方向感知的退化标志 (min 类目标 oos<is 未必是退化)
                    const degraded = f.oos_degraded === true
                    return (
                      <tr key={f.index} className="border-b border-border/40 hover:bg-elevated/50">
                        <td className="px-2 py-1.5 text-secondary">{f.index + 1}</td>
                        <td className="px-2 py-1.5 text-secondary">{f.test_start} ~ {f.test_end}</td>
                        <td className="px-2 py-1.5 text-foreground">
                          {f.best_params ? Object.entries(f.best_params).map(([k, v]) => `${k}=${v}`).join(', ') : '—'}
                        </td>
                        <td className="px-2 py-1.5 text-right">{is != null ? is.toFixed(3) : '—'}</td>
                        <td className="px-2 py-1.5 text-right" style={degraded ? { color: '#f87171' } : undefined}>
                          {oos != null ? oos.toFixed(3) : '—'}
                        </td>
                        <td className="px-2 py-1.5 text-right">
                          {f.oos_stats?.total_return != null ? fmtPct(f.oos_stats.total_return) : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {degradation != null && degradation > 0 && (
              <div className="flex items-center gap-1.5 rounded-input border border-red-500/30 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">
                <TrendingDown className="h-3.5 w-3.5" />
                样本外目标较样本内退化 {degradation.toFixed(3)}，提示参数可能过拟合训练区间。
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}


/** 参数漂移轨迹: 各折 best_params 逐参数序列 (如 ma_fast: 5→5→8→10) + 收敛徽章。
 *  与稳健优化的"各段冠军收敛度"同一诊断的滚动版: 各折最优值稳定 → 参数可信;
 *  逐折大幅摆动 → 策略对参数敏感, 任何单折"最优"都别全信。 */
function ParamDrift({ folds }: { folds: { index: number; best_params: Record<string, any> | null }[] }) {
  const withParams = folds.filter(f => f.best_params)
  if (withParams.length < 2) return null
  const keys = Object.keys(withParams[0].best_params!)
  if (!keys.length) return null
  return (
    <div className="rounded-card border border-border/60 p-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-secondary">
        <GitBranch className="h-3.5 w-3.5" /> 参数漂移轨迹 (逐折最优值)
      </div>
      <div className="space-y-1">
        {keys.map(k => {
          const seq = withParams.map(f => String(f.best_params![k]))
          const stable = seq.every(v => v === seq[0])
          return (
            <div key={k} className="flex items-center gap-2 text-[11px]">
              <span className="w-32 shrink-0 truncate text-muted" title={k}>{k}</span>
              <span className="min-w-0 flex-1 truncate text-foreground">{seq.join(' → ')}</span>
              <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] ${stable ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400' : 'border-amber-500/40 bg-amber-500/10 text-amber-400'}`}>
                {stable ? '稳定' : '漂移'}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
