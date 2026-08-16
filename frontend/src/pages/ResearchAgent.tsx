import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Database,
  ExternalLink,
  FileSearch,
  Globe2,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { toast } from '@/components/Toast'
import { api, type ResearchAgentEvidence, type ResearchAgentRun } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useLastStock } from '@/lib/useLastStock'

const ACTIVE_STATUSES = new Set(['queued', 'planning', 'collecting', 'analyzing'])

const STATUS_LABEL: Record<string, string> = {
  queued: '等待调度',
  planning: '规划中',
  collecting: '采集中',
  analyzing: '分析中',
  succeeded: '已完成',
  failed: '未完成',
}

const TOOL_LABEL: Record<string, string> = {
  market_snapshot: '日线与技术指标',
  realtime_snapshot: '实时行情',
  financials: '财务与估值',
  market_intelligence: '热度、题材与龙虎榜',
  strategy_signals: '策略与信号',
  research_reports: '机构研报',
  announcements: '公司公告',
  web_news: '联网新闻',
}

function formatTime(value?: string) {
  if (!value) return '--'
  try {
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return value
  }
}

function statusClass(status: string) {
  if (status === 'succeeded' || status === 'available') return 'text-bull bg-bull/10 border-bull/25'
  if (status === 'failed' || status === 'unavailable') return 'text-danger bg-danger/10 border-danger/25'
  return 'text-amber-300 bg-amber-400/10 border-amber-400/25'
}

export function ResearchAgent() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [question, setQuestion] = useState('')
  const [includeWebNews, setIncludeWebNews] = useState(true)
  const [currentRunId, setCurrentRunId] = useState('')
  const [selectedCitation, setSelectedCitation] = useState('')
  const { last: lastStock, remember: rememberStock } = useLastStock('quant-lab')

  const requestedSymbol = (searchParams.get('symbol') ?? '').trim().toUpperCase()
  const urlSymbol = /^\d{6}\.(SH|SZ|BJ)$/.test(requestedSymbol) ? requestedSymbol : ''
  const urlName = (searchParams.get('name') ?? '').trim()
  const urlNameQuery = useQuery({
    queryKey: QK.instrumentNames(urlSymbol ? [urlSymbol] : []),
    queryFn: () => api.instrumentNames([urlSymbol]),
    enabled: Boolean(urlSymbol && !urlName),
    staleTime: 5 * 60_000,
  })
  const resolvedUrlName = urlName || urlNameQuery.data?.names[urlSymbol] || ''

  useEffect(() => {
    if (urlSymbol) {
      setSymbol(current => current === urlSymbol ? current : urlSymbol)
      if (resolvedUrlName) setName(resolvedUrlName)
      return
    }
    if (!symbol && lastStock) {
      setSymbol(lastStock.symbol)
      setName(lastStock.name)
    }
  }, [lastStock, resolvedUrlName, symbol, urlSymbol])

  const history = useQuery({
    queryKey: QK.researchAgentRuns,
    queryFn: () => api.researchAgentRuns(20),
    refetchInterval: (query) => query.state.data?.runs.some(run => ACTIVE_STATUSES.has(run.status)) ? 3_000 : false,
  })
  const current = useQuery({
    queryKey: QK.researchAgentRun(currentRunId),
    queryFn: () => api.researchAgentRun(currentRunId),
    enabled: Boolean(currentRunId),
    refetchInterval: (query) => ACTIVE_STATUSES.has(query.state.data?.run.status ?? '') ? 1_500 : false,
  })
  const run = current.data?.run || history.data?.runs.find(item => item.id === currentRunId) || null

  useEffect(() => {
    if (!currentRunId && history.data?.runs.length) setCurrentRunId(history.data.runs[0].id)
  }, [currentRunId, history.data?.runs])

  useEffect(() => {
    if (!run?.evidence.length) {
      setSelectedCitation('')
      return
    }
    if (!run.evidence.some(item => item.citation === selectedCitation)) {
      setSelectedCitation(run.evidence[0].citation)
    }
  }, [run?.id, run?.evidence, selectedCitation])

  const startRun = useMutation({
    mutationFn: () => api.researchAgentCreate({
      symbol,
      name,
      question,
      include_web_news: includeWebNews,
    }),
    onSuccess: ({ run: created }) => {
      queryClient.setQueryData(QK.researchAgentRun(created.id), { run: created })
      queryClient.invalidateQueries({ queryKey: QK.researchAgentRuns })
      setCurrentRunId(created.id)
      setSelectedCitation('')
      rememberStock(created.symbol, created.name || name)
      toast('研究任务已启动', 'success')
    },
    onError: (error) => {
      toast(error instanceof Error && error.message ? error.message : '研究任务启动失败，请稍后重试', 'error')
    },
  })

  const selectStock = (nextSymbol: string, nextName: string) => {
    setSymbol(nextSymbol)
    setName(nextName)
    rememberStock(nextSymbol, nextName)
    const next = new URLSearchParams(searchParams)
    next.set('symbol', nextSymbol)
    if (nextName) next.set('name', nextName)
    setSearchParams(next)
  }

  const active = Boolean(run && ACTIVE_STATUSES.has(run.status))
  const selectedEvidence = run?.evidence.find(item => item.citation === selectedCitation) || null

  return (
    <>
      <PageHeader
        title="Quant Lab"
        subtitle="证据优先的个股研究 Agent"
        titleExtra={<ShieldCheck className="h-4 w-4 text-emerald-400" aria-label="证据可审阅" />}
      />

      <main className="w-full px-5 py-5 space-y-5">
        <section className="border border-border/60 bg-surface/35 p-4">
          <div className="grid gap-3 xl:grid-cols-[18rem_minmax(18rem,1fr)_auto] xl:items-end">
            <div>
              <label className="mb-1.5 block text-[11px] text-muted">研究标的</label>
              <StockFinancialSearch onSelect={selectStock} assetTypes="stock" />
              {symbol && (
                <div className="mt-1.5 flex items-center gap-2 text-xs">
                  <span className="font-medium text-foreground">{name || symbol}</span>
                  <span className="font-mono text-muted">{symbol}</span>
                </div>
              )}
            </div>
            <label className="block min-w-0">
              <span className="mb-1.5 block text-[11px] text-muted">研究问题（可选）</span>
              <input
                value={question}
                maxLength={600}
                onChange={event => setQuestion(event.target.value)}
                placeholder="例如：梳理近期基本面变化、消息线索与尚待核实的风险"
                className="h-9 w-full border border-border bg-base px-3 text-xs text-foreground outline-none placeholder:text-muted/60 focus:border-accent/60"
              />
            </label>
            <div className="flex items-center justify-between gap-3 xl:justify-end">
              <label className="inline-flex items-center gap-2 text-xs text-secondary cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={includeWebNews}
                  onChange={event => setIncludeWebNews(event.target.checked)}
                  className="h-3.5 w-3.5 accent-emerald-500"
                />
                <Globe2 className="h-3.5 w-3.5 text-emerald-400" />
                联网新闻
              </label>
              <button
                onClick={() => startRun.mutate()}
                disabled={!symbol || startRun.isPending || active}
                className="inline-flex h-9 items-center gap-2 border border-emerald-400/35 bg-emerald-400/10 px-3 text-xs font-medium text-emerald-300 transition-colors hover:bg-emerald-400/15 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {startRun.isPending || active ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                {active ? '研究进行中' : '启动研究'}
              </button>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border/50 pt-2.5 text-[11px] text-muted">
            <span>服务端受控采集：行情、财务、热度、龙虎榜、策略、研报、公告</span>
            <span>模型仅接收脱敏后的证据包</span>
            <span>所有结果可按来源复核</span>
          </div>
        </section>

        {!run && !history.isLoading && (
          <EmptyState
            icon={Sparkles}
            title="选择标的后启动一份研究"
            hint="研究完成后会保留数据截止时间、各源状态、原始来源链接和逐条证据编号。"
          />
        )}

        {(history.isLoading || current.isLoading) && !run && (
          <div className="grid min-h-56 place-items-center text-muted">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        )}

        {run && (
          <RunWorkspace
            run={run}
            active={active}
            selectedEvidence={selectedEvidence}
            selectedCitation={selectedCitation}
            onSelectEvidence={setSelectedCitation}
            onRefresh={() => {
              current.refetch()
              history.refetch()
            }}
          />
        )}

        <RunHistory
          runs={history.data?.runs || []}
          activeRunId={currentRunId}
          onSelect={runId => {
            setCurrentRunId(runId)
            setSelectedCitation('')
          }}
        />
      </main>
    </>
  )
}

function RunWorkspace({
  run,
  active,
  selectedEvidence,
  selectedCitation,
  onSelectEvidence,
  onRefresh,
}: {
  run: ResearchAgentRun
  active: boolean
  selectedEvidence: ResearchAgentEvidence | null
  selectedCitation: string
  onSelectEvidence: (citation: string) => void
  onRefresh: () => void
}) {
  const coverage = useMemo(() => {
    const available = run.evidence.filter(item => item.status === 'available').length
    return `${available}/${run.evidence.length}`
  }, [run.evidence])

  return (
    <section className="border border-border/60 bg-surface/25">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border/60 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-foreground">{run.name || run.symbol}</span>
            <span className="font-mono text-[11px] text-muted">{run.symbol}</span>
            <span className={`border px-1.5 py-0.5 text-[10px] ${statusClass(run.status)}`}>
              {STATUS_LABEL[run.status] || run.status}
            </span>
          </div>
          <div className="mt-1 text-[11px] text-muted">{run.question || '全景研究'} · 创建于 {formatTime(run.created_at)}</div>
        </div>
        <div className="ml-auto flex items-center gap-3 text-[11px] text-muted">
          <span>证据覆盖 {coverage}</span>
          {run.runtime?.model && <span className="hidden md:inline">模型 {String(run.runtime.model)}</span>}
          <button onClick={onRefresh} className="p-1 text-muted hover:text-foreground" title="刷新任务状态">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {active && (
        <div className="border-b border-border/60 px-4 py-3">
          <div className="mb-2 flex items-center gap-2 text-xs text-secondary">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-400" />
            <span>{run.stage}</span>
            <span className="ml-auto font-mono text-muted">{run.progress}%</span>
          </div>
          <div className="h-1 overflow-hidden bg-elevated">
            <div className="h-full bg-emerald-400 transition-[width] duration-500" style={{ width: `${Math.max(2, run.progress)}%` }} />
          </div>
          {run.plan.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {run.plan.map(item => (
                <span key={item.tool} className="border border-border/60 bg-base/50 px-1.5 py-0.5 text-[10px] text-muted">
                  {TOOL_LABEL[item.tool] || item.tool}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {run.error && (
        <div className="flex items-start gap-2 border-b border-danger/25 bg-danger/5 px-4 py-3 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{run.error}</span>
        </div>
      )}

      <div className="grid min-h-[34rem] xl:grid-cols-[minmax(0,1.55fr)_minmax(20rem,0.8fr)]">
        <article className="min-w-0 border-b border-border/60 p-5 xl:border-b-0 xl:border-r">
          {run.answer ? (
            <MarkdownRenderer content={run.answer} />
          ) : active ? (
            <div className="grid min-h-80 place-items-center text-center">
              <div>
                <FileSearch className="mx-auto h-8 w-8 text-emerald-400/70" />
                <p className="mt-3 text-sm text-secondary">正在建立证据包</p>
                <p className="mt-1 text-xs text-muted">采集结果会先出现在右侧，完成后再生成带引用的研究记录。</p>
              </div>
            </div>
          ) : (
            <EmptyState icon={AlertTriangle} title="没有可展示的研究结论" hint="请检查右侧证据状态后重新运行。" />
          )}
        </article>

        <aside className="min-w-0 bg-base/15">
          <div className="border-b border-border/60 px-4 py-3">
            <div className="flex items-center gap-2">
              <Database className="h-3.5 w-3.5 text-emerald-400" />
              <h2 className="text-xs font-medium text-foreground">证据来源</h2>
              <span className="ml-auto text-[10px] text-muted">{run.evidence.length}</span>
            </div>
          </div>
          <div className="max-h-[28rem] overflow-y-auto p-2">
            {run.evidence.length === 0 ? (
              <div className="px-3 py-8 text-center text-xs text-muted">等待采集结果</div>
            ) : run.evidence.map(item => (
              <EvidenceRow
                key={item.citation}
                item={item}
                selected={selectedCitation === item.citation}
                onSelect={() => onSelectEvidence(item.citation)}
              />
            ))}
          </div>
          {selectedEvidence && <EvidenceDetail item={selectedEvidence} />}
        </aside>
      </div>
    </section>
  )
}

function EvidenceRow({ item, selected, onSelect }: {
  item: ResearchAgentEvidence
  selected: boolean
  onSelect: () => void
}) {
  return (
    <div className={`mb-1.5 border ${selected ? 'border-emerald-400/45 bg-emerald-400/[0.07]' : 'border-border/50 bg-surface/25'}`}>
      <div className="flex items-start gap-1 p-2">
        <button onClick={onSelect} className="min-w-0 flex-1 text-left">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-[10px] text-emerald-300">{item.citation}</span>
            <span className={`border px-1 py-px text-[9px] ${statusClass(item.status)}`}>{item.status}</span>
          </div>
          <div className="mt-1 truncate text-[11px] font-medium text-foreground">{item.title}</div>
          <div className="mt-0.5 truncate text-[10px] text-muted">{item.source}</div>
          <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-secondary">{item.summary}</p>
        </button>
        {item.url && (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            title="打开原始来源"
            className="p-1 text-muted hover:text-emerald-300"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
      </div>
    </div>
  )
}

function EvidenceDetail({ item }: { item: ResearchAgentEvidence }) {
  const data = JSON.stringify(item.data, null, 2)
  return (
    <div className="border-t border-border/60 px-3 py-3">
      <div className="mb-1.5 flex items-center gap-2 text-[11px] text-secondary">
        <ChevronRight className="h-3.5 w-3.5 text-emerald-400" />
        <span className="font-mono text-emerald-300">{item.citation}</span>
        <span className="truncate">{item.as_of ? `数据截止 ${item.as_of}` : `采集于 ${formatTime(item.retrieved_at)}`}</span>
      </div>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words border border-border/50 bg-base/60 p-2 text-[10px] leading-relaxed text-secondary">{data}</pre>
    </div>
  )
}

function RunHistory({ runs, activeRunId, onSelect }: {
  runs: ResearchAgentRun[]
  activeRunId: string
  onSelect: (runId: string) => void
}) {
  return (
    <section className="border-t border-border/70 pt-4">
      <div className="mb-2 flex items-center gap-2">
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
        <h2 className="text-xs font-medium text-foreground">研究运行记录</h2>
      </div>
      {runs.length === 0 ? (
        <p className="text-xs text-muted">还没有研究运行记录。</p>
      ) : (
        <div className="grid gap-px border border-border/60 bg-border/60 md:grid-cols-2 xl:grid-cols-3">
          {runs.map(item => (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={`min-w-0 bg-surface px-3 py-2.5 text-left transition-colors hover:bg-elevated/60 ${item.id === activeRunId ? 'bg-emerald-400/[0.07]' : ''}`}
            >
              <div className="flex items-center gap-2">
                <span className="truncate text-xs font-medium text-foreground">{item.name || item.symbol}</span>
                <span className={`ml-auto shrink-0 border px-1 py-px text-[9px] ${statusClass(item.status)}`}>{STATUS_LABEL[item.status] || item.status}</span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-[10px] text-muted">
                <span className="font-mono">{item.symbol}</span>
                <span>{formatTime(item.created_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}
