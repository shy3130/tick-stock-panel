import { useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Database, ShieldAlert } from 'lucide-react'
import type { BacktestMetricContext, StrategyBacktestResult } from '@/lib/api'
import { buildMetricFindings } from '@/lib/backtestMetrics'
import { useECharts } from '../charts/useECharts'
import {
  EXIT_REASON_LABELS,
  ContributionChart,
  ExecutionTimelineChart,
  UnderwaterDurationChart,
  buildContributionModel,
  buildExecutionModel,
  buildUnderwaterRows,
} from './ExecutionDiagnosticsCharts'
import { MetricExplainer } from './MetricExplainer'

interface Props {
  result: StrategyBacktestResult
}

type CurvePoint = { date: string; value: number; exposure?: number }

const WINDOWS = [20, 60, 120] as const

const MONTHS = Array.from({ length: 12 }, (_, index) => index + 1)

const finite = (value: unknown): number | null => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const fmtRatio = (value: unknown, digits = 2) => {
  const parsed = finite(value)
  return parsed == null ? '—' : parsed.toFixed(digits)
}

const fmtPct = (value: unknown, digits = 2) => {
  const parsed = finite(value)
  return parsed == null ? '—' : `${(parsed * 100).toFixed(digits)}%`
}

const rollingRows = (curve: CurvePoint[], window: number, annualRiskFree: number) => {
  // 展示型聚合: 过滤非正/非有限净值点，避免无效点污染收益率序列(曲线异常由健康审计单独报告)
  const points = curve
    .map(point => ({ date: point.date, value: Number(point.value) }))
    .filter(point => Number.isFinite(point.value) && point.value > 0)
  if (points.length < 2) return []
  const values = points.map(point => point.value)
  const returns = values.slice(1).map((value, index) => value / values[index] - 1)
  const riskFreePerDay = annualRiskFree > -1
    ? (1 + annualRiskFree) ** (1 / 252) - 1
    : 0
  return returns.map((_, index) => {
    const end = index + 1
    const start = Math.max(0, end - window)
    const sample = returns.slice(start, end)
    const mean = sample.reduce((sum, value) => sum + value, 0) / sample.length
    const variance = sample.length > 1
      ? sample.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (sample.length - 1)
      : 0
    const volatility = Math.sqrt(variance) * Math.sqrt(252)
    const excessMean = mean - riskFreePerDay
    const sharpe = variance > 0 ? excessMean / Math.sqrt(variance) * Math.sqrt(252) : null
    const periodReturn = values[end] / values[start] - 1
    return {
      date: points[end].date.slice(0, 10),
      periodReturn,
      volatility,
      sharpe,
    }
  })
}

const monthlyRows = (curve: CurvePoint[]) => {
  const months = new Map<string, { first: number; last: number }>()
  for (const point of curve) {
    const key = point.date.slice(0, 7)
    const value = Number(point.value)
    if (!Number.isFinite(value) || value <= 0) continue
    const current = months.get(key)
    if (current) current.last = value
    else months.set(key, { first: value, last: value })
  }
  const rows = new Map<number, Record<number, number>>()
  let previousLast: number | null = null
  for (const [key, values] of [...months.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    const [yearText, monthText] = key.split('-')
    const year = Number(yearText)
    const month = Number(monthText)
    if (!rows.has(year)) rows.set(year, {})
    // 口径: 月末净值相对上月末净值；首个可见月以区间内首个有效净值为基准
    const base = previousLast ?? values.first
    rows.get(year)![month] = values.last / base - 1
    previousLast = values.last
  }
  return [...rows.entries()].sort(([left], [right]) => right - left)
}

const heatClass = (value: number | undefined) => {
  if (value == null) return 'bg-base/40 text-muted'
  if (value >= 0.08) return 'bg-bull/25 text-bull'
  if (value >= 0.02) return 'bg-bull/15 text-bull'
  if (value >= 0) return 'bg-bull/5 text-bull'
  if (value <= -0.08) return 'bg-bear/25 text-bear'
  if (value <= -0.02) return 'bg-bear/15 text-bear'
  return 'bg-bear/5 text-bear'
}

function RollingChart({ curve, runId, riskFreeRate }: { curve: CurvePoint[]; runId: string; riskFreeRate: number }) {
  const [window, setWindow] = useState<(typeof WINDOWS)[number]>(60)
  const rows = useMemo(() => rollingRows(curve, window, riskFreeRate), [curve, riskFreeRate, window])
  const option = useMemo(() => rows.length === 0 ? null : ({
    animation: false,
    grid: { left: 52, right: 52, top: 24, bottom: 34 },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { color: '#94a3b8', fontSize: 10 } },
    xAxis: {
      type: 'category',
      data: rows.map(row => row.date),
      axisLabel: { color: '#64748b', fontSize: 10, interval: Math.max(0, Math.floor(rows.length / 6)) },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: '#64748b', fontSize: 10, formatter: (value: number) => `${value.toFixed(0)}%` },
        splitLine: { lineStyle: { color: '#1e293b' } },
      },
      {
        type: 'value', position: 'right',
        axisLabel: { color: '#64748b', fontSize: 10 },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: `${window}日收益`, type: 'line', symbol: 'none',
        data: rows.map(row => row.periodReturn == null ? null : row.periodReturn * 100),
        lineStyle: { width: 1.8, color: '#3b82f6' },
      },
      {
        name: '年化波动', type: 'line', symbol: 'none',
        data: rows.map(row => row.volatility * 100),
        lineStyle: { width: 1.2, color: '#f59e0b' },
      },
      {
        name: '滚动夏普', type: 'line', symbol: 'none', yAxisIndex: 1,
        data: rows.map(row => row.sharpe),
        lineStyle: { width: 1.2, color: '#10b981', type: 'dashed' },
      },
    ],
  }) as any, [rows, window])
  const ref = useECharts(option, [runId, window])

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2 px-3">
        <span className="text-[11px] text-muted">窗口内收益、波动与夏普随时间变化</span>
        <div className="inline-flex rounded-btn border border-border bg-base p-0.5">
          {WINDOWS.map(value => (
            <button
              key={value}
              type="button"
              onClick={() => setWindow(value)}
              className={`rounded px-2 py-1 text-[10px] ${window === value ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary'}`}
            >
              {value}日
            </button>
          ))}
        </div>
      </div>
      <div ref={ref} className="h-[240px]" />
    </div>
  )
}

export function ProfessionalDiagnostics({ result }: Props) {
  const curve = result.equity_curve as CurvePoint[]
  const exitRows = useMemo(() => {
    const groups = new Map<string, { count: number; wins: number; pnl: number; duration: number }>()
    for (const trade of result.trades ?? []) {
      const reason = trade.exit_reason || 'unknown'
      const row = groups.get(reason) ?? { count: 0, wins: 0, pnl: 0, duration: 0 }
      const pnlPct = finite(trade.pnl_pct) ?? 0
      row.count += 1
      row.wins += pnlPct > 0 ? 1 : 0
      row.pnl += finite(trade.pnl_amount) ?? 0
      row.duration += finite(trade.duration) ?? 0
      groups.set(reason, row)
    }
    return [...groups.entries()]
      .map(([reason, row]) => ({
        reason,
        label: EXIT_REASON_LABELS[reason] ?? reason,
        count: row.count,
        winRate: row.count > 0 ? row.wins / row.count : null,
        pnl: row.pnl,
        avgDuration: row.count > 0 ? row.duration / row.count : null,
      }))
      .sort((left, right) => right.count - left.count)
  }, [result.trades])
  // 占比分母用已聚合笔数(旧结果 trades 可能缺失, 不直接依赖 result.trades.length)
  const exitTotal = exitRows.reduce((sum, row) => sum + row.count, 0)
  const months = useMemo(() => monthlyRows(curve), [curve])
  const stats = result.stats ?? {}
  const config = result.config ?? {}
  const metricContext: Partial<BacktestMetricContext> = result.metric_context ?? {}
  const snapshot = result.data_snapshot
  const cost = (stats.cost_breakdown ?? {}) as Record<string, unknown>
  // 执行诊断模型: 全部由既有 equity_curve/drawdown_curve/trades/config 推导, 不改动交易计算
  const execution = useMemo(
    () => buildExecutionModel(curve, result.trades ?? [], result.config ?? {}, stats.initial_capital),
    [curve, result.trades, result.config, stats.initial_capital],
  )
  const underwater = useMemo(() => buildUnderwaterRows(result.drawdown_curve, curve), [result.drawdown_curve, curve])
  const exitContribution = useMemo(() => buildContributionModel(result.trades ?? [], 'exit_reason'), [result.trades])
  const symbolContribution = useMemo(() => buildContributionModel(result.trades ?? [], 'symbol'), [result.trades])
  // MAE/MFE: 逐笔持仓期偏移(旧 Run/不可得为 null → 剔除), 全部无效时区块不渲染
  const excursion = useMemo(() => {
    const maes: number[] = []
    const mfes: number[] = []
    for (const trade of result.trades ?? []) {
      const mae = finite(trade.mae_pct)
      const mfe = finite(trade.mfe_pct)
      if (mae != null) maes.push(mae)
      if (mfe != null) mfes.push(mfe)
    }
    return {
      nMae: maes.length,
      nMfe: mfes.length,
      avgMae: maes.length > 0 ? maes.reduce((sum, v) => sum + v, 0) / maes.length : null,
      avgMfe: mfes.length > 0 ? mfes.reduce((sum, v) => sum + v, 0) / mfes.length : null,
      worstMae: maes.length > 0 ? Math.min(...maes) : null,
      bestMfe: mfes.length > 0 ? Math.max(...mfes) : null,
    }
  }, [result.trades])
  const showExecutionTimeline = execution != null
    && execution.rows.length > 1
    && (execution.hasExposure || execution.hasTurnover || execution.hasCost)
  const costTotal = finite(cost.total)
  const estimatedCostTotal = execution?.estimatedCostTotal ?? null
  const riskFreeRate = finite(metricContext.risk_free_rate ?? config.risk_free_rate) ?? 0
  const warnings = [...new Set((result.warnings ?? []).filter(Boolean))]
  const curveFindings = useMemo(() => {
    const findings: Array<{ key: string; title: string; detail: string; severity: 'danger' | 'warning' }> = []
    const invalid = curve.filter(point => !Number.isFinite(Number(point.value)) || Number(point.value) <= 0).length
    let duplicateDates = 0
    let reversedDates = 0
    for (let index = 1; index < curve.length; index += 1) {
      const previous = curve[index - 1].date.slice(0, 10)
      const current = curve[index].date.slice(0, 10)
      if (current === previous) duplicateDates += 1
      else if (current < previous) reversedDates += 1
    }
    if (invalid > 0) findings.push({ key: 'curve_invalid', title: '净值曲线含无效点', detail: `${invalid} 个净值点不是正有限数，滚动指标可能失真。`, severity: 'danger' })
    if (duplicateDates > 0 || reversedDates > 0) findings.push({ key: 'curve_order', title: '净值日期序列异常', detail: `${duplicateDates} 个重复日期，${reversedDates} 个逆序日期。`, severity: 'danger' })
    const benchmarkPoints = result.benchmark_curve?.length ?? 0
    if (curve.length >= 20 && benchmarkPoints === 0) findings.push({ key: 'benchmark_missing', title: '缺少基准曲线', detail: '无法独立复核 Alpha、Beta、信息比率和跟踪误差。', severity: 'warning' })
    else if (curve.length >= 20 && benchmarkPoints / curve.length < 0.8) findings.push({ key: 'benchmark_coverage', title: '基准覆盖不足', detail: `基准点数仅为策略净值点数的 ${Math.round(benchmarkPoints / curve.length * 100)}%。`, severity: 'warning' })
    return findings
  }, [curve, result.benchmark_curve])
  const findings = [
    ...buildMetricFindings(stats),
    ...curveFindings,
    ...(warnings.length > 0 ? [{
      key: 'methodology_warnings',
      title: `${warnings.length} 条方法论提醒`,
      detail: '请先处理结果上方的方法论提醒，再解释收益与风险指标。',
      severity: 'warning' as const,
    }] : []),
  ]
  const advanced: Array<{ label: string; value: string; term?: string }> = [
    { label: 'Sortino', value: fmtRatio(stats.sortino), term: 'sortino' },
    { label: 'Calmar', value: fmtRatio(stats.calmar), term: 'calmar' },
    { label: 'Omega', value: fmtRatio(stats.omega) },
    { label: '利润因子', value: fmtRatio(stats.profit_factor), term: 'profit_factor' },
    { label: '盈亏比', value: fmtRatio(stats.payoff_ratio), term: 'payoff_ratio' },
    { label: '尾部比率', value: fmtRatio(stats.tail_ratio) },
    { label: '恢复因子', value: fmtRatio(stats.recovery_factor) },
    { label: '年化波动', value: fmtPct(stats.annual_volatility) },
    { label: '下行波动', value: fmtPct(stats.downside_deviation) },
    { label: 'Ulcer Index', value: fmtPct(stats.ulcer_index) },
    { label: 'VaR (5%)', value: fmtPct(stats.value_at_risk), term: 'var' },
    { label: 'CVaR (5%)', value: fmtPct(stats.conditional_value_at_risk), term: 'cvar' },
    { label: 'Alpha', value: fmtPct(stats.alpha), term: 'alpha' },
    { label: 'Beta', value: fmtRatio(stats.beta), term: 'beta' },
    { label: '信息比率', value: fmtRatio(stats.information_ratio) },
    { label: '跟踪误差', value: fmtPct(stats.tracking_error) },
  ]
  const sourceGenerations = snapshot?.source_generations
    ? Object.entries(snapshot.source_generations)
    : []

  return (
    <div className="space-y-3">
      <section className="overflow-hidden rounded-btn border border-border">
        <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div>
            <div className="text-xs font-medium text-foreground">结果健康审计</div>
            <div className="mt-0.5 text-[10px] text-muted">集中检查指标、曲线、基准覆盖和方法论告警</div>
          </div>
          <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${findings.length === 0 ? 'border-bull/30 bg-bull/10 text-bull' : 'border-warning/30 bg-warning/10 text-warning'}`}>
            {findings.length === 0 ? <CheckCircle2 className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />}
            {findings.length === 0 ? '未发现显著异常' : `${findings.length} 项待核对`}
          </span>
        </div>
        {findings.length === 0 ? (
          <div className="flex items-center gap-2 bg-bull/5 px-3 py-3 text-[11px] text-secondary">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-bull" />
            当前可计算指标与曲线结构未触发内置异常规则；这不替代样本外检验和人工复核。
          </div>
        ) : (
          <div className="divide-y divide-border/70">
            {findings.map(finding => (
              <div key={finding.key} className={`flex items-start gap-2 px-3 py-2.5 ${finding.severity === 'danger' ? 'bg-bear/5' : 'bg-warning/5'}`}>
                <AlertTriangle className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${finding.severity === 'danger' ? 'text-bear' : 'text-warning'}`} />
                <div className="min-w-0">
                  <div className="text-[11px] font-medium text-foreground">{finding.title}</div>
                  <div className="mt-0.5 text-[10px] leading-4 text-muted">{finding.detail}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-btn border border-border">
        <div className="border-b border-border px-3 py-2">
          <div className="text-xs font-medium text-foreground">统计口径与数据覆盖</div>
          <div className="mt-0.5 text-[10px] text-muted">所有风险调整指标共享同一 MetricContext，无风险收益按复利换算至单期</div>
        </div>
        <div className="grid gap-px bg-border sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">统计频率</div>
            <div className="mt-1 text-xs font-medium text-foreground">{String(metricContext.return_frequency ?? 'daily')} · {finite(metricContext.periods_per_year) ?? 252} 期/年</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">无风险年化</div>
            <div className="mt-1 font-mono text-xs font-semibold text-foreground num">{fmtPct(riskFreeRate)}</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">样本标准差</div>
            <div className="mt-1 text-xs font-medium text-foreground">ddof={finite(metricContext.std_ddof) ?? 1}</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">指标契约版本</div>
            <div className="mt-1 font-mono text-xs font-medium text-foreground">{String(metricContext.version ?? 'legacy')}</div>
          </div>
        </div>
        <div className="grid gap-px border-t border-border bg-border sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-surface px-3 py-2.5">
            <div className="flex items-center gap-1 text-[10px] text-muted"><Database className="h-3 w-3" />请求区间</div>
            <div className="mt-1 font-mono text-[11px] text-secondary">{String(config.start ?? '—')} → {String(config.end ?? '—')}</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">实际数据覆盖</div>
            <div className="mt-1 font-mono text-[11px] text-secondary">{String(snapshot?.data_start ?? curve[0]?.date?.slice(0, 10) ?? '—')} → {String(snapshot?.data_cutoff ?? curve.at(-1)?.date?.slice(0, 10) ?? '—')}</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">Canonical generation</div>
            <div className="mt-1 truncate font-mono text-[11px] text-secondary" title={String(snapshot?.canonical_generation ?? '')}>{String(snapshot?.canonical_generation ?? '未冻结')}</div>
          </div>
          <div className="bg-surface px-3 py-2.5">
            <div className="text-[10px] text-muted">数据源 generations</div>
            <div className="mt-1 text-[11px] text-secondary">{sourceGenerations.length > 0 ? `${sourceGenerations.length} 个已记录` : '未记录'}</div>
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-btn border border-border">
        <div className="border-b border-border px-3 py-2">
          <div className="text-xs font-medium text-foreground">高级收益与风险指标</div>
          <div className="mt-0.5 text-[10px] text-muted">不可计算时显示“—”；优先结合滚动窗口、月度分布和样本外结果解释</div>
        </div>
        <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
          {advanced.map(item => (
            <div key={item.label} className="bg-surface px-3 py-2.5">
              <div className="flex items-center gap-1 text-[10px] text-muted">
                {item.label}
                {item.term && <MetricExplainer term={item.term} />}
              </div>
              <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{item.value}</div>
            </div>
          ))}
        </div>
      </section>

      {excursion.nMae + excursion.nMfe > 0 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">持仓期偏移 (MAE / MFE)</div>
            <div className="mt-0.5 text-[10px] text-muted">
              可观测持仓窗口内日 K 最高/最低价相对入场价的偏移：MAE ≤ 0 为最大不利、MFE ≥ 0 为最大有利；
              建仓次日开盘成交（open_t+1）含入场日区间，收盘成交（close_t）自下一交易日起观测；退出日区间保守不计入；
              基于日 K 日内区间，不代表可实际成交实现的收益；旧结果或字段不可得时整块隐藏
            </div>
          </div>
          <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
            <div className="bg-surface px-3 py-2.5" title="持仓期间日 K 最低价相对入场价偏移的均值（≤0）">
              <div className="text-[10px] text-muted">平均 MAE</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${excursion.avgMae != null && excursion.avgMae < 0 ? 'text-bear' : 'text-foreground'}`}>{fmtPct(excursion.avgMae)}</div>
            </div>
            <div className="bg-surface px-3 py-2.5" title="全部样本中最深的单笔不利偏移（≤0）">
              <div className="text-[10px] text-muted">最深 MAE</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${excursion.worstMae != null && excursion.worstMae < 0 ? 'text-bear' : 'text-foreground'}`}>{fmtPct(excursion.worstMae)}</div>
            </div>
            <div className="bg-surface px-3 py-2.5" title="持仓期间日 K 最高价相对入场价偏移的均值（≥0）">
              <div className="text-[10px] text-muted">平均 MFE</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${excursion.avgMfe != null && excursion.avgMfe > 0 ? 'text-bull' : 'text-foreground'}`}>{fmtPct(excursion.avgMfe)}</div>
            </div>
            <div className="bg-surface px-3 py-2.5" title="全部样本中最高的单笔有利偏移（≥0）">
              <div className="text-[10px] text-muted">最高 MFE</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${excursion.bestMfe != null && excursion.bestMfe > 0 ? 'text-bull' : 'text-foreground'}`}>{fmtPct(excursion.bestMfe)}</div>
            </div>
          </div>
          <div className="border-t border-border px-3 py-2 text-[10px] text-muted">
            有效样本 {excursion.nMae} 笔 MAE / {excursion.nMfe} 笔 MFE（字段缺失或非有限的交易不参与聚合，不按 0 计）。
          </div>
        </section>
      )}

      {curve.length > 2 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2 text-xs font-medium text-foreground">滚动稳定性</div>
          <RollingChart curve={curve} runId={result.run_id} riskFreeRate={riskFreeRate} />
        </section>
      )}

      {underwater.length > 1 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">水下回撤与持续时长</div>
            <div className="mt-0.5 text-[10px] text-muted">按日期顺序累计的连续水下天数，净值回到前高后归零；与回撤幅度同图对照</div>
          </div>
          <UnderwaterDurationChart rows={underwater} />
        </section>
      )}

      {months.length > 0 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">月度收益热图</div>
            <div className="mt-0.5 text-[10px] text-muted">月末净值相对上月末；首个可见月自区间内首个有效净值起算，不以单笔交易收益替代</div>
          </div>
          <div className="overflow-x-auto p-3">
            <div className="min-w-[720px] space-y-1">
              <div className="grid grid-cols-[3.5rem_repeat(12,minmax(3rem,1fr))] gap-1 text-center text-[9px] text-muted">
                <span>年份</span>
                {MONTHS.map(month => <span key={month}>{month}月</span>)}
              </div>
              {months.map(([year, values]) => (
                <div key={year} className="grid grid-cols-[3.5rem_repeat(12,minmax(3rem,1fr))] gap-1 text-center text-[10px]">
                  <span className="flex items-center justify-center font-mono text-secondary">{year}</span>
                  {MONTHS.map(month => (
                    <span key={month} className={`rounded px-1 py-1.5 font-mono num ${heatClass(values[month])}`}>
                      {values[month] == null ? '—' : `${(values[month] * 100).toFixed(1)}%`}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      <section className="overflow-hidden rounded-btn border border-border">
        <div className="border-b border-border px-3 py-2">
          <div className="text-xs font-medium text-foreground">暴露与成本</div>
          <div className="mt-0.5 text-[10px] text-muted">执行时间线由净值曲线与成交记录推导：当日换手 = 当日成交名义金额 ÷ 初始资金；成本按固定成本模型（fees_pct + slippage_bps/10000 × 名义金额）逐日累计估算</div>
        </div>
        <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">平均暴露</div><div className="mt-1 font-mono text-sm text-foreground">{fmtPct(stats.avg_exposure)}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">最大暴露</div><div className="mt-1 font-mono text-sm text-foreground">{fmtPct(stats.max_exposure)}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">组合换手</div><div className="mt-1 font-mono text-sm text-foreground">{fmtRatio(cost.turnover, 2)}x</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">估算总成本</div><div className="mt-1 font-mono text-sm text-foreground">{finite(cost.total)?.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) ?? '—'}</div></div>
        </div>
        {showExecutionTimeline && execution && (
          <div className="border-t border-border">
            <ExecutionTimelineChart model={execution} />
          </div>
        )}
        <div className="border-t border-border px-3 py-2 text-[10px] text-muted">
          成本拆分：佣金 {finite(cost.commission)?.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) ?? '—'} · 滑点 {finite(cost.slippage)?.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) ?? '—'}；按实际成交名义金额估算。
          {estimatedCostTotal != null && (
            <span>
              {` 时间线累计估算 ${estimatedCostTotal.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 元`}
              {costTotal == null
                ? '（旧结果无 cost_breakdown 可校验）。'
                : Math.abs(estimatedCostTotal - costTotal) < 0.01
                  ? '，与 cost_breakdown 总额一致。'
                  : `，与 cost_breakdown 总额差异 ${Math.abs(costTotal) > 1e-9 ? `${(((estimatedCostTotal - costTotal) / costTotal) * 100).toFixed(2)}%` : `${(estimatedCostTotal - costTotal).toFixed(2)} 元`}（源于四舍五入或缺字段交易）。`}
            </span>
          )}
          {execution != null && execution.skippedTrades > 0 && ` ${execution.skippedTrades} 笔交易缺少名义金额字段，未计入换手与成本时间线。`}
        </div>
      </section>
      {exitRows.length > 0 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">退出归因</div>
            <div className="mt-0.5 text-[10px] text-muted">按真实平仓原因聚合笔数、胜率、盈亏额和平均持有期；不把单笔结果解释为策略因果</div>
          </div>
          {exitContribution.rows.length > 0 && <ContributionChart model={exitContribution} />}
          <div className="data-table-scroll border-t border-border">
            <table className="data-table min-w-[640px]">
              <thead>
                <tr>
                  <th>退出原因</th>
                  <th className="text-right">笔数</th>
                  <th className="text-right">占比</th>
                  <th className="text-right">胜率</th>
                  <th className="text-right">累计盈亏</th>
                  <th className="text-right">平均持有</th>
                </tr>
              </thead>
              <tbody>
                {exitRows.map(row => (
                  <tr key={row.reason}>
                    <td className="font-medium text-foreground">{row.label}</td>
                    <td className="text-right font-mono num">{row.count}</td>
                    <td className="text-right font-mono num">{fmtPct(exitTotal > 0 ? row.count / exitTotal : null)}</td>
                    <td className="text-right font-mono num">{fmtPct(row.winRate)}</td>
                    <td className={`text-right font-mono num ${row.pnl > 0 ? 'text-bull' : row.pnl < 0 ? 'text-bear' : 'text-muted'}`}>
                      {row.pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                    </td>
                    <td className="text-right font-mono num">{row.avgDuration == null ? '—' : `${row.avgDuration.toFixed(1)} 天`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {symbolContribution.rows.length > 0 && (
        <section className="overflow-hidden rounded-btn border border-border">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">标的贡献</div>
            <div className="mt-0.5 text-[10px] text-muted">
              按标的聚合笔数、胜率、平均与累计盈亏，按 |累计盈亏| 降序{symbolContribution.restCount > 0 ? `取前 10 只、其余 ${symbolContribution.restCount} 只合并` : ''}；聚合口径不等于收益归因模型
            </div>
          </div>
          <ContributionChart model={symbolContribution} />
        </section>
      )}
    </div>
  )
}
