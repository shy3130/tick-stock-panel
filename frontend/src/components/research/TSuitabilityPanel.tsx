import { useState } from 'react'
import { AlertCircle, BarChart3, CheckCircle2, CircleDashed, Database, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import { Badge, Btn, Panel, PanelBody, PanelHeader, badgeTone, type BadgeTone } from '@/components/ui/Primitives'
import { fetchMarketState, marketStateQueryKey, type MarketState, type MarketStateSnapshot } from '@/lib/marketState'
import { useQuery } from '@tanstack/react-query'
import { cn } from '@/lib/cn'

const STATE_META: Record<MarketState, { label: string; tone: BadgeTone; hint: string }> = {
  concentrated: { label: '抱团 / 拥挤', tone: 'warning', hint: '成交额集中度较高，且正超额贡献或 Top3 贡献集中。' },
  transition: { label: '过渡', tone: 'accent', hint: '未满足抱团或分散的全部固定条件。' },
  dispersed: { label: '分散', tone: 'success', hint: '仅该状态允许创建做T研究假设；不代表交易结论。' },
  unavailable: { label: '不可用', tone: 'danger', hint: '覆盖、校准或数据读取不足时 fail-closed，绝不按分散状态处理。' },
}

const METRICS: { key: keyof MarketStateSnapshot['metrics']; label: string; description: string; percentile?: keyof MarketStateSnapshot['percentiles'] }[] = [
  { key: 'return_std', label: '收益离散度', description: '个股 raw_close 收益截面标准差', percentile: 'return_std' },
  { key: 'return_q90_q10', label: '收益 Q90–Q10', description: '个股 raw_close 收益分位差' },
  { key: 'turnover_hhi', label: '成交额 HHI', description: '行业成交额归一化集中度', percentile: 'turnover_hhi' },
  { key: 'positive_return_hhi', label: '正超额 HHI', description: '行业正超额贡献归一化集中度', percentile: 'positive_return_hhi' },
  { key: 'top3_contribution', label: 'Top3 贡献', description: '正超额贡献前三行业累计占比', percentile: 'top3_contribution' },
  { key: 'top5_contribution', label: 'Top5 贡献', description: '正超额贡献前五行业累计占比' },
]

const PROTOCOL_ROWS = [
  ['协议', 'bollinger_volatility_t_research_v1'],
  ['K线精度', '5m'],
  ['观察窗口', '120 个交易日；至少 30 个事件'],
  ['信号滞后', 'T-1'],
  ['验证', '严格 walk-forward'],
  ['对照', '全部合格交易日 vs market_state=dispersed'],
  ['往返成本', '20 bps；敏感性 10 / 20 / 30 bps'],
] as const

function formatNumber(value: number | null): string {
  if (value === null) return '不可用'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value)
}

function formatPercentile(value: number | null | undefined): string {
  if (value === null || value === undefined) return '不可用'
  return `${(value * 100).toFixed(1)}%`
}

function coverageLabel(value: number | null, percent = false): string {
  if (value === null) return '不可用'
  return percent ? `${(value * 100).toFixed(1)}%` : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value)
}

function StateLegend({ active }: { active: MarketState }) {
  return (
    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4" aria-label="市场状态分类说明">
      {(Object.keys(STATE_META) as MarketState[]).map(state => {
        const meta = STATE_META[state]
        return (
          <div key={state} className={cn('rounded-input border px-2 py-1.5 text-[10px]', state === active ? badgeTone(meta.tone) : 'border-border bg-base/30 text-muted')}>
            <p className="font-medium">{meta.label}</p>
            <p className="mt-0.5 leading-relaxed opacity-80">{meta.hint}</p>
          </div>
        )
      })}
    </div>
  )
}

function GateReasons({ snapshot }: { snapshot: MarketStateSnapshot }) {
  const reasons = [...snapshot.gates.reasons, ...(snapshot.reason ? [snapshot.reason] : []), ...snapshot.warnings]
  if (snapshot.gates.automatic_research_allowed) {
    return <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-success"><CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />市场轴满足创建研究假设的固定条件；不会自动运行回测或生成买卖点。</p>
  }
  return (
    <div className="rounded-input border border-warning/30 bg-warning/5 px-2.5 py-2 text-[11px] leading-relaxed text-warning" role="status">
      <p className="flex items-center gap-1 font-medium"><ShieldAlert className="h-3.5 w-3.5" />研究动作已阻断</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-4">
        {(reasons.length ? reasons : ['市场状态未满足固定研究门槛。']).map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}
      </ul>
    </div>
  )
}

function SnapshotView({ snapshot }: { snapshot: MarketStateSnapshot }) {
  const meta = STATE_META[snapshot.state]
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border/70 pb-3">
        <div>
          <div className="flex items-center gap-2"><Badge tone={meta.tone}>{meta.label}</Badge><span className="text-[11px] text-secondary">市场轴 · {snapshot.available ? '可计算' : '不可用'}</span></div>
          <p className="mt-1 text-[11px] text-muted">目标日 {snapshot.target_date} · 严格使用上一交易日 {snapshot.signal_date ?? '不可用'}（T-1）</p>
        </div>
        <span className="font-mono text-[10px] text-muted">market_concentration_v1 · v1</span>
      </div>

      <StateLegend active={snapshot.state} />

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {METRICS.map(metric => (
          <div key={metric.key} className="rounded-input border border-border bg-base/30 px-2.5 py-2">
            <p className="text-[10px] text-muted">{metric.label}</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-foreground">{formatNumber(snapshot.metrics[metric.key])}</p>
            <p className="mt-0.5 text-[10px] leading-relaxed text-muted">{metric.description}{metric.percentile ? ` · 经验分位 ${formatPercentile(snapshot.percentiles[metric.percentile])}` : ''}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-2 lg:grid-cols-2">
        <div className="rounded-input border border-border bg-base/30 px-2.5 py-2">
          <p className="mb-1.5 text-[10px] font-medium text-secondary">覆盖与校准</p>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
            <dt className="text-muted">股票 / 行业</dt><dd className="text-right font-mono text-secondary">{coverageLabel(snapshot.coverage.stock_count)} / {coverageLabel(snapshot.coverage.industry_count)}</dd>
            <dt className="text-muted">标的 / 成交额映射</dt><dd className="text-right font-mono text-secondary">{coverageLabel(snapshot.coverage.symbol_coverage, true)} / {coverageLabel(snapshot.coverage.turnover_coverage, true)}</dd>
            <dt className="text-muted">有效校准日</dt><dd className="text-right font-mono text-secondary">{snapshot.coverage.calibration_days}</dd>
          </dl>
          <p className="mt-1.5 text-[10px] leading-relaxed text-muted">最低：1,000 股票、20 行业、90% 标的映射、95% 成交额映射；严格此前最多 252 日，至少 120 日。</p>
        </div>
        <div className="rounded-input border border-border bg-base/30 px-2.5 py-2">
          <p className="mb-1.5 text-[10px] font-medium text-secondary">数据来源与方法边界</p>
          <dl className="space-y-1 text-[10px]">
            <div className="flex justify-between gap-2"><dt className="text-muted">日线</dt><dd className="font-mono text-secondary">{snapshot.source.daily}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted">行业</dt><dd className="font-mono text-secondary">{snapshot.source.industry}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted">价格</dt><dd className="font-mono text-secondary">{snapshot.source.adjustment}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted">外部 fallback</dt><dd className="font-mono text-secondary">否</dd></div>
          </dl>
          <p className="mt-1.5 text-[10px] leading-relaxed text-muted">指标逐日计算后取 5 日中位数。未复刻视频隐藏公式。</p>
        </div>
      </div>

      <details className="rounded-input border border-border bg-base/30 px-2.5 py-2">
        <summary className="cursor-pointer text-[11px] font-medium text-secondary">固定研究协议（不会自动运行）</summary>
        <dl className="mt-2 grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
          {PROTOCOL_ROWS.map(([label, value]) => <div key={label} className="flex gap-2 text-[10px]"><dt className="shrink-0 text-muted">{label}</dt><dd className="font-mono text-secondary">{value}</dd></div>)}
        </dl>
      </details>

      <GateReasons snapshot={snapshot} />
    </div>
  )
}

export function TSuitabilityPanel() {
  const [requested, setRequested] = useState(false)
  const query = useQuery({
    queryKey: marketStateQueryKey(),
    queryFn: () => fetchMarketState(),
    enabled: requested,
    retry: false,
  })

  const load = () => setRequested(true)
  const refresh = () => void query.refetch()

  return (
    <Panel className="max-w-6xl">
      <PanelHeader className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          <BarChart3 className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-foreground">AI 短线研究池 · 做T适用性</h2>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted">研究观察池 / 非投资建议 / 不是买卖点。市场状态严格采用 T-1 已知事实，AI 不参与候选、阈值或状态判定。</p>
          </div>
        </div>
        {!requested ? <Btn variant="primary" className="text-xs" onClick={load}><Database className="h-3.5 w-3.5" />加载市场状态</Btn> : <Btn variant="secondary" className="text-xs" onClick={refresh} disabled={query.isFetching}><RefreshCw className={cn('h-3.5 w-3.5', query.isFetching && 'animate-spin')} />刷新</Btn>}
      </PanelHeader>
      <PanelBody>
        {!requested && <div className="flex min-h-48 flex-col items-center justify-center text-center"><CircleDashed className="h-6 w-6 text-muted" /><p className="mt-2 text-xs font-medium text-secondary">尚未加载市场状态</p><p className="mt-1 max-w-md text-[11px] leading-relaxed text-muted">点击“加载市场状态”才会发出只读请求；此处不会创建假设、运行回测或写入行情数据。</p></div>}
        {requested && query.isFetching && !query.data && <div className="flex min-h-48 items-center justify-center gap-2 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" />读取严格 T-1 市场状态…</div>}
        {requested && query.isError && <div className="rounded-input border border-danger/30 bg-danger/5 px-3 py-3 text-xs text-danger" role="alert"><p className="flex items-center gap-1.5 font-medium"><AlertCircle className="h-4 w-4" />无法读取市场状态</p><p className="mt-1 text-[11px] leading-relaxed">{query.error instanceof Error ? query.error.message : '请求未完成。'} 研究动作保持关闭。</p></div>}
        {query.data && <SnapshotView snapshot={query.data} />}
      </PanelBody>
    </Panel>
  )
}
