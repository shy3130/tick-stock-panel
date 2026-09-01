import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, Loader2, MessagesSquare, RotateCcw, Square } from 'lucide-react'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { api, type PositionAnalysisRequest } from '@/lib/api'
import { cn } from '@/lib/cn'

const OPENAI_COMPAT_PROVIDERS = ['openai_compat'] as const

type RunStatus = 'idle' | 'running' | 'done' | 'error' | 'stopped'

interface Props {
  snapshotReady: boolean
  fholdAvailable: boolean
  positionCount: number
}

export function PositionAnalysisAgentPanel({ snapshotReady, fholdAvailable, positionCount }: Props) {
  const [profileId, setProfileId] = useState('')
  const [publicResearchEnabled, setPublicResearchEnabled] = useState(false)
  const [status, setStatus] = useState<RunStatus>('idle')
  const [content, setContent] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [elapsedMs, setElapsedMs] = useState<number | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const statusRef = useRef<RunStatus>('idle')
  const generationRef = useRef(0)

  const publicResearchHealth = useQuery({
    queryKey: ['positionAnalysisPublicResearchHealth'],
    queryFn: api.positionAnalysisPublicResearchHealth,
    enabled: publicResearchEnabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const blockedReason = !snapshotReady
    ? '组合快照不可用，无法确认 fhold 持仓。'
    : !fholdAvailable
      ? 'fhold 券商持仓不可用，无法运行全仓研究。'
      : positionCount === 0
        ? '当前无持仓，无法运行全仓研究。'
        : null
  const canStart = blockedReason === null && status !== 'running'

  useEffect(() => {
    return () => {
      statusRef.current = 'stopped'
      generationRef.current += 1
      abortRef.current?.abort()
      abortRef.current = null
    }
  }, [])

  const stop = () => {
    if (statusRef.current !== 'running') return
    statusRef.current = 'stopped'
    generationRef.current += 1
    setStatus('stopped')
    abortRef.current?.abort()
    abortRef.current = null
  }

  const run = async () => {
    if (statusRef.current === 'running') return
    if (!snapshotReady || !fholdAvailable || positionCount === 0) return

    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    const generation = generationRef.current + 1
    generationRef.current = generation
    statusRef.current = 'running'
    setStatus('running')
    setContent('')
    setError(null)
    setElapsedMs(null)

    const payload: PositionAnalysisRequest = {
      l2_rules: [],
      index_rebalance_tail_window: false,
      public_research_enabled: publicResearchEnabled,
      public_research_channels: ['twitter'],
    }
    if (profileId) payload.profile_id = profileId

    try {
      for await (const event of api.positionAnalysisStream(payload, ac.signal)) {
        if (generation !== generationRef.current || statusRef.current !== 'running') return
        if (event.type === 'delta') {
          setContent(prev => prev + event.content)
        } else if (event.type === 'done') {
          if (generation !== generationRef.current || statusRef.current !== 'running') return
          statusRef.current = 'done'
          setStatus('done')
          if (typeof event.elapsed_ms === 'number') setElapsedMs(event.elapsed_ms)
        } else if (event.type === 'error') {
          if (generation !== generationRef.current || statusRef.current !== 'running') return
          statusRef.current = 'error'
          setError(event.message)
          setStatus('error')
          if (typeof event.elapsed_ms === 'number') setElapsedMs(event.elapsed_ms)
        }
      }
      if (generation === generationRef.current && statusRef.current === 'running' && !ac.signal.aborted) {
        statusRef.current = 'error'
        setError('持仓研究流已结束，但未收到完成事件')
        setStatus('error')
      }
    } catch (err) {
      if (generation !== generationRef.current) return
      const aborted = ac.signal.aborted
        || (err instanceof DOMException && err.name === 'AbortError')
        || (err instanceof Error && err.name === 'AbortError')
      if (aborted) {
        if (statusRef.current === 'running') {
          statusRef.current = 'stopped'
          setStatus('stopped')
        }
        return
      }
      if (statusRef.current !== 'running') return
      statusRef.current = 'error'
      setError(err instanceof Error && err.message ? err.message : '持仓研究失败')
      setStatus('error')
    } finally {
      if (generation === generationRef.current && abortRef.current === ac) {
        abortRef.current = null
      }
    }
  }

  const twitterHealth = publicResearchHealth.data?.health.twitter
  const healthLabel = publicResearchHealth.isLoading
    ? '检查中'
    : twitterHealth?.status === 'ok'
      ? `X · ${twitterHealth.active_backend ?? '可用'}`
      : '源不可用'

  return (
    <section className="panel overflow-hidden">
      <div className="panel-header">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <h3 className="section-title">持仓研究</h3>
          <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">Pi Agent</span>
          <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-secondary">只读</span>
          <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-secondary">全仓</span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1.5">
          <AiProviderSelector
            entry="position_analysis"
            value={profileId}
            onChange={setProfileId}
            compact
            providers={OPENAI_COMPAT_PROVIDERS}
          />
          <label
            className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border bg-elevated/30 px-2 text-[11px] text-secondary transition-colors hover:border-sky-500/30 hover:text-foreground"
            title="通过 Agent Reach 搜索 Twitter/X 公开消息；默认关闭，外部内容按 C 级未核验证据处理，与 Pi 运行时分离"
          >
            <input
              type="checkbox"
              checked={publicResearchEnabled}
              onChange={event => setPublicResearchEnabled(event.target.checked)}
              className="h-3.5 w-3.5 rounded border-border accent-sky-500"
            />
            <MessagesSquare className="h-3.5 w-3.5 text-sky-300" />
            <span>Twitter/X</span>
            {publicResearchEnabled && (
              <span className="font-mono text-[9px] text-muted">{healthLabel}</span>
            )}
          </label>
          {status === 'running' ? (
            <button type="button" onClick={stop} className="btn-secondary !h-8 text-xs">
              <Square className="h-3 w-3" />
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => { void run() }}
              disabled={!canStart}
              title={blockedReason ?? undefined}
              className="btn-primary !h-8 text-xs"
            >
              {status === 'idle' ? <Bot className="h-3 w-3" /> : <RotateCcw className="h-3 w-3" />}
              {status === 'idle' ? '运行持仓研究' : '重新运行'}
            </button>
          )}
        </div>
      </div>
      <div className="panel-body space-y-3">
        <p className="text-[11px] leading-relaxed text-muted">
          全仓只读研究，不是交易动作。前端不提交持仓明细、成本或单票；后端自行读取并冻结 fhold 日快照。
          Agent Reach 公开消息默认关闭，仅允许 Twitter/X，与 Pi runtime 分离。
        </p>
        {blockedReason && (
          <p className="text-xs text-warning">{blockedReason}</p>
        )}
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted">
          {status === 'running' && (
            <span className="inline-flex items-center gap-1 text-accent">
              <Loader2 className="h-3 w-3 animate-spin" />
              研究中
            </span>
          )}
          {status === 'stopped' && <span>已停止</span>}
          {status === 'done' && elapsedMs != null && (
            <span>耗时 {elapsedMs >= 1000 ? `${(elapsedMs / 1000).toFixed(1)}s` : `${Math.round(elapsedMs)}ms`}</span>
          )}
          {status === 'error' && elapsedMs != null && (
            <span>耗时 {elapsedMs >= 1000 ? `${(elapsedMs / 1000).toFixed(1)}s` : `${Math.round(elapsedMs)}ms`}</span>
          )}
        </div>
        {error && status === 'error' && (
          <p className="text-xs text-danger">{error}</p>
        )}
        {content ? (
          <div className={cn('text-xs leading-relaxed text-secondary', status === 'running' && 'opacity-90')}>
            <MarkdownRenderer content={content} />
          </div>
        ) : status === 'running' ? (
          <p className="py-2 text-center text-xs text-muted">正在生成全仓研究报告…</p>
        ) : null}
      </div>
    </section>
  )
}
