import { AlertTriangle, CheckCircle2, Database, ShieldAlert } from 'lucide-react'
import type { BacktestMetricContext, FactorBacktestResult } from '@/lib/api'
import { buildMetricFindings, finiteMetric } from '@/lib/backtestMetrics'
import { fmtPct } from '@/lib/format'

interface Props {
  result: FactorBacktestResult
}

const fmt = (value: unknown, digits = 2) => {
  const parsed = finiteMetric(value)
  return parsed == null ? '—' : parsed.toFixed(digits)
}

const groupMonotonicity = (result: FactorBacktestResult) => {
  const ordered = [...result.group_stats].sort((left, right) => left.group - right.group)
  if (ordered.length < 2) return null
  let increasing = 0
  let decreasing = 0
  for (let index = 1; index < ordered.length; index += 1) {
    if (ordered[index].total_return >= ordered[index - 1].total_return) increasing += 1
    if (ordered[index].total_return <= ordered[index - 1].total_return) decreasing += 1
  }
  return Math.max(increasing, decreasing) / (ordered.length - 1)
}

export function FactorDiagnostics({ result }: Props) {
  const stats = result.long_short_stats ?? {}
  const context: Partial<BacktestMetricContext> = result.metric_context ?? stats.metric_context ?? {}
  const snapshot = result.data_snapshot
  const warnings = [...new Set((result.warnings ?? []).filter(Boolean))]
  const monotonicity = groupMonotonicity(result)
  const findings = [...buildMetricFindings(stats as Record<string, unknown>)]
  const icMean = finiteMetric(result.ic_mean)
  const ir = finiteMetric(result.ir)
  const winRate = finiteMetric(result.ic_win_rate)
  if (icMean != null && Math.abs(icMean) < 0.02) findings.push({ key: 'ic_mean', title: 'IC 信号较弱', detail: `Rank IC 均值 ${(icMean * 100).toFixed(2)}%，横截面排序能力有限。`, severity: 'warning' })
  if (ir != null && Math.abs(ir) < 0.5) findings.push({ key: 'ic_ir', title: 'IC 稳定性偏弱', detail: `ICIR ${ir.toFixed(2)}，因子方向在不同日期间不够稳定。`, severity: 'warning' })
  if (winRate != null && winRate < 0.55) findings.push({ key: 'ic_win_rate', title: 'IC 胜率偏低', detail: `IC 胜率 ${(winRate * 100).toFixed(1)}%，正相关日期未形成明显优势。`, severity: 'warning' })
  if (monotonicity != null && monotonicity < 0.75) findings.push({ key: 'monotonicity', title: '分层收益缺少单调性', detail: `相邻分组同向比例 ${(monotonicity * 100).toFixed(0)}%，极端组差异可能由少数组驱动。`, severity: 'warning' })
  if (result.n_dates < 60) findings.push({ key: 'sample_days', title: '样本期偏短', detail: `仅 ${result.n_dates} 个交易日，难以覆盖多种市场环境。`, severity: 'warning' })
  if (warnings.length > 0) findings.push({ key: 'methodology_warnings', title: `${warnings.length} 条方法论提醒`, detail: '请先处理结果上方的方法论提醒，再解释 IC 与分层收益。', severity: 'warning' })

  const riskFreeRate = finiteMetric(context.risk_free_rate) ?? 0
  const advanced = [
    ['多空总收益', fmtPct(finiteMetric(stats.total_return))],
    ['多空年化', fmtPct(finiteMetric(stats.annual_return))],
    ['最大回撤', fmtPct(finiteMetric(stats.max_drawdown))],
    ['夏普', fmt(stats.sharpe)],
    ['年化波动', fmtPct(finiteMetric(stats.annual_volatility))],
    ['Calmar', fmt(stats.calmar)],
    ['Sortino', fmt(stats.sortino)],
    ['Omega', fmt(stats.omega)],
    ['尾部比率', fmt(stats.tail_ratio)],
    ['下行波动', fmtPct(finiteMetric(stats.downside_deviation))],
    ['Ulcer Index', fmtPct(finiteMetric(stats.ulcer_index))],
    ['VaR (5%)', fmtPct(finiteMetric(stats.value_at_risk))],
    ['CVaR (5%)', fmtPct(finiteMetric(stats.conditional_value_at_risk))],
    ['平均换手', fmtPct(finiteMetric(stats.avg_turnover))],
    ['累计换手', fmtPct(finiteMetric(stats.total_turnover))],
    ['累计成本', fmtPct(finiteMetric(stats.total_cost), 4)],
    ['分层单调性', monotonicity == null ? '—' : `${(monotonicity * 100).toFixed(0)}%`],
  ]

  return (
    <div className="space-y-3">
      <section className="overflow-hidden rounded-btn border border-border">
        <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div>
            <div className="text-xs font-medium text-foreground">因子健康审计</div>
            <div className="mt-0.5 text-[10px] text-muted">联合检查 IC、分层单调性、多空风险和样本覆盖</div>
          </div>
          <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${findings.length === 0 ? 'border-bull/30 bg-bull/10 text-bull' : 'border-warning/30 bg-warning/10 text-warning'}`}>
            {findings.length === 0 ? <CheckCircle2 className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />}
            {findings.length === 0 ? '未发现显著异常' : `${findings.length} 项待核对`}
          </span>
        </div>
        {findings.length === 0 ? (
          <div className="flex items-center gap-2 bg-bull/5 px-3 py-3 text-[11px] text-secondary">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-bull" />
            当前指标未触发内置异常规则；仍需独立留出期、容量与交易可实现性验证。
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
          <div className="mt-0.5 text-[10px] text-muted">IC 按日计算；分层与多空指标按实际调仓频率年化</div>
        </div>
        <div className="grid gap-px bg-border sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">收益频率</div><div className="mt-1 text-xs font-medium text-foreground">{String(context.return_frequency ?? result.config.rebalance ?? 'monthly')} · {finiteMetric(context.periods_per_year) ?? '—'} 期/年</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">无风险年化</div><div className="mt-1 font-mono text-xs font-semibold text-foreground">{fmtPct(riskFreeRate)}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">样本规模</div><div className="mt-1 text-xs font-medium text-foreground">{result.n_symbols} 只 · {result.n_dates} 日</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">样本标准差</div><div className="mt-1 text-xs font-medium text-foreground">ddof={finiteMetric(context.std_ddof) ?? 1}</div></div>
        </div>
        <div className="grid gap-px border-t border-border bg-border sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-surface px-3 py-2.5"><div className="flex items-center gap-1 text-[10px] text-muted"><Database className="h-3 w-3" />请求区间</div><div className="mt-1 font-mono text-[11px] text-secondary">{String(result.config.start ?? '—')} → {String(result.config.end ?? '—')}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">实际数据覆盖</div><div className="mt-1 font-mono text-[11px] text-secondary">{String(snapshot?.data_start ?? '—')} → {String(snapshot?.data_cutoff ?? '—')}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">Canonical generation</div><div className="mt-1 truncate font-mono text-[11px] text-secondary">{String(snapshot?.canonical_generation ?? '未冻结')}</div></div>
          <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">指标契约版本</div><div className="mt-1 font-mono text-[11px] text-secondary">{String(context.version ?? 'legacy')}</div></div>
        </div>
      </section>

      <section className="overflow-hidden rounded-btn border border-border">
        <div className="border-b border-border px-3 py-2">
          <div className="text-xs font-medium text-foreground">多空组合专业指标</div>
          <div className="mt-0.5 text-[10px] text-muted">最高分组做多、最低分组做空；成本按两腿实际换手扣除</div>
        </div>
        <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-3 lg:grid-cols-4">
          {advanced.map(([label, value]) => (
            <div key={label} className="bg-surface px-3 py-2.5">
              <div className="text-[10px] text-muted">{label}</div>
              <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{value}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
