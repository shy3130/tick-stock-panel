import type { EChartsOption } from 'echarts'
import { Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import {
  formatMetric,
  metricKeys,
  type ArmRow,
  type CalendarWindow,
  type EventRow,
  type HorizonRow,
  type RetrievalItem,
  type RiskBlock,
  type SeriesPoint,
  type ShapeBin,
} from '../model/result'
import type { ResearchRunDetail } from '../model/run'
import { UnavailableState } from './QueryState'

export function ArmTable({ arms }: { arms: ArmRow[] }) {
  if (arms.length === 0) return <p className="text-xs text-muted">此运行没有 arm 对照表。</p>
  const keys = metricKeys(arms)
  return (
    <div className="data-table-scroll">
      <table className="data-table min-w-[40rem]">
        <thead>
          <tr>
            <th>Arm</th>
            <th>基线</th>
            <th>样本</th>
            <th>OOS</th>
            <th>裁决</th>
            {keys.map((key) => <th key={key}>{key}</th>)}
          </tr>
        </thead>
        <tbody>
          {arms.map((arm) => (
            <tr key={arm.id}>
              <td><span className="font-medium">{arm.title}</span><p className="font-mono text-[11px] text-muted">{arm.id}</p></td>
              <td className="font-mono text-xs">{arm.baseline ?? '—'}</td>
              <td className="num">{arm.samples ?? '—'}</td>
              <td className="num">{arm.oos_samples ?? '—'}</td>
              <td>{arm.verdict ?? '—'}</td>
              {keys.map((key) => <td key={key} className="num">{formatMetric(arm.metrics[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function HorizonMatrix({ rows }: { rows: HorizonRow[] }) {
  if (rows.length === 0) return <p className="text-xs text-muted">此运行没有 horizon 矩阵。</p>
  const keys = metricKeys(rows)
  return (
    <div className="data-table-scroll">
      <table className="data-table min-w-[32rem]">
        <thead>
          <tr>
            <th>Horizon</th>
            <th>Arm</th>
            {keys.map((key) => <th key={key}>{key}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.horizon}-${row.arm_id}-${index}`}>
              <td className="font-mono">{row.horizon || '—'}</td>
              <td className="font-mono text-xs">{row.arm_id ?? '—'}</td>
              {keys.map((key) => <td key={key} className="num">{formatMetric(row.metrics[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function RiskCharts({ risk, series }: { risk: RiskBlock | null; series: SeriesPoint[] }) {
  const option: EChartsOption | null = series.length === 0 ? null : {
    backgroundColor: 'transparent',
    textStyle: { color: 'currentColor' },
    tooltip: { trigger: 'axis' },
    legend: { data: ['equity', 'baseline', 'drawdown'], top: 0 },
    grid: { left: 48, right: 16, top: 28, bottom: 32 },
    xAxis: { type: 'category', data: series.map((point) => point.t), axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { opacity: 0.2 } } },
    series: [
      { name: 'equity', type: 'line', showSymbol: false, data: series.map((point) => point.equity) },
      { name: 'baseline', type: 'line', showSymbol: false, data: series.map((point) => point.baseline) },
      { name: 'drawdown', type: 'line', showSymbol: false, data: series.map((point) => point.drawdown) },
    ],
  }
  const chartRef = useECharts(option, [series])
  return (
    <div className="space-y-3">
      {risk ? (
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          <RiskMetric label="MDD" value={risk.max_drawdown} />
          <RiskMetric label="Vol" value={risk.volatility} />
          <RiskMetric label="Sharpe" value={risk.sharpe} />
          <RiskMetric label="Sortino" value={risk.sortino} />
          <RiskMetric label="Calmar" value={risk.calmar} />
        </dl>
      ) : <p className="text-xs text-muted">没有风险摘要。</p>}
      {option ? <div ref={chartRef} className="h-64 w-full min-w-0" role="img" aria-label="净值与回撤" /> : <p className="text-xs text-muted">没有可绘制的曲线。曲线由服务端下采样，前端不重算。</p>}
    </div>
  )
}

function RiskMetric({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-input border border-border bg-base/40 px-2 py-2">
      <dt className="text-[11px] text-muted">{label}</dt>
      <dd className="font-mono text-sm tabular-nums">{formatMetric(value)}</dd>
    </div>
  )
}

export function EventTable({
  rows,
  onLoadMore,
  hasMore,
  pending,
}: {
  rows: EventRow[]
  onLoadMore?: () => void
  hasMore?: boolean
  pending?: boolean
}) {
  if (rows.length === 0) return <p className="text-xs text-muted">没有事件行。事件按 cursor 分页，单页最多 200 行。</p>
  const extraKeys = Array.from(new Set(rows.flatMap((row) => Object.keys(row.extra)))).slice(0, 6)
  return (
    <div className="space-y-2">
      <div className="data-table-scroll">
        <table className="data-table min-w-[48rem]">
          <thead>
            <tr>
              <th>日期</th>
              <th>标的</th>
              <th>Arm</th>
              <th>合格</th>
              <th>可达</th>
              <th>删失</th>
              {extraKeys.map((key) => <th key={key}>{key}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td className="font-mono text-xs">{row.date ?? '—'}</td>
                <td className="font-mono text-xs">{row.symbol ?? '—'}</td>
                <td className="font-mono text-xs">{row.arm ?? '—'}</td>
                <td>{row.qualified == null ? '—' : row.qualified ? '是' : '否'}</td>
                <td>{row.reachable == null ? '—' : row.reachable ? '是' : '否'}</td>
                <td className="font-mono text-[11px]">{row.censor_code ?? '—'}</td>
                {extraKeys.map((key) => <td key={key} className="num">{formatMetric(row.extra[key] as never)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMore && onLoadMore ? (
        <button type="button" className="btn-secondary min-h-11 text-xs" onClick={onLoadMore} disabled={pending}>
          {pending ? '加载中…' : '加载更多事件'}
        </button>
      ) : null}
    </div>
  )
}

export function ShapeDistributionView({ bins }: { bins: ShapeBin[] }) {
  if (bins.length === 0) return <p className="text-xs text-muted">没有形态分布。</p>
  const option: EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 12, top: 16, bottom: 48 },
    xAxis: { type: 'category', data: bins.map((bin) => bin.label), axisLabel: { rotate: 30, fontSize: 10 } },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: bins.map((bin) => bin.count) }],
  }
  const chartRef = useECharts(option, [bins])
  return (
    <div className="space-y-3">
      <div ref={chartRef} className="h-56 w-full min-w-0" role="img" aria-label="形态分布" />
      <div className="data-table-scroll">
        <table className="data-table">
          <thead><tr><th>形态</th><th>计数</th><th>占比</th></tr></thead>
          <tbody>
            {bins.map((bin) => (
              <tr key={bin.label}>
                <td>{bin.label}</td>
                <td className="num">{bin.count}</td>
                <td className="num">{bin.share == null ? '—' : formatMetric(bin.share)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function RetrievalView({ items }: { items: RetrievalItem[] }) {
  if (items.length === 0) return <p className="text-xs text-muted">没有检索/路由结果。</p>
  const extraKeys = Array.from(new Set(items.flatMap((item) => Object.keys(item.extra)))).slice(0, 6)
  return (
    <div className="data-table-scroll">
      <table className="data-table min-w-[36rem]">
        <thead>
          <tr>
            <th>Rank</th>
            <th>条目</th>
            <th>Score</th>
            {extraKeys.map((key) => <th key={key}>{key}</th>)}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td className="num">{item.rank ?? '—'}</td>
              <td>{item.title}<p className="font-mono text-[11px] text-muted">{item.id}</p></td>
              <td className="num">{formatMetric(item.score)}</td>
              {extraKeys.map((key) => <td key={key} className="num">{formatMetric(item.extra[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function CalendarEffectView({ windows }: { windows: CalendarWindow[] }) {
  if (windows.length === 0) return <p className="text-xs text-muted">没有日历窗口。</p>
  return (
    <div className="data-table-scroll">
      <table className="data-table min-w-[36rem]">
        <thead>
          <tr>
            <th>窗口</th>
            <th>开始</th>
            <th>结束</th>
            <th>效应</th>
            <th>样本</th>
          </tr>
        </thead>
        <tbody>
          {windows.map((window) => (
            <tr key={window.id}>
              <td>{window.title}</td>
              <td className="font-mono text-xs">{window.start ?? '—'}</td>
              <td className="font-mono text-xs">{window.end ?? '—'}</td>
              <td className="num">{formatMetric(window.effect)}</td>
              <td className="num">{window.samples ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ProfileResult({ run }: { run: ResearchRunDetail }) {
  const profile = run.result_profile ?? run.result?.profile
  if (run.verdict === 'unavailable') {
    return <UnavailableState reasons={run.unavailable_reasons} />
  }
  if (profile === 'event_signal') {
    return <EventTable rows={run.result?.profile === 'event_signal' ? run.result.preview : []} />
  }
  if (profile === 'shape_distribution') {
    return <ShapeDistributionView bins={run.result?.profile === 'shape_distribution' ? run.result.bins : []} />
  }
  if (profile === 'retrieval') {
    return <RetrievalView items={run.result?.profile === 'retrieval' ? run.result.items : []} />
  }
  if (profile === 'calendar_effect') {
    return <CalendarEffectView windows={run.result?.profile === 'calendar_effect' ? run.result.windows : []} />
  }
  return (
    <div className="space-y-4">
      <ArmTable arms={run.arms} />
      <HorizonMatrix rows={run.horizons} />
    </div>
  )
}

export function SummaryFacts({ run }: { run: ResearchRunDetail }) {
  const entries = Object.entries(run.summary).slice(0, 24)
  if (entries.length === 0) return <p className="text-xs text-muted">摘要为空。前端不重算指标。</p>
  return (
    <dl className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {entries.map(([key, value]) => (
        <div key={key} className="rounded-input border border-border bg-base/40 px-2 py-2">
          <dt className="truncate text-[11px] text-muted">{key}</dt>
          <dd className="truncate font-mono text-xs tabular-nums">{typeof value === 'number' ? formatMetric(value) : value == null ? '—' : String(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

export function WarningList({ run }: { run: ResearchRunDetail }) {
  if (run.warnings.length === 0) return null
  return (
    <Panel>
      <PanelHeader><h3 className="section-title">警告</h3></PanelHeader>
      <PanelBody>
        <ul className="space-y-1 text-xs text-warning">
          {run.warnings.map((warning) => (
            <li key={`${warning.code}-${warning.message}`}>{warning.message}</li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}
