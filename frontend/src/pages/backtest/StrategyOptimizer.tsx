import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Play, Square, Trophy, Mountain, GitBranch, CheckCircle2 } from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { toast } from '@/components/Toast'
import {
  startRobust,
  stopRobust,
  clearRobust,
  tryReconnectRobust,
  useRobustTask,
  type RobustResultRow,
} from '@/lib/robustTask'
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
const ONE_YEAR_AGO = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10)

/** 诊断徽章的中文标签 (与后端 robust_optimizer 的 label 一一对应)。 */
const CONVERGENCE_LABEL: Record<string, { text: string; cls: string }> = {
  unanimous: { text: '各段冠军一致', cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400' },
  majority: { text: '冠军多数收敛', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-400' },
  scattered: { text: '冠军散落 · 参数不稳', cls: 'border-red-500/40 bg-red-500/10 text-red-400' },
  no_data: { text: '无有效段', cls: 'border-border bg-elevated text-secondary' },
}

const PLATEAU_LABEL: Record<string, { text: string; cls: string }> = {
  plateau: { text: '参数高原 · 选择不敏感', cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400' },
  slope: { text: '缓坡', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-400' },
  peak: { text: '孤峰 · 过拟合警报', cls: 'border-red-500/40 bg-red-500/10 text-red-400' },
  isolated: { text: '邻居无效', cls: 'border-border bg-elevated text-secondary' },
}

function Badge({ icon, text, cls }: { icon?: React.ReactNode; text: string; cls: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-normal ${cls}`}>
      {icon}{text}
    </span>
  )
}

/** 数值兜底: 非有限值渲染 '—' (零成交/缺段可能产出 NaN/Infinity)。 */
function num(v: number | null | undefined, digits = 3): string {
  return v != null && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

export function StrategyOptimizer() {
  const task = useRobustTask()
  const { data: stratData } = useQuery({ queryKey: ['strategies'], queryFn: () => api.strategyList() })
  const strategies: StrategyDetail[] = stratData?.strategies ?? []

  // 切策略: 有任务在跑时先真正取消 (关 SSE + 后端 cancel + 清 localStorage), 不能静默丢
  const sweep = useParamSweep(strategies, () => {
    if (task?.isPending) stopRobust()
    else clearRobust()
  })
  const [objective, setObjective] = useState('sortino')
  const [start, setStart] = useState(ONE_YEAR_AGO)
  const [end, setEnd] = useState(TODAY)
  const [mode, setMode] = useState<'position' | 'full'>('position')
  const [kSegments, setKSegments] = useState(4)
  const [minTrades, setMinTrades] = useState(30)
  const [applying, setApplying] = useState(false)

  // 刷新/切页后: 恢复未完成的稳健优化任务
  useEffect(() => {
    tryReconnectRobust()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 折预览: 运行前实时显示验证窗切分 (不回测)
  const preview = useQuery({
    queryKey: ['robust-preview', start, end, kSegments],
    queryFn: () => api.robustOptimizePreview(start, end, kSegments),
    enabled: !!sweep.strategyId && start < end,
    retry: false,
    staleTime: 30_000,
  })
  const windows = preview.data?.windows
  const previewWarnings = preview.data?.warnings ?? []

  const canRun = sweep.strategyId && sweep.combos > 0 && sweep.combos <= GRID_MAX_COMBINATIONS
    && !sweep.gridError && !task?.isPending

  const onRun = () => {
    if (!canRun) return
    clearRobust()
    startRobust({
      strategy_id: sweep.strategyId,
      param_grid: sweep.buildGrid(),
      objective,
      k_segments: kSegments,
      min_trades_total: minTrades,
      // 未扫描参数固定为策略当前默认值; overrides 让 basic_filter/信号/风控按当前策略参与,
      // 保证优化的就是用户实际回测的策略 (而非被剥离配置的裸策略)。
      params: sweep.selected?.params_defaults,
      overrides: sweep.selected ? buildDefaultOverrides(sweep.selected) : undefined,
      start,
      end,
      mode,
    })
  }

  /** 应用参数 → 策略配置 override 通道 (可逆, 策略页可还原)。 */
  const onApply = async (params: Record<string, any>, sourceNote: string) => {
    if (!sweep.selected || applying) return
    if (!window.confirm(`将参数应用到策略「${sweep.selected.name}」?\n${sourceNote}\n(写入策略配置, 可在策略页查看/还原)`)) return
    setApplying(true)
    try {
      await applyStrategyParams(sweep.selected, params)
      toast('已应用到策略配置 (策略页可还原)', 'success')
    } catch (e) {
      toast(`应用失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setApplying(false)
    }
  }

  const result = task?.result
  const progress = task?.progress
  const bestRow = result?.results.find(r => r.rank === 1 && !r.error && !r.disqualified)
  const conv = result?.convergence
  const plateau = result?.plateau

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[320px_minmax(0,1fr)] h-full min-h-0 overflow-hidden">
      {/* ── 配置面板 ── */}
      <div className="space-y-3 rounded-card border border-border bg-surface p-4 overflow-y-auto min-h-0">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">策略</label>
          <StrategySelect strategies={strategies} value={sweep.strategyId} onChange={sweep.selectStrategy} />
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">优化目标 (白名单)</label>
          <select value={objective} onChange={e => setObjective(e.target.value)} className={INPUT_CLS}>
            {PRIMARY_OBJECTIVES.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
          <div className="mt-1 text-[10px] text-muted">胜率/持仓天数等退化目标已排除 (交易少时可被运气推高)</div>
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

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">验证段数 K</label>
            <select value={kSegments} onChange={e => setKSegments(Number(e.target.value))} className={INPUT_CLS}>
              {[2, 3, 4, 5, 6].map(k => <option key={k} value={k}>{k} 段</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-secondary">最小交易数 (合计)</label>
            <input type="number" min={0} value={minTrades} onChange={e => setMinTrades(Number(e.target.value))} className={INPUT_CLS} />
          </div>
        </div>

        {windows && windows.length > 0 && (
          <div className="rounded-input border border-border/60 bg-elevated/30 p-2">
            <div className="mb-1 text-[11px] font-medium text-secondary">验证窗预览 ({windows.length} 段)</div>
            <div className="space-y-0.5">
              {windows.map(w => (
                <div key={w.start} className="flex justify-between text-[10px] text-muted">
                  <span>{w.start} ~ {w.end}</span>
                  <span>{w.days} 天</span>
                </div>
              ))}
            </div>
            {previewWarnings.map(w => (
              <div key={w} className="mt-1 text-[10px] text-amber-400">{w}</div>
            ))}
          </div>
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
        <div className="text-[11px] text-secondary">每组跑 K 段验证 + Top-20 全窗细化, 耗时约为旧版 1.7 倍</div>

        {task?.isPending ? (
          <button onClick={stopRobust} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-red-500/90 px-3 py-2 text-xs font-medium text-white hover:bg-red-500">
            <Square className="h-3.5 w-3.5" /> 停止
          </button>
        ) : (
          <button onClick={onRun} disabled={!canRun} className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed">
            <Play className="h-3.5 w-3.5" /> 开始稳健优化
          </button>
        )}
      </div>

      {/* ── 结果面板 ── */}
      <div className="min-h-0 rounded-card border border-border bg-surface p-4 overflow-y-auto">
        {task?.error && (
          <div className="mb-3 rounded-input border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">{task.error}</div>
        )}

        {task?.isPending && progress && (
          <div className="mb-4">
            <div className="mb-1 flex justify-between text-xs text-secondary">
              <span>
                {progress.type === 'robust_optimizer_prepare' ? '准备共享矩阵'
                  : progress.type === 'robust_optimizer_stage2' ? `阶段二全窗细化 ${progress.done}/${progress.total}`
                  : `阶段一 ${progress.done}/${progress.total}`}
              </span>
              <span>当前最优: {progress.best_score != null ? progress.best_score.toFixed(3) : '—'}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
              <div className="h-full bg-accent transition-all" style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </div>
        )}

        {!result && !task?.isPending && (
          <EmptyState title="稳健优化" hint="每组参数在 K 个互不重叠验证段上独立评估, 按 R = 0.6×中位 + 0.4×最差 段稳健分排名; 只有在多段行情上都好的参数才能当冠军。" />
        )}

        {result && (
          <div className="space-y-4">
            {/* ── 冠军卡 ── */}
            {bestRow && result.best_params && (
              <div className="rounded-card border border-accent/30 bg-accent/5 p-3">
                <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs font-semibold text-accent">
                  <Trophy className="h-3.5 w-3.5" /> 稳健冠军 · {result.objective} 稳健分 {num(result.best_score)}
                  {conv && <Badge icon={<GitBranch className="h-3 w-3" />} text={CONVERGENCE_LABEL[conv.label]?.text ?? conv.label} cls={CONVERGENCE_LABEL[conv.label]?.cls ?? ''} />}
                  {plateau && <Badge icon={<Mountain className="h-3 w-3" />} text={PLATEAU_LABEL[plateau.label]?.text ?? plateau.label} cls={PLATEAU_LABEL[plateau.label]?.cls ?? ''} />}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(result.best_params).map(([k, v]) => (
                    <span key={k} className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px]">{k}: {String(v)}</span>
                  ))}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-secondary sm:grid-cols-4">
                  <span>中位 {num(bestRow.median)}</span>
                  <span>最差段 {num(bestRow.worst)}</span>
                  <span className={bestRow.spread != null && bestRow.spread > 5 ? 'text-amber-400' : ''}>段间离散 {num(bestRow.spread)}</span>
                  <span>合计交易 {bestRow.trades_total ?? '—'}</span>
                </div>
                {bestRow.is_full?.objective_raw != null && (
                  <div className={`mt-2 text-[11px] ${bestRow.degradation != null && bestRow.degradation > 0 ? 'text-amber-400' : 'text-secondary'}`}>
                    全窗样本内 {num(bestRow.is_full.objective_raw)} → 稳健分 {num(result.best_score)}
                    {bestRow.degradation != null && ` · 乐观偏差 ${num(bestRow.degradation)}${bestRow.degradation_pct != null ? ` (${fmtPct(bestRow.degradation_pct)})` : ''}`}
                  </div>
                )}
                <button
                  onClick={() => onApply(result.best_params!, '来源: 稳健优化冠军 (含退化/高原判据)')}
                  disabled={applying}
                  className="mt-2.5 inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-40"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" /> {applying ? '应用中…' : '应用参数到策略'}
                </button>
              </div>
            )}

            {/* ── 各段冠军 (收敛度明细) ── */}
            {conv && conv.champions.length > 0 && (
              <div className="rounded-card border border-border/60 p-3">
                <div className="mb-1.5 text-xs font-medium text-secondary">各段冠军 (收敛度诊断)</div>
                <div className="space-y-1">
                  {conv.champions.map(c => (
                    <div key={c.segment} className="flex items-center justify-between gap-2 text-[11px]">
                      <span className="text-muted">V{c.segment + 1}</span>
                      <span className="min-w-0 flex-1 truncate text-foreground">{Object.entries(c.params).map(([k, v]) => `${k}=${String(v)}`).join(', ')}</span>
                      <span className="text-secondary">{num(c.objective_raw)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* ── 高原邻域 ── */}
            {plateau && plateau.neighbors.length > 0 && (
              <div className="rounded-card border border-border/60 p-3">
                <div className="mb-1.5 text-xs font-medium text-secondary">
                  冠军邻域 (得分衰减 ≤ {plateau.tolerance != null ? plateau.tolerance.toFixed(3) : '—'} 视为在高原上)
                </div>
                <div className="space-y-1">
                  {plateau.neighbors.map((n, i) => (
                    <div key={i} className="flex items-center justify-between gap-2 text-[11px]">
                      <span className="min-w-0 flex-1 truncate text-foreground">{Object.entries(n.params).map(([k, v]) => `${k}=${String(v)}`).join(', ')}</span>
                      <span className={n.decay == null ? 'text-muted' : n.decay <= (plateau.tolerance ?? 0) ? 'text-emerald-400' : 'text-red-400'}>
                        {n.decay != null ? `衰减 ${n.decay.toFixed(3)}` : '无效 (错误/除名)'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="text-xs text-secondary">
              {result.n_completed}/{result.n_combinations} 组完成 · K={result.k_segments} 段 · 阶段二细化 {result.stage2.evaluated} 组 · 耗时 {(result.elapsed_ms / 1000).toFixed(1)}s
              {result.cancelled && ' · 已取消 (部分结果)'}
            </div>

            {/* ── 排名表 ── */}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-secondary">
                    <th className="px-2 py-1.5 text-left">#</th>
                    <th className="px-2 py-1.5 text-left">参数</th>
                    <th className="px-2 py-1.5 text-right">稳健分</th>
                    <th className="px-2 py-1.5 text-right">中位</th>
                    <th className="px-2 py-1.5 text-right">最差</th>
                    <th className="px-2 py-1.5 text-right">离散</th>
                    {Array.from({ length: result.k_segments }, (_, i) => (
                      <th key={i} className="px-2 py-1.5 text-right">V{i + 1}</th>
                    ))}
                    <th className="px-2 py-1.5 text-right">全窗IS</th>
                    <th className="px-2 py-1.5 text-right">退化</th>
                    <th className="px-2 py-1.5 text-right">交易数</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.slice(0, 50).map(r => (
                    <ResultRow key={r.rank} row={r} k={result.k_segments} />
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

function ResultRow({ row, k }: { row: RobustResultRow; k: number }) {
  if (row.error) {
    return (
      <tr className="border-b border-border/40">
        <td className="px-2 py-1.5 text-secondary">{row.rank}</td>
        <td className="px-2 py-1.5" colSpan={k + 7}>
          <span className="text-foreground">{Object.entries(row.params).map(([kk, v]) => `${kk}=${v}`).join(', ')}</span>
          <span className="ml-2 text-red-400">失败: {row.error.slice(0, 50)}</span>
        </td>
      </tr>
    )
  }
  const dq = !!row.disqualified
  return (
    <tr className={`border-b border-border/40 hover:bg-elevated/50 ${dq ? 'opacity-60' : ''}`}>
      <td className="px-2 py-1.5 text-secondary">{row.rank}</td>
      <td className="px-2 py-1.5">
        <span className="text-foreground">{Object.entries(row.params).map(([kk, v]) => `${kk}=${v}`).join(', ')}</span>
        {dq && <span className="ml-2 text-amber-400" title={row.disqualify_reason ?? ''}>除名: {row.disqualify_reason}</span>}
      </td>
      <td className="px-2 py-1.5 text-right font-medium">{num(row.score)}</td>
      <td className="px-2 py-1.5 text-right">{num(row.median)}</td>
      <td className="px-2 py-1.5 text-right">{num(row.worst)}</td>
      <td className="px-2 py-1.5 text-right">{num(row.spread)}</td>
      {Array.from({ length: k }, (_, i) => (
        <td key={i} className="px-2 py-1.5 text-right text-secondary">
          {row.segments[i]?.objective_raw != null ? num(row.segments[i].objective_raw) : '—'}
        </td>
      ))}
      <td className="px-2 py-1.5 text-right">{row.is_full?.objective_raw != null ? num(row.is_full.objective_raw) : '—'}</td>
      <td className={`px-2 py-1.5 text-right ${row.degradation != null && row.degradation > 0 ? 'text-amber-400' : ''}`}>
        {row.degradation_pct != null ? fmtPct(row.degradation_pct) : '—'}
      </td>
      <td className="px-2 py-1.5 text-right">{row.trades_total ?? '—'}</td>
    </tr>
  )
}
