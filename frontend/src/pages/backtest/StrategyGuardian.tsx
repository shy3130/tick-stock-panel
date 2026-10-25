import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, GitMerge, Play, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react'
import { api, type GuardianCombinationSuggestion, type GuardianConfig, type GuardianProfile, type LabReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EmptyState } from '@/components/EmptyState'
import { INPUT_CLS, PRIMARY_OBJECTIVES, StrategySelect } from './components/paramSweep'

const STATUS_META: Record<GuardianProfile['status'], { label: string; cls: string }> = {
  pending: { label: '未复验', cls: 'bg-elevated text-secondary' },
  healthy: { label: '健康', cls: 'bg-emerald-500/15 text-emerald-400' },
  watch: { label: '观察', cls: 'bg-amber-500/15 text-amber-400' },
  retired: { label: '已退役', cls: 'bg-red-500/15 text-red-400' },
}

const BASELINE_KIND_LABEL: Record<string, string> = {
  mining_gate: '挖掘门槛 (OOS)',
  trailing_reference: '尾随参考',
}

function fmtDegradation(v: number | null | undefined): string {
  return v != null && Number.isFinite(v) ? `${(v * 100).toFixed(0)}%` : '—'
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  return v != null && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

function StatusBadge({ status }: { status: GuardianProfile['status'] }) {
  const meta = STATUS_META[status]
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${meta.cls}`}>
      {status === 'healthy' && <ShieldCheck className="mr-0.5 h-3 w-3" />}
      {meta.label}
    </span>
  )
}

/** 守护配置卡: 阈值改动即时 PATCH, 与后端 GuardianThresholds 同一约束。 */
function ConfigCard({ config }: { config: GuardianConfig }) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const patch = useMutation({
    mutationFn: (body: Parameters<typeof api.guardianPatchConfig>[0]) => api.guardianPatchConfig(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QK.guardianOverview }),
    onError: (e: Error) => setError(e.message),
  })
  const update = (body: Parameters<typeof api.guardianPatchConfig>[0]) => {
    setError(null)
    patch.mutate(body)
  }
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium">守护设置</div>
        <label className="flex cursor-pointer items-center gap-1.5 text-xs text-secondary">
          <input
            type="checkbox"
            checked={config.guardian_schedule_enabled}
            onChange={e => update({ guardian_schedule_enabled: e.target.checked })}
            className="h-3.5 w-3.5 accent-[var(--color-accent)]"
          />
          每日自动复验 (收盘数据更新后)
        </label>
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="text-[11px] text-secondary">
          观察阈值
          <input
            type="number" step="0.05" min="0.05" max="0.9"
            defaultValue={config.guardian_watch_pct}
            key={`w-${config.guardian_watch_pct}`}
            onBlur={e => update({ guardian_watch_pct: Number(e.target.value) })}
            className={`${INPUT_CLS} mt-0.5 w-24`}
          />
        </label>
        <label className="text-[11px] text-secondary">
          退役阈值
          <input
            type="number" step="0.05" min="0.1" max="1"
            defaultValue={config.guardian_retire_pct}
            key={`r-${config.guardian_retire_pct}`}
            onBlur={e => update({ guardian_retire_pct: Number(e.target.value) })}
            className={`${INPUT_CLS} mt-0.5 w-24`}
          />
        </label>
        <label className="text-[11px] text-secondary">
          验证段数上限 K
          <input
            type="number" step="1" min="2" max="4"
            defaultValue={config.guardian_k_max}
            key={`k-${config.guardian_k_max}`}
            onBlur={e => update({ guardian_k_max: Number(e.target.value) })}
            className={`${INPUT_CLS} mt-0.5 w-20`}
          />
        </label>
        <label className="text-[11px] text-secondary">
          挖掘策略名额上限
          <input
            type="number" step="1" min="0" max="50"
            defaultValue={config.lab_mined_slot_limit}
            key={`slot-${config.lab_mined_slot_limit}`}
            onBlur={e => update({ lab_mined_slot_limit: Number(e.target.value) })}
            className={`${INPUT_CLS} mt-0.5 w-24`}
          />
        </label>
        <div className="text-[10px] text-secondary">基线后退化为百分比; 段回测失败只降级到观察, 不会直接退役</div>
      </div>
      {error && <div className="mt-2 text-[11px] text-red-400">{error}</div>}
    </div>
  )
}

/** 手动入护: 任意策略 + 目标 (尾随参考基线, 首跑自动标定)。 */
function AdoptCard({ onChanged }: { onChanged: () => void }) {
  const { data: stratData } = useQuery({ queryKey: ['strategies'], queryFn: () => api.strategyList() })
  const strategies = stratData?.strategies ?? []
  const [strategyId, setStrategyId] = useState('')
  const [objective, setObjective] = useState('sharpe')
  const [error, setError] = useState<string | null>(null)
  const adopt = useMutation({
    mutationFn: () => api.guardianAdopt(strategyId, objective),
    onSuccess: () => { setError(null); onChanged() },
    onError: (e: Error) => setError(e.message),
  })
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="text-xs font-medium">加入守护</div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <div className="min-w-52 flex-1">
          <StrategySelect strategies={strategies} value={strategyId} onChange={setStrategyId} />
        </div>
        <select value={objective} onChange={e => setObjective(e.target.value)} className={`${INPUT_CLS} w-32`}>
          {PRIMARY_OBJECTIVES.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
        <button
          onClick={() => adopt.mutate()}
          disabled={!strategyId || adopt.isPending}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <ShieldCheck className="h-3.5 w-3.5" /> 入护并复验
        </button>
      </div>
      <div className="mt-1.5 text-[10px] text-secondary">
        手动入护用入护日前 252 个交易日的尾随表现作参考基线 (非样本外), 判定只针对此后新数据; 挖掘发布的策略自动入护 (门槛基线, 天然 OOS)。
      </div>
      {error && <div className="mt-2 text-[11px] text-red-400">{error}</div>}
    </div>
  )
}

/** P3 实验室战报: 挖掘/守护/名额聚合总览。 */
function LabReportCard({ report }: { report: LabReport | undefined }) {
  if (!report) return null
  const mining = report.mining.last_run
  const refinement = mining?.refinement as { applied?: boolean; refined_weights?: number[]; original_weights?: number[] } | null | undefined
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-medium">实验室战报</div>
        <div className="flex flex-wrap gap-2 text-[11px] text-secondary">
          <span>守护 {report.guardian.profiles} 个
            {report.guardian.by_status.healthy ? ` · 健康 ${report.guardian.by_status.healthy}` : ''}
            {report.guardian.by_status.watch ? ` · 观察 ${report.guardian.by_status.watch}` : ''}
            {report.guardian.by_status.retired ? ` · 退役 ${report.guardian.by_status.retired}` : ''}
          </span>
          {report.guardian.alerts > 0 && (
            <span className="inline-flex items-center gap-0.5 text-amber-400">
              <BellRing className="h-3 w-3" /> {report.guardian.alerts} 个连续观察告警
            </span>
          )}
          <span className={report.slots.full ? 'text-red-400' : ''}>
            挖掘名额 {report.slots.active_mined}/{report.slots.limit === 0 ? '∞' : report.slots.limit}
            {report.slots.full ? ' (已满, 新候选等位)' : ''}
          </span>
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-secondary">
        {mining
          ? `最近挖掘 ${mining.status} · ${mining.algorithm_version ?? '—'} · 候选 ${mining.candidate_count ?? '—'} 个 · 有效折 ${mining.valid_fold_count ?? '—'}`
          : '还没有挖掘运行'}
        {refinement?.applied === true && (
          <span className="text-emerald-400"> · 冠军权重已精调 {JSON.stringify(refinement.original_weights)} → {JSON.stringify(refinement.refined_weights)}</span>
        )}
      </div>
    </div>
  )
}

/** P2d 组合建议: 健康冠军低相关对, 确认后生成 composite 并入护。 */
function CombinationsCard({
  combinations,
  busy,
  onScan,
  onCreate,
}: {
  combinations: GuardianCombinationSuggestion[]
  generatedAt?: string | null
  busy: boolean
  onScan: () => void
  onCreate: (a: string, b: string) => Promise<unknown>
}) {
  const [creating, setCreating] = useState<string | null>(null)
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-center justify-between">
        <div className="inline-flex items-center gap-1.5 text-xs font-medium">
          <GitMerge className="h-3.5 w-3.5 text-secondary" /> 组合建议
        </div>
        <button
          onClick={onScan}
          disabled={busy}
          className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-40"
        >
          <RefreshCw className="h-3 w-3" /> 扫描相关性
        </button>
      </div>
      {combinations.length === 0 ? (
        <div className="mt-2 text-[11px] text-secondary">
          暂无建议 — 扫描会计算健康守护冠军间近 126 个交易日的日收益相关性, 相关性 ≤0.7 的对才会出现在这里。
        </div>
      ) : (
        <div className="mt-2 space-y-1">
          {combinations.map(item => {
            const key = `${item.a}+${item.b}`
            return (
              <div key={key} className="flex items-center justify-between gap-2 rounded-input bg-elevated/40 px-2 py-1.5 text-[11px]">
                <span className="min-w-0 truncate">
                  {item.a} <span className="text-secondary">×</span> {item.b}
                  <span className="ml-2 text-secondary">相关性 {item.corr.toFixed(2)} · {item.common_days} 日</span>
                </span>
                <button
                  onClick={() => {
                    if (window.confirm(`生成组合策略 ${item.a} + ${item.b}? (等权 composite, 自动加入守护)`)) {
                      setCreating(key)
                      // 成功或失败都要复位按钮状态 (错误展示走父组件 error)
                      Promise.resolve(onCreate(item.a, item.b))
                        .catch(() => {})
                        .finally(() => setCreating(null))
                    }
                  }}
                  disabled={busy || creating === key}
                  className="shrink-0 rounded-btn bg-accent px-2 py-1 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-40"
                >
                  {creating === key ? '生成中…' : '生成组合'}
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export function StrategyGuardian() {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const { data, isLoading, error: queryError } = useQuery({
    queryKey: QK.guardianOverview,
    queryFn: () => api.guardianOverview(),
    // 复验进行中每 2s 轮询; 平时按 TanStack 默认 (staleTime 到点后窗口聚焦刷新)
    refetchInterval: query => (query.state.data?.active_run ? 2000 : false),
  })
  const { data: labReport } = useQuery({
    queryKey: QK.labReport,
    queryFn: () => api.labReport(),
    refetchInterval: () => (data?.active_run ? 5000 : false),
  })
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: QK.guardianOverview })
    queryClient.invalidateQueries({ queryKey: QK.labReport })
  }
  const runAll = useMutation({
    mutationFn: () => api.guardianRunAll(),
    onSuccess: () => { setError(null); refresh() },
    onError: (e: Error) => setError(e.message),
  })
  const rerun = useMutation({
    mutationFn: (sid: string) => api.guardianRerun(sid),
    onSuccess: () => { setError(null); refresh() },
    onError: (e: Error) => setError(e.message),
  })
  const drop = useMutation({
    mutationFn: (sid: string) => api.guardianDrop(sid),
    onSuccess: () => { setError(null); refresh() },
    onError: (e: Error) => setError(e.message),
  })
  const scanCombinations = useMutation({
    mutationFn: () => api.guardianScanCombinations(),
    onSuccess: () => { setError(null); refresh() },
    onError: (e: Error) => setError(e.message),
  })
  const createCombination = useMutation({
    mutationFn: ({ a, b }: { a: string; b: string }) => api.guardianCreateCombination(a, b),
    onSuccess: () => { setError(null); refresh() },
    onError: (e: Error) => setError(e.message),
  })

  const items = data?.items ?? []
  const activeRun = data?.active_run ?? null
  const progress = activeRun?.progress

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-1">
      {(error || queryError) && (
        <div className="rounded-input border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
          {error ?? (queryError instanceof Error ? queryError.message : '加载失败')}
        </div>
      )}

      {activeRun && (
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="flex items-center justify-between text-xs">
            <span className="inline-flex items-center gap-1.5 text-secondary">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              复验进行中 {progress?.strategy_id ? `· ${progress.strategy_id}` : ''}
            </span>
            <span className="text-secondary">{progress?.done ?? 0}/{progress?.total ?? '?'}</span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-elevated">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${progress?.total ? ((progress.done ?? 0) / progress.total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      <LabReportCard report={labReport} />
      {data?.config && <ConfigCard config={data.config} />}
      <CombinationsCard
        combinations={data?.combinations?.suggestions ?? []}
        busy={!!activeRun || scanCombinations.isPending || createCombination.isPending}
        onScan={() => scanCombinations.mutate()}
        onCreate={(a, b) => createCombination.mutateAsync({ a, b })}
      />
      <AdoptCard onChanged={refresh} />

      <div className="flex items-center justify-between">
        <div className="text-xs font-medium">守护中的策略 ({items.length})</div>
        <button
          onClick={() => runAll.mutate()}
          disabled={runAll.isPending || !!activeRun}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Play className="h-3.5 w-3.5" /> 全部复验
        </button>
      </div>

      {isLoading ? (
        <div className="py-10 text-center text-xs text-secondary">加载中…</div>
      ) : items.length === 0 ? (
        <EmptyState
          title="还没有守护中的策略"
          hint="挖掘发布的策略会在发布时自动建立守护基线; 也可以在上方手动把任意策略加入守护。守护会在每天收盘数据更新后, 用基线之后的新数据自动复验, 退化时告警、严重时标记退役。"
        />
      ) : (
        <div className="overflow-x-auto rounded-card border border-border bg-surface">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-secondary">
                <th className="px-3 py-2 text-left">策略</th>
                <th className="px-2 py-2 text-left">状态</th>
                <th className="px-2 py-2 text-left">基线</th>
                <th className="px-2 py-2 text-left">最近复验</th>
                <th className="px-2 py-2 text-right">退化</th>
                <th className="px-2 py-2 text-left">说明</th>
                <th className="px-2 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map(p => {
                const last = p.last_check
                const lastText = last?.skipped
                  ? (last.reason ?? '跳过')
                  : last?.error
                    ? last.error
                    : last?.verdict
                      ? `${last.window_start ?? ''} ~ ${last.window_end ?? ''} · ${last.k ?? '—'} 段`
                      : '—'
                return (
                  <tr key={p.strategy_id} className="border-b border-border/40 align-top hover:bg-elevated/50">
                    <td className="px-3 py-2">
                      <div className="font-medium">{p.name ?? p.strategy_id}</div>
                      <div className="text-[10px] text-secondary">{p.strategy_id}</div>
                    </td>
                    <td className="px-2 py-2">
                      <div className="flex flex-col items-start gap-1">
                        <StatusBadge status={p.status} />
                        {p.alert && (
                          <span className="inline-flex items-center gap-0.5 text-[10px] text-amber-400">
                            <BellRing className="h-3 w-3" /> 连续 {p.consecutive_watch} 次观察
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-2 py-2 text-secondary">
                      <div>{BASELINE_KIND_LABEL[p.baseline.kind ?? ''] ?? '—'}</div>
                      <div className="text-[10px]">
                        {p.baseline.objective ?? '—'} {fmtNum(p.baseline.score)} · {p.baseline.n_trades ?? '—'} 笔 · 截至 {p.baseline.data_as_of ?? '—'}
                      </div>
                    </td>
                    <td className="px-2 py-2 text-secondary">
                      <div>{last?.checked_at ? new Date(last.checked_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</div>
                      <div className="max-w-56 truncate text-[10px]" title={lastText}>{lastText}</div>
                    </td>
                    <td className="px-2 py-2 text-right font-medium" style={{ color: last?.degradation_pct != null && last.degradation_pct > 0 ? '#f87171' : undefined }}>
                      {fmtDegradation(last?.degradation_pct)}
                    </td>
                    <td className="max-w-64 px-2 py-2 text-[10px] text-secondary">
                      {(last?.reasons?.length ? last.reasons : [p.status_reason]).filter(Boolean).join('; ') || '—'}
                    </td>
                    <td className="px-2 py-2">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => rerun.mutate(p.strategy_id)}
                          disabled={rerun.isPending || !!activeRun}
                          title="立即复验"
                          className="rounded-btn border border-border p-1.5 text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-40"
                        >
                          <RefreshCw className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => {
                            if (window.confirm(`移除 ${p.name ?? p.strategy_id} 的守护? 档案与复验历史将删除 (策略本身不受影响)。`))
                              drop.mutate(p.strategy_id)
                          }}
                          disabled={drop.isPending}
                          title="移除守护"
                          className="rounded-btn border border-border p-1.5 text-secondary hover:bg-red-500/10 hover:text-red-400 disabled:opacity-40"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
