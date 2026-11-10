import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, BookmarkCheck, CheckCircle2, Clock3, Link2, Loader2, RotateCcw, Trash2, X, XCircle } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type ResearchCandidate, type ResearchCandidateStatus, type ScoringDirection } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { selectableStrategies } from '@/lib/strategyLibrary'
import { StrategyOptions } from '@/components/StrategyOptions'

const STATUS_OPTIONS: { value: ResearchCandidateStatus; label: string; icon: typeof Clock3 }[] = [
  { value: 'pending', label: '待验证', icon: Clock3 },
  { value: 'validated', label: '已验证', icon: CheckCircle2 },
  { value: 'rejected', label: '已排除', icon: XCircle },
]

type LinkDraft = {
  candidateId: string
  strategyId: string
  direction: ScoringDirection
  weight: number
}

function metricSummary(item: ResearchCandidate) {
  const metric = (key: string) => {
    const value = item.metrics[key]
    return typeof value === 'number' ? value : null
  }
  if (item.kind === 'factor') {
    const ic = metric('ic_mean')
    const ir = metric('ir')
    return [
      ic == null ? null : `IC ${fmtPct(ic)}`,
      ir == null ? null : `IR ${ir.toFixed(2)}`,
    ].filter(Boolean).join(' · ') || '暂无指标摘要'
  }
  const totalReturn = metric('total_return')
  const sharpe = metric('sharpe')
  return [
    totalReturn == null ? null : `收益 ${fmtPct(totalReturn)}`,
    sharpe == null ? null : `夏普 ${sharpe.toFixed(2)}`,
  ].filter(Boolean).join(' · ') || '暂无指标摘要'
}

export function ResearchCandidatesDialog({ onClose, onLoadStrategy }: {
  onClose: () => void
  /** 策略候选「载入复测」: 把保存的 config 回填到回测表单 (由回测页接线) */
  onLoadStrategy?: (candidate: ResearchCandidate) => void
}) {
  const queryClient = useQueryClient()
  const [kind, setKind] = useState<'all' | 'factor' | 'strategy'>('all')
  const [linkDraft, setLinkDraft] = useState<LinkDraft | null>(null)
  const candidates = useQuery({ queryKey: QK.researchCandidates, queryFn: api.researchCandidates })
  const strategies = useQuery({ queryKey: QK.strategyLinkOptions(), queryFn: () => api.strategyList() })
  const factorColumns = useQuery({ queryKey: QK.factorColumns, queryFn: api.factorColumns })
  const supportedFactors = useMemo(
    () => new Set((factorColumns.data?.columns ?? []).map(item => item.id)),
    [factorColumns.data],
  )
  const visible = useMemo(() => {
    const items = candidates.data?.items ?? []
    return kind === 'all' ? items : items.filter(item => item.kind === kind)
  }, [candidates.data, kind])

  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: ResearchCandidateStatus }) =>
      api.researchCandidateUpdate(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QK.researchCandidates }),
    onError: error => toast(`更新失败 · ${String((error as Error).message || error)}`, 'error'),
  })
  const remove = useMutation({
    mutationFn: api.researchCandidateDelete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.researchCandidates })
      toast('候选方案已删除', 'success')
    },
    onError: error => toast(`删除失败 · ${String((error as Error).message || error)}`, 'error'),
  })
  const applyFactor = useMutation({
    mutationFn: async ({ candidate, draft }: { candidate: ResearchCandidate; draft: LinkDraft }) => {
      const detail = await api.strategyGet(draft.strategyId)
      const factorName = candidate.source_id
      const targetWeight = Math.min(100, Math.max(1, draft.weight)) / 100
      const otherEntries = Object.entries(detail.scoring)
        .filter(([name, weight]) => name !== factorName && Number(weight) > 0)
      const otherTotal = otherEntries.reduce((sum, [, weight]) => sum + Number(weight), 0)
      const scoring = otherTotal > 0
        ? Object.fromEntries([
            ...otherEntries.map(([name, weight]) => [name, +(Number(weight) / otherTotal * (1 - targetWeight)).toFixed(6)]),
            [factorName, targetWeight],
          ])
        : { [factorName]: 1 }
      await api.strategyPatchConfig(draft.strategyId, {
        scoring,
        scoring_directions: {
          ...(detail.scoring_directions ?? {}),
          [factorName]: draft.direction,
        },
        scoring_replace: true,
      })
      return { strategyId: draft.strategyId, strategyName: detail.name }
    },
    onSuccess: result => {
      queryClient.invalidateQueries({ queryKey: QK.strategyDetail(result.strategyId) })
      queryClient.invalidateQueries({ queryKey: ['screener-strategies'] })
      queryClient.invalidateQueries({ queryKey: QK.strategyLinkOptions() })
      setLinkDraft(null)
      toast(`已加入“${result.strategyName}”评分方案`, 'success')
    },
    onError: error => toast(`加入失败 · ${String((error as Error).message || error)}`, 'error'),
  })

  const compatibleStrategies = (item: ResearchCandidate) => {
    const assetType = typeof item.config.asset_type === 'string' ? item.config.asset_type : null
    return selectableStrategies(strategies.data?.strategies ?? [],
      linkDraft?.candidateId === item.id ? [linkDraft.strategyId] : []).filter(strategy => (
      strategy.execution_backend !== 'composite'
      && (!assetType || strategy.asset_types.includes(assetType))
      && strategy.timeframes.includes('1d')
    ))
  }
  const startLink = (item: ResearchCandidate) => {
    const options = compatibleStrategies(item)
    const ic = typeof item.metrics.ic_mean === 'number' ? item.metrics.ic_mean : null
    setLinkDraft({
      candidateId: item.id,
      strategyId: options[0]?.id ?? '',
      direction: ic != null && ic < 0 ? 'low' : 'high',
      weight: 20,
    })
  }

  return (
    <Modal
      onClose={onClose}
      labelledBy="research-candidates-title"
      panelClassName="flex max-h-[82vh] w-[94vw] max-w-4xl flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
    >
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
        <BookmarkCheck className="h-4 w-4 text-accent" />
        <div className="min-w-0">
          <h2 id="research-candidates-title" className="text-sm font-semibold text-foreground">候选方案</h2>
          <div className="mt-0.5 text-[11px] text-muted">仅保存研究定义、配置与指标摘要</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto flex h-8 w-8 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground"
          title="关闭"
          aria-label="关闭候选方案"
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      <div className="flex shrink-0 items-center gap-1 border-b border-border bg-surface/50 px-4 py-2">
        {([['all', '全部'], ['factor', '因子'], ['strategy', '策略']] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setKind(value)}
            aria-pressed={kind === value}
            className={`rounded-btn px-3 py-1.5 text-xs font-medium transition-colors ${kind === value
              ? 'bg-accent/15 text-accent'
              : 'text-secondary hover:bg-elevated hover:text-foreground'
            }`}
          >
            {label}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-muted">{visible.length} 个</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {candidates.isLoading && (
          <div className="px-4 py-10 text-center text-sm text-muted">加载中…</div>
        )}
        {candidates.isError && (
          <div className="m-4 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {String((candidates.error as Error).message)}
          </div>
        )}
        {!candidates.isLoading && !candidates.isError && visible.length === 0 && (
          <div className="px-4 py-12 text-center">
            <BookmarkCheck className="mx-auto h-7 w-7 text-muted/50" />
            <div className="mt-3 text-sm font-medium text-secondary">暂无候选方案</div>
          </div>
        )}
        {visible.map(item => {
          const linking = linkDraft?.candidateId === item.id
          const options = compatibleStrategies(item)
          const factorSupported = supportedFactors.has(item.source_id)
          const canLink = item.kind === 'factor' && item.status === 'validated' && factorSupported
          const ic = typeof item.metrics.ic_mean === 'number' ? item.metrics.ic_mean : null
          return (
          <div key={item.id} className="border-b border-border/70 last:border-b-0">
            <div className="grid grid-cols-1 gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_8rem_7rem] md:items-center">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-medium ${item.kind === 'factor'
                    ? 'border-cyan-400/30 bg-cyan-400/10 text-cyan-400'
                    : 'border-amber-400/30 bg-amber-400/10 text-amber-400'
                  }`}>
                    {item.kind === 'factor' ? '因子' : '策略'}
                  </span>
                  <span className="truncate text-sm font-medium text-foreground">{item.name}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
                  <span>{metricSummary(item)}</span>
                  {item.data_as_of && <span>数据截至 {item.data_as_of}</span>}
                  {item.kind === 'factor' && !factorSupported && !factorColumns.isLoading && (
                    <span className="text-danger">当前因子目录不支持</span>
                  )}
                </div>
              </div>
              <select
                value={item.status}
                onChange={event => update.mutate({ id: item.id, status: event.target.value as ResearchCandidateStatus })}
                disabled={update.isPending}
                aria-label={`更新 ${item.name} 的状态`}
                className="h-8 rounded-input border border-border bg-surface px-2 text-xs text-secondary focus:border-accent focus:outline-none disabled:opacity-50"
              >
                {STATUS_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <div className="flex items-center justify-end gap-1">
                {item.kind === 'strategy' && onLoadStrategy && (
                  <button
                    type="button"
                    onClick={() => onLoadStrategy(item)}
                    className="inline-flex h-8 items-center gap-1.5 rounded-btn px-2 text-[11px] text-accent transition-colors hover:bg-accent/10"
                    title="把此候选保存的回测配置 (策略/区间/参数/费率/仓位/环境过滤) 回填到回测表单, 可直接复测"
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                    载入复测
                  </button>
                )}
                {item.kind === 'factor' && (
                  <button
                    type="button"
                    onClick={() => linking ? setLinkDraft(null) : startLink(item)}
                    disabled={!canLink || strategies.isLoading || factorColumns.isLoading}
                    className="inline-flex h-8 items-center gap-1.5 rounded-btn px-2 text-[11px] text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:text-muted disabled:opacity-50"
                    title={item.status !== 'validated' ? '验证通过后可加入策略' : factorSupported ? '加入策略评分' : '当前因子不可用于策略评分'}
                  >
                    <Link2 className="h-3.5 w-3.5" />
                    加入策略
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm(`确认删除候选方案“${item.name}”？`)) remove.mutate(item.id)
                  }}
                  disabled={remove.isPending}
                  className="flex h-8 w-8 items-center justify-center rounded-btn text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                  title="删除候选"
                  aria-label={`删除候选 ${item.name}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            {linking && linkDraft && (
              <div className="grid gap-3 border-t border-border/60 bg-surface/45 px-4 py-3 md:grid-cols-[minmax(12rem,1fr)_9rem_8rem_auto] md:items-end">
                <label className="block min-w-0">
                  <span className="mb-1 block text-[10px] text-muted">目标策略</span>
                  <select
                    value={linkDraft.strategyId}
                    onChange={event => setLinkDraft(current => current ? { ...current, strategyId: event.target.value } : current)}
                    aria-label="目标策略"
                    className="h-8 w-full rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none"
                  >
                    {options.length === 0 && <option value="">没有兼容策略</option>}
                    <StrategyOptions strategies={options} selected={linkDraft ? [linkDraft.strategyId] : []} />
                  </select>
                </label>
                <div>
                  <span className="mb-1 block text-[10px] text-muted">评分方向</span>
                  <div className="grid h-8 grid-cols-2 overflow-hidden rounded-input border border-border bg-base">
                    {([['high', ArrowUp, '高值'], ['low', ArrowDown, '低值']] as const).map(([value, Icon, label]) => (
                      <button
                        key={value}
                        type="button"
                        onClick={() => setLinkDraft(current => current ? { ...current, direction: value } : current)}
                        className={`flex items-center justify-center gap-1 text-[11px] transition-colors ${linkDraft.direction === value
                          ? value === 'high' ? 'bg-emerald-400/15 text-emerald-400' : 'bg-cyan-400/15 text-cyan-400'
                          : 'text-muted hover:bg-elevated'
                        }`}
                        title={`偏好${label}`}
                        aria-label={`评分方向：偏好${label}`}
                        aria-pressed={linkDraft.direction === value}
                      >
                        <Icon className="h-3 w-3" />
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                <label className="block">
                  <span className="mb-1 flex items-center justify-between text-[10px] text-muted">
                    初始权重
                    {ic != null && <span>{ic < 0 ? 'IC<0 推荐低值' : 'IC≥0 推荐高值'}</span>}
                  </span>
                  <div className="relative">
                    <input
                      type="number"
                      min={1}
                      max={100}
                      step={1}
                      value={linkDraft.weight}
                      aria-label="因子初始权重百分比"
                      onChange={event => setLinkDraft(current => current ? {
                        ...current,
                        weight: Math.min(100, Math.max(1, Number(event.target.value) || 1)),
                      } : current)}
                      className="h-8 w-full rounded-input border border-border bg-base px-2 pr-7 text-xs text-foreground focus:border-accent focus:outline-none"
                    />
                    <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted">%</span>
                  </div>
                </label>
                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setLinkDraft(null)}
                    className="h-8 rounded-btn px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={() => applyFactor.mutate({ candidate: item, draft: linkDraft })}
                    disabled={!linkDraft.strategyId || applyFactor.isPending}
                    className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {applyFactor.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Link2 className="h-3.5 w-3.5" />}
                    应用
                  </button>
                </div>
              </div>
            )}
          </div>
          )
        })}
      </div>
    </Modal>
  )
}
