import { useState, type ReactNode } from 'react'
import { AlertTriangle, Clock3, Database, RadioTower } from 'lucide-react'
import {
  useCollectionMonitor,
} from '@/components/collection-monitor/useCollectionMonitor'
import type {
  CollectionMonitorFilters,
  DatasetCollectionEvidence,
  DatasetKey,
  EvidenceEnvelope,
  EvidenceState,
  HealthState,
  MarketKey,
} from '@/components/collection-monitor/types'

const marketLabels: Record<MarketKey, string> = {
  cn: 'A股',
  hk: '港股',
  us: '美股',
}

const datasetLabels: Record<DatasetKey, string> = {
  capital_distribution: '资金分布',
  capital_flow: '资金流',
  candlestick_1m: '分钟 K 线',
  depth: '盘口深度',
  trades: '逐笔成交',
}

const statusLabels: Record<HealthState, string> = {
  green: '健康',
  yellow: '降级',
  red: '异常',
  gray: '未运行',
  unavailable: '不可用',
}

const modeLabels = {
  production: '生产',
  shadow: '影子观察',
  backfill: '回补',
} as const

const evidenceClasses: Record<EvidenceState, string> = {
  live: 'border-bear/30 bg-bear/10 text-bear',
  cached: 'border-warning/30 bg-warning/10 text-warning',
  unavailable: 'border-danger/30 bg-danger/10 text-danger',
}

const healthClasses: Record<HealthState, string> = {
  green: 'bg-bear/10 text-bear',
  yellow: 'bg-warning/10 text-warning',
  red: 'bg-danger/10 text-danger',
  gray: 'bg-elevated text-muted',
  unavailable: 'bg-danger/10 text-danger',
}

function todayInShanghai() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

function evidenceLabel(state: EvidenceState) {
  if (state === 'live') return '实时证据'
  if (state === 'cached') return '陈旧 / 缓存证据'
  return '证据不可用'
}

function EvidenceBadge({ evidence }: { evidence: EvidenceEnvelope }) {
  const confirmed = evidence.lastConfirmed
  const confirmedDetails = confirmed
    ? [
        confirmed.expectedCount !== undefined ? `预期 ${confirmed.expectedCount}` : undefined,
        confirmed.collectedCount !== undefined ? `采集 ${confirmed.collectedCount}` : undefined,
        confirmed.freshCount !== undefined ? `新鲜 ${confirmed.freshCount}` : undefined,
        confirmed.staleCount !== undefined ? `陈旧 ${confirmed.staleCount}` : undefined,
        confirmed.missingCount !== undefined ? `缺失 ${confirmed.missingCount}` : undefined,
        confirmed.latestDataAt ? `最新数据 ${confirmed.latestDataAt}` : undefined,
        confirmed.provenance ? `来源 ${confirmed.provenance}` : undefined,
      ].filter(Boolean).join(' · ')
    : ''

  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className={`rounded-full border px-2 py-0.5 font-medium ${evidenceClasses[evidence.evidenceState]}`}>
        {evidenceLabel(evidence.evidenceState)}
      </span>
      {evidence.evidenceAt && (
        <span className="inline-flex items-center gap-1 text-muted">
          <Clock3 className="h-3 w-3" />
          {evidence.evidenceAt}
        </span>
      )}
      {confirmed?.evidenceAt && (
        <span className="text-muted">最后确认 {confirmed.evidenceAt}</span>
      )}
      {confirmedDetails && (
        <span className="basis-full text-muted">{confirmedDetails}</span>
      )}
    </div>
  )
}

function Section({
  title,
  eyebrow,
  children,
  ariaLabel,
}: {
  title: string
  eyebrow: string
  children: ReactNode
  ariaLabel?: string
}) {
  return (
    <section
      className="rounded-card border border-border bg-surface p-4 sm:p-5"
      aria-label={ariaLabel}
    >
      <header className="mb-4">
        <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">{eyebrow}</p>
        <h2 className="mt-1 text-base font-semibold text-foreground">{title}</h2>
      </header>
      {children}
    </section>
  )
}

function EmptyEvidence() {
  return (
    <div className="rounded-btn border border-danger/25 bg-danger/5 px-3 py-4 text-sm text-danger">
      证据不可用
    </div>
  )
}

function HealthBadge({ state }: { state: HealthState | undefined }) {
  const value = state ?? 'unavailable'
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${healthClasses[value]}`}>
      {statusLabels[value]}
    </span>
  )
}

function DatasetCard({ dataset }: { dataset: DatasetCollectionEvidence }) {
  const key = dataset.datasetKey ?? dataset.dataset
  return (
    <div className="min-w-0 rounded-btn border border-border bg-base/50 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <strong className="text-sm text-foreground">{key ? datasetLabels[key] : '未知数据集'}</strong>
        <HealthBadge state={dataset.displayState ?? dataset.status ?? dataset.dataHealth} />
      </div>
      <div className="mt-2"><EvidenceBadge evidence={dataset} /></div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">任务状态</dt><dd className="text-right text-secondary">{statusLabels[dataset.taskHealth ?? 'unavailable']}</dd>
        <dt className="text-muted">数据状态</dt><dd className="text-right text-secondary">{statusLabels[dataset.dataHealth ?? 'unavailable']}</dd>
        <dt className="text-muted">采集 / 预期</dt><dd className="text-right font-mono text-secondary">{dataset.collectedCount ?? '—'} / {dataset.expectedCount ?? '—'}</dd>
        <dt className="text-muted">新鲜 / 陈旧</dt><dd className="text-right font-mono text-secondary">{dataset.freshCount ?? '—'} / {dataset.staleCount ?? '—'}</dd>
        <dt className="text-muted">缺口 / 重复</dt><dd className="text-right font-mono text-secondary">{dataset.missingCount ?? '—'} / {dataset.duplicateCount ?? '—'}</dd>
        <dt className="text-muted">最新数据</dt><dd className="text-right font-mono text-secondary">{dataset.latestDataAt ?? '—'}</dd>
      </dl>
      <p className="mt-2 truncate text-[11px] text-muted" title={dataset.provenance}>
        来源 {dataset.provenance ?? '未确认'}
      </p>
    </div>
  )
}

export function CollectionMonitor({ initialDate }: { initialDate?: string }) {
  const [filters, setFilters] = useState<CollectionMonitorFilters>({
    date: initialDate ?? todayInShanghai(),
    market: 'hk',
    dataset: 'capital_distribution',
  })
  const queries = useCollectionMonitor(filters)
  const availableOverview = queries.overview.data?.evidenceState === 'unavailable'
    ? undefined
    : queries.overview.data
  const anyError = queries.overview.isError
    || queries.markets.some(query => query.isError)
    || queries.tasks.isError
    || queries.gaps.isError

  const updateFilter = <K extends keyof CollectionMonitorFilters>(
    key: K,
    value: CollectionMonitorFilters[K],
  ) => setFilters(current => ({ ...current, [key]: value }))

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-4 p-3 sm:p-5 lg:p-6">
      <header className="flex flex-col gap-3 rounded-card border border-border bg-surface p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
        <div>
          <div className="flex items-center gap-2 text-accent">
            <RadioTower className="h-4 w-4" />
            <span className="text-[10px] font-semibold uppercase tracking-[0.2em]">Observation only</span>
          </div>
          <h1 className="mt-2 text-xl font-semibold text-foreground">采集监控</h1>
          <p className="mt-1 text-xs text-muted">只读证据视图 · 30 秒刷新 · 不控制采集器</p>
        </div>
        <div className="inline-flex w-fit items-center gap-2 rounded-btn border border-border bg-base px-3 py-2 text-xs text-secondary">
          <Database className="h-3.5 w-3.5 text-accent" />
          同源证据 API
        </div>
      </header>

      <form
        className="grid grid-cols-2 gap-3 rounded-card border border-border bg-surface p-4 sm:grid-cols-3 xl:grid-cols-6"
        aria-label="采集监控筛选"
        onSubmit={event => event.preventDefault()}
      >
        <Filter label="业务日期">
          <input
            className="w-full rounded-btn border border-border bg-base px-2.5 py-2 text-sm text-foreground"
            type="date"
            value={filters.date}
            onChange={event => updateFilter('date', event.target.value)}
          />
        </Filter>
        <Filter label="市场">
          <select
            value={filters.market}
            onChange={event => updateFilter('market', event.target.value as MarketKey)}
          >
            <option value="cn">A股</option><option value="hk">港股</option><option value="us">美股</option>
          </select>
        </Filter>
        <Filter label="数据集">
          <select
            value={filters.dataset}
            onChange={event => updateFilter('dataset', event.target.value as DatasetKey)}
          >
            {Object.entries(datasetLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Filter>
        <Filter label="状态">
          <select
            value={filters.status ?? ''}
            onChange={event => updateFilter('status', (event.target.value || undefined) as HealthState | undefined)}
          >
            <option value="">全部状态</option>
            {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Filter>
        <Filter label="技术">
          <select
            value={filters.technology ?? ''}
            onChange={event => updateFilter('technology', (event.target.value || undefined) as CollectionMonitorFilters['technology'])}
          >
            <option value="">全部技术</option><option value="rust">Rust</option>
            <option value="websocket">WebSocket</option><option value="python">Python</option>
            <option value="batch">批处理</option>
          </select>
        </Filter>
        <Filter label="模式">
          <select
            value={filters.mode ?? ''}
            onChange={event => updateFilter('mode', (event.target.value || undefined) as CollectionMonitorFilters['mode'])}
          >
            <option value="">全部模式</option><option value="production">生产</option>
            <option value="shadow">影子观察</option><option value="backfill">回补</option>
          </select>
        </Filter>
      </form>

      {anyError && (
        <div role="alert" className="flex items-center gap-2 rounded-card border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          采集证据当前不可用；未使用健康回退数据。
        </div>
      )}

      <Section title="今日采集结论" eyebrow="Daily overview">
        {queries.overview.isError
          ? <EmptyEvidence />
          : (
            <div>
              {queries.overview.data && <EvidenceBadge evidence={queries.overview.data} />}
              <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
                {[
                  ['登记任务', availableOverview?.taskCount],
                  ['生产健康任务', availableOverview?.productionHealthyCount],
                  ['异常任务', availableOverview?.unhealthyTaskCount],
                  ['开放缺口', availableOverview?.openGapCount],
                ].map(([label, value]) => (
                  <div key={String(label)} className="rounded-btn border border-border bg-base/50 p-3">
                    <p className="text-xs text-muted">{label}</p>
                    <p className="mt-1 font-mono text-xl font-semibold text-foreground">{value ?? '—'}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
      </Section>

      <Section title="市场 × 数据集" eyebrow="Market matrix" ariaLabel="市场 × 数据集">
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
          {(['cn', 'hk', 'us'] as const).map((market, index) => {
            const query = queries.markets[index]
            const evidence = query.data
            return (
              <article key={market} className="min-w-0 rounded-card border border-border bg-base/30 p-3">
                <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
                  <h3 className="text-sm font-semibold text-foreground">{marketLabels[market]}</h3>
                  {evidence && !query.isError && <EvidenceBadge evidence={evidence} />}
                </header>
                {query.isError
                  ? <EmptyEvidence />
                  : evidence?.datasets.length
                    ? <div className="grid gap-2">{evidence.datasets.map(dataset => (
                        <DatasetCard
                          key={`${market}-${dataset.datasetKey ?? dataset.dataset ?? 'unknown'}`}
                          dataset={dataset}
                        />
                      ))}</div>
                    : evidence
                      ? (
                        <div className="space-y-2">
                          {evidence.evidenceState === 'unavailable' && <EmptyEvidence />}
                          {evidence.lastConfirmed?.provenance && (
                            <p className="text-xs text-muted">来源 {evidence.lastConfirmed.provenance}</p>
                          )}
                        </div>
                      )
                      : <p className="text-sm text-muted">读取中…</p>}
              </article>
            )
          })}
        </div>
      </Section>

      <Section title="采集任务" eyebrow="Task evidence">
        {queries.tasks.isError
          ? <EmptyEvidence />
          : (
            <div className="overflow-x-auto">
              {queries.tasks.data && <div className="mb-3"><EvidenceBadge evidence={queries.tasks.data} /></div>}
              <table className="w-full min-w-[820px] text-left text-xs" aria-label="采集任务">
                <thead className="border-b border-border text-muted">
                  <tr>
                    <th className="px-3 py-2 font-medium">任务</th>
                    <th className="px-3 py-2 font-medium">技术</th>
                    <th className="px-3 py-2 font-medium">模式</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 font-medium">证据</th>
                    <th className="px-3 py-2 font-medium">最后写入</th>
                    <th className="px-3 py-2 font-medium">来源</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(queries.tasks.data?.tasks ?? []).map(task => (
                    <tr key={task.taskKey}>
                      <th scope="row" className="px-3 py-3 font-mono font-medium text-foreground">{task.taskKey}</th>
                      <td className="px-3 py-3 text-secondary">{task.technology ?? '—'}</td>
                      <td className="px-3 py-3 text-secondary">{task.mode ? modeLabels[task.mode] : '—'}</td>
                      <td className="px-3 py-3"><HealthBadge state={task.status} /></td>
                      <td className="px-3 py-3"><EvidenceBadge evidence={task} /></td>
                      <td className="px-3 py-3 font-mono text-muted">{task.lastWriteAt ?? '—'}</td>
                      <td className="px-3 py-3 text-muted">{task.provenance ?? '未确认'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!queries.tasks.isLoading && !queries.tasks.data?.tasks.length && (
                <p className="py-4 text-sm text-muted">当前筛选条件下没有任务证据。</p>
              )}
            </div>
          )}
      </Section>

      <Section title="Symbol / 分钟缺口" eyebrow="Gap evidence">
        {queries.gaps.isError
          ? <EmptyEvidence />
          : (
            <div className="overflow-x-auto">
              {queries.gaps.data && <div className="mb-3"><EvidenceBadge evidence={queries.gaps.data} /></div>}
              <table className="w-full min-w-[760px] text-left text-xs" aria-label="缺口证据">
                <thead className="border-b border-border text-muted">
                  <tr>
                    <th className="px-3 py-2 font-medium">Symbol</th>
                    <th className="px-3 py-2 font-medium">开始</th>
                    <th className="px-3 py-2 font-medium">结束</th>
                    <th className="px-3 py-2 font-medium">缺失</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 font-medium">来源</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(queries.gaps.data?.gaps ?? []).map((gap, index) => (
                    <tr key={`${gap.symbol}-${gap.startMinute}-${index}`}>
                      <th scope="row" className="px-3 py-3 font-mono font-medium text-foreground">{gap.symbol}</th>
                      <td className="px-3 py-3 font-mono text-muted">{gap.startMinute}</td>
                      <td className="px-3 py-3 font-mono text-muted">{gap.endMinute ?? gap.startMinute}</td>
                      <td className="px-3 py-3 font-mono text-secondary">{gap.missingCount ?? '—'}</td>
                      <td className="px-3 py-3 text-secondary">{gap.gapState === 'recovered' ? '已恢复' : '未恢复'}</td>
                      <td className="px-3 py-3 text-muted">{gap.provenance ?? '未确认'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!queries.gaps.isLoading && !queries.gaps.data?.gaps.length && (
                <p className="py-4 text-sm text-muted">没有已确认的缺口证据。</p>
              )}
            </div>
          )}
      </Section>
    </div>
  )
}

function Filter({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="min-w-0 text-xs text-muted">
      <span className="mb-1.5 block">{label}</span>
      <span className="[&>select]:w-full [&>select]:rounded-btn [&>select]:border [&>select]:border-border [&>select]:bg-base [&>select]:px-2.5 [&>select]:py-2 [&>select]:text-sm [&>select]:text-foreground">
        {children}
      </span>
    </label>
  )
}
