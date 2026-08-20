import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import type { EChartsOption } from 'echarts'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  Eye,
  FileJson,
  FileSpreadsheet,
  FileDown,
  History,
  Loader2,
  FlaskConical,
  Grid3X3,
  Pencil,
  RadioTower,
  RotateCcw,
  Search,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import {
  api,
  type BacktestDataSnapshot,
  type BacktestRunComparison,
  type BacktestRunKind,
  type BacktestRunListResponse,
  type BacktestExperimentSummary,
  type BacktestRunSummary,
  type BacktestRunTradeSample,
  type BacktestRun,
  type StrategyDetail,
} from '@/lib/api'
import { downloadCompareReportHtml, downloadRunReportHtml } from '@/lib/backtestReportDownload'
import {
  COMPARE_COLORS,
  CORE_METRIC_ORDER,
  KIND_LABELS,
  METRIC_META,
  compareWarningLabel,
  fmtDateTime,
  fmtNumOrDash,
  fmtPctOrDash,
  formatDeltaValue,
  formatDiffValue,
  formatMetricValue,
  metricLabel,
  runDisplayName,
} from '@/lib/compareReport'
import { toast } from '@/components/Toast'
import { EmptyState } from '@/components/EmptyState'
import { fmtPct, priceColorClass } from '@/lib/format'
import { openOptimizerExperiment } from '@/lib/optimizerTask'
import { openParameterGridExperiment } from '@/lib/parameterGridTask'
import { BacktestWarnings } from './components/BacktestWarnings'
import { useECharts } from './charts/useECharts'

/** 局部 query key 前缀；所有 run mutation 统一 invalidate 该前缀 */
export const RUNS_KEY = ['backtest-runs'] as const
const PAGE_SIZE = 50
/** 同时参与对比的最大 run 数；矩阵/曲线/导出报告按列数自适应，不再挤压 */
const MAX_COMPARE = 8

const KIND_BADGE_CLS: Record<BacktestRunKind, string> = {
  strategy: 'border-accent/30 bg-accent/10 text-accent',
  factor: 'border-warning/30 bg-warning/10 text-warning',
  composite: 'border-bull/30 bg-bull/10 text-bull',
}

/** F7 实验区 — 类型徽标与状态文案 (寻优/网格两类实验统一摘要) */
const EXPERIMENT_KIND_META: Record<BacktestExperimentSummary['kind'], { label: string; cls: string; Icon: typeof Grid3X3 }> = {
  optimizer: { label: '寻优', cls: 'border-accent/30 bg-accent/10 text-accent', Icon: FlaskConical },
  grid: { label: '网格', cls: 'border-warning/30 bg-warning/10 text-warning', Icon: Grid3X3 },
}

const EXPERIMENT_STATUS_LABELS: Record<BacktestExperimentSummary['status'], string> = {
  pending: '等待执行',
  running: '运行中',
  completed: '已完成',
  cancelled: '已取消',
  failed: '失败',
}

function experimentBestLine(row: BacktestExperimentSummary): string {
  if (!row.best) return '暂无最佳场景'
  const returns = row.best.total_return == null ? '' : ` · 收益 ${fmtPct(row.best.total_return)}`
  const sharpe = row.best.sharpe == null ? '' : ` · 夏普 ${row.best.sharpe.toFixed(2)}`
  return `${row.best.label} · 得分 ${row.best.score == null ? '—' : row.best.score.toFixed(3)}${returns}${sharpe}`
}

/** 列表行头部最多展示的指标数 */
const HEADLINE_ORDER = ['total_return', 'annual_return', 'sharpe', 'max_drawdown', 'win_rate', 'ic_mean', 'ir']

function metricValueClass(key: string, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return 'text-muted'
  if (METRIC_META[key]?.format === 'pct') return priceColorClass(value)
  return 'text-foreground'
}

function headlineStats(stats: Record<string, number>): [string, number][] {
  const entries: [string, number][] = []
  for (const key of HEADLINE_ORDER) {
    const value = stats[key]
    if (typeof value === 'number' && Number.isFinite(value)) entries.push([key, value])
    if (entries.length >= 4) break
  }
  return entries
}

/** F13: 定义指纹截断展示 (完整 12 位过长, 取前 8 位 + 省略号) */
function shortDefHash(hash: string): string {
  return hash.length > 8 ? `${hash.slice(0, 8)}…` : hash
}

/** F13 版本感知横幅: changed=定义已变更(黄) / removed=策略已不存在(红) / null=一致或无法比对 */
type DefHashBanner =
  | { kind: 'changed'; from: string; to: string }
  | { kind: 'removed' }

function defHashBannerOf(run: BacktestRun | undefined, strategies: StrategyDetail[] | undefined): DefHashBanner | null {
  if (!run || !strategies) return null
  // Run 未持久化指纹 (旧 Run / 因子 Run) 或列表未加载完成时不比对
  const hash = run.stats?.strategy_def_hash
  if (typeof hash !== 'string' || !hash) return null
  const strategyId = typeof run.config?.strategy_id === 'string' ? run.config.strategy_id : ''
  if (!strategyId) return null
  const current = strategies.find(st => st.id === strategyId)
  if (!current) return { kind: 'removed' }
  // 当前列表无指纹 (旧后端) → 无法比对, 不提示
  if (!current.def_hash) return null
  if (current.def_hash === hash) return null
  return { kind: 'changed', from: hash, to: current.def_hash }
}

const CONFIG_LABELS: Record<string, string> = {
  strategy_id: '策略',
  factor_name: '因子',
  start: '开始日期',
  end: '结束日期',
  symbols: '标的',
  params: '参数',
  overrides: '覆盖配置',
  fees_pct: '手续费率',
  slippage_bps: '滑点(bps)',
  matching: '成交匹配',
  entry_fill: '入场成交',
  exit_fill: '出场成交',
  max_positions: '最大持仓',
  initial_capital: '初始资金',
  position_sizing: '仓位分配',
  benchmark_symbol: '基准',
  risk_free_rate: '无风险年化',
  n_groups: '分组数',
  rebalance: '再平衡',
  weight: '权重方式',
  stop_loss_pct: '止损',
  max_hold_days: '最大持有天数',
  holding_days: '持有天数',
  regime_filter: '市场环境过滤',
  children: '子策略',
  merge_mode: '合并方式',
  min_confirm: '最少确认数',
}

function configDisplayEntries(config: Record<string, any>): [string, string][] {
  const entries: [string, string][] = []
  for (const [key, raw] of Object.entries(config)) {
    if (raw == null || raw === '') continue
    let text: string
    if (key === 'symbols' && Array.isArray(raw)) {
      text = raw.length === 0
        ? '全市场'
        : `${raw.length} 只：${raw.slice(0, 6).join(' ')}${raw.length > 6 ? ' …' : ''}`
    } else if (typeof raw === 'object') {
      text = JSON.stringify(raw)
    } else {
      text = String(raw)
    }
    entries.push([CONFIG_LABELS[key] ?? key, text])
  }
  return entries
}

/** 严格只接收有限 number 或非空 numeric string；null/boolean/array/object/空串一律拒绝，避免 Number(null)=0 之类垃圾指标 */
function coerceMetricScalar(raw: unknown): number | null {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null
  if (typeof raw === 'string') {
    const trimmed = raw.trim()
    if (!trimmed) return null
    const value = Number(trimmed)
    return Number.isFinite(value) ? value : null
  }
  return null
}

/** 详情指标网格：核心指标优先，其余数值指标随后；非数值(文本/数组)不展示 */
function numericStatEntries(stats: Record<string, any>): [string, number][] {
  const out = new Map<string, number>()
  for (const key of CORE_METRIC_ORDER) {
    const value = coerceMetricScalar(stats[key])
    if (value !== null) out.set(key, value)
  }
  for (const [key, raw] of Object.entries(stats)) {
    if (out.has(key)) continue
    const value = coerceMetricScalar(raw)
    if (value !== null) out.set(key, value)
  }
  return [...out.entries()]
}

function StatBox({ label, value, valueClass = 'text-foreground' }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-btn border border-border bg-elevated/40 px-3 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`metric-value mt-1 !text-sm ${valueClass}`}>{value}</div>
    </div>
  )
}

// ===== 指标矩阵 =====

function MetricMatrix({ comparison }: { comparison: BacktestRunComparison }) {
  const [showAll, setShowAll] = useState(false)
  const runOrder = comparison.runs.map(run => run.run_id)
  const metricKeys = Object.keys(comparison.metric_matrix)
  const coreKeys = CORE_METRIC_ORDER.filter(key => metricKeys.includes(key))
  const extraKeys = metricKeys.filter(key => !CORE_METRIC_ORDER.includes(key)).sort()
  const visibleKeys = showAll ? [...coreKeys, ...extraKeys] : coreKeys

  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="flex items-baseline justify-between gap-2 border-b border-border px-3 py-2">
        <div className="text-xs font-semibold text-foreground">指标矩阵</div>
        <div className="text-[10px] text-muted">首列为对比基线；其余列同时显示相对基线的 Δ</div>
      </div>
      <div className="data-table-scroll">
        <table
          className="data-table"
          style={{ minWidth: `${8 + comparison.runs.length * 10}rem` }}
        >
          <thead>
            <tr>
              <th>指标</th>
              {comparison.runs.map((run, index) => (
                <th key={run.run_id} className="text-right">
                  <span className="inline-flex max-w-[10rem] items-center justify-end gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: COMPARE_COLORS[index % COMPARE_COLORS.length] }}
                    />
                    <span className="truncate">{runDisplayName(run)}</span>
                  </span>
                  <span className="block font-mono text-[9px] font-normal text-muted">
                    {run.run_id.slice(0, 8)} · {KIND_LABELS[run.kind]}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleKeys.map(key => (
              <tr key={key}>
                <td className="whitespace-nowrap text-secondary">{metricLabel(key)}</td>
                {runOrder.map((runId, index) => {
                  const value = comparison.metric_matrix[key]?.[runId]
                  const baseline = comparison.metric_matrix[key]?.[runOrder[0]]
                  const delta = index > 0 && Number.isFinite(value) && Number.isFinite(baseline)
                    ? Number(value) - Number(baseline)
                    : null
                  return (
                    <td key={runId} className="text-right font-mono num">
                      <div className={metricValueClass(key, value)}>{formatMetricValue(key, value)}</div>
                      <div className="mt-0.5 text-[9px] text-muted">
                        {index === 0 ? '基线' : delta == null ? 'Δ —' : `Δ ${formatDeltaValue(key, delta)}`}
                      </div>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {extraKeys.length > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(value => !value)}
          className="flex w-full items-center justify-center gap-1 border-t border-border px-3 py-1.5 text-[11px] text-secondary transition-colors hover:bg-elevated/40"
        >
          {showAll ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          {showAll ? '收起扩展指标' : `展开其余 ${extraKeys.length} 项指标`}
        </button>
      )}
    </div>
  )
}

// ===== 归一化净值对比曲线 =====

interface PreparedCurve {
  runId: string
  name: string
  color: string
  data: [string, number][]
}

function CompareEquityChart({ comparison }: { comparison: BacktestRunComparison }) {
  const prepared = useMemo(() => {
    const summaryById = new Map(comparison.runs.map(run => [run.run_id, run]))
    const series: PreparedCurve[] = []
    const skipped: string[] = []
    comparison.curves.forEach((curve, index) => {
      const summary = summaryById.get(curve.run_id)
      const name = summary ? runDisplayName(summary) : curve.run_id
      const points = curve.equity_curve ?? []
      // 空曲线不伪造：因子 run / 旧 run_card 没有账户净值，直接标记跳过
      const first = points.length > 0 ? Number(points[0].value ?? points[0].equity) : NaN
      if (points.length === 0 || !Number.isFinite(first) || first === 0) {
        skipped.push(name)
        return
      }
      const data: [string, number][] = []
      for (const point of points) {
        const value = Number(point.value ?? point.equity)
        if (!point.date || !Number.isFinite(value)) continue
        data.push([String(point.date).slice(0, 10), value / first])
      }
      if (data.length === 0) {
        skipped.push(name)
        return
      }
      series.push({
        runId: curve.run_id,
        name,
        color: COMPARE_COLORS[index % COMPARE_COLORS.length],
        data,
      })
    })
    return { series, skipped }
  }, [comparison])

  const skippedNote = prepared.skipped.length > 0 && (
    <div className="mt-1 text-[10px] text-muted">
      {prepared.skipped.join('、')} 无账户净值曲线（因子/旧记录），未参与绘图。
    </div>
  )

  if (prepared.series.length === 0) {
    return (
      <div className="rounded-btn border border-border p-3">
        <div className="text-xs font-semibold text-foreground">归一化净值曲线</div>
        <div className="py-8 text-center text-[11px] text-muted">
          所选 run 均无账户净值曲线，无法绘制对比图；因子 run 的分层/多空曲线请经详情 JSON 导出查看。
        </div>
        {skippedNote}
      </div>
    )
  }
  return <CompareEquityChartCanvas prepared={prepared} skippedNote={skippedNote} />
}

function CompareEquityChartCanvas({ prepared, skippedNote }: { prepared: { series: PreparedCurve[] }; skippedNote: ReactNode }) {
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 44, right: 12, top: 12, bottom: 24 },
    xAxis: {
      type: 'time',
      axisLabel: { color: '#64748b', fontSize: 10 },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: '净值(起点=1)',
      nameTextStyle: { color: '#64748b', fontSize: 10 },
      axisLabel: { color: '#64748b', fontSize: 10, formatter: (v: number) => v.toFixed(2) },
      splitLine: { lineStyle: { color: '#1e293b' } },
      axisLine: { show: false },
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15,23,42,0.95)',
      borderColor: 'rgba(148,163,184,0.2)',
      textStyle: { color: '#e2e8f0', fontSize: 12 },
      formatter: (params: any) => {
        const list = Array.isArray(params) ? params : [params]
        const date = list[0]?.value?.[0] ?? ''
        let html = `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">${date}</div>`
        for (const item of list) {
          const value = Array.isArray(item.value) ? item.value[1] : item.value
          if (value == null || !Number.isFinite(value)) continue
          html += `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${item.color}">${item.seriesName}</span><span style="font-family:monospace">${Number(value).toFixed(3)}</span></div>`
        }
        return html
      },
    },
    series: prepared.series.map(curve => ({
      name: curve.name,
      type: 'line' as const,
      showSymbol: false,
      data: curve.data,
      lineStyle: { width: 1.6, color: curve.color },
      itemStyle: { color: curve.color },
      emphasis: { focus: 'series' as const },
    })),
  }), [prepared])

  const chartRef = useECharts(option, [prepared])

  return (
    <div className="rounded-btn border border-border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
        <div className="text-xs font-semibold text-foreground">归一化净值曲线</div>
        <div className="text-[10px] text-muted">各 run 首日均归一为 1.0，仅比较相对走势，不代表真实资金</div>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pb-1 pt-2">
        {prepared.series.map(curve => (
          <span key={curve.runId} className="flex min-w-0 items-center gap-1.5 text-[10px] text-secondary">
            <span className="h-0.5 w-3 shrink-0 rounded" style={{ background: curve.color }} />
            <span className="max-w-[10rem] truncate">{curve.name}</span>
            <span className="font-mono text-muted">{curve.runId.slice(0, 6)}</span>
          </span>
        ))}
      </div>
      <div ref={chartRef} className="h-[260px]" />
      {skippedNote}
    </div>
  )
}

// ===== 配置差异 / 交易变化 (相对 baseline) =====

const CONFIG_DIFF_OP_META: Record<string, { label: string; cls: string }> = {
  added: { label: '新增', cls: 'text-bull' },
  removed: { label: '移除', cls: 'text-danger' },
  changed: { label: '修改', cls: 'text-warning' },
}

function ConfigDiffSection({ comparison }: { comparison: BacktestRunComparison }) {
  const diff = comparison.config_diff
  if (!diff) return null
  const summaryById = new Map(comparison.runs.map(run => [run.run_id, run]))
  const baseline = summaryById.get(diff.baseline_run_id)
  return (
    <div className="rounded-btn border border-border">
      <div className="flex items-baseline justify-between gap-2 border-b border-border px-3 py-2">
        <div className="text-xs font-semibold text-foreground">配置差异</div>
        <div className="text-[10px] text-muted">
          基线：{baseline ? runDisplayName(baseline) : diff.baseline_run_id.slice(0, 8)}
        </div>
      </div>
      <div className="divide-y divide-border">
        {diff.candidates.map(candidate => {
          const run = summaryById.get(candidate.run_id)
          return (
            <div key={candidate.run_id} className="px-3 py-2">
              <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                <div className="text-[11px] font-medium text-secondary">
                  {run ? runDisplayName(run) : candidate.run_id.slice(0, 8)}
                </div>
                <div className="text-[10px] text-muted">
                  {candidate.total === 0 ? '与基线配置一致' : `共 ${candidate.total} 项差异`}
                </div>
              </div>
              {candidate.total === 0 ? (
                <div className="text-[11px] text-muted">配置完全一致，无差异项。</div>
              ) : (
                <div className="space-y-1">
                  <div className="data-table-scroll">
                    <table className="data-table min-w-[30rem]">
                      <thead>
                        <tr>
                          <th>配置项</th>
                          <th>变化</th>
                          <th className="text-right">基线值</th>
                          <th className="text-right">对比值</th>
                        </tr>
                      </thead>
                      <tbody>
                        {candidate.entries.map((entry, index) => {
                          const meta = CONFIG_DIFF_OP_META[entry.op]
                          return (
                            <tr key={`${entry.path}|${entry.op}|${index}`}>
                              <td className="whitespace-nowrap font-mono text-[11px] text-secondary">{entry.path}</td>
                              <td className={`whitespace-nowrap text-[11px] ${meta?.cls ?? ''}`}>
                                {meta?.label ?? entry.op}
                              </td>
                              <td
                                className="max-w-[16rem] truncate text-right font-mono text-[11px] text-muted"
                                title={formatDiffValue(entry.before)}
                              >
                                {formatDiffValue(entry.before)}
                              </td>
                              <td
                                className="max-w-[16rem] truncate text-right font-mono text-[11px]"
                                title={formatDiffValue(entry.after)}
                              >
                                {formatDiffValue(entry.after)}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                  {candidate.truncated && (
                    <div className="text-[10px] text-muted">
                      差异较多，仅展示前 {candidate.entries.length} 项（共 {candidate.total} 项）。
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TradeSampleTable({ title, rows, tone }: { title: string; rows: BacktestRunTradeSample[]; tone: 'bull' | 'danger' }) {
  if (rows.length === 0) return null
  const toneCls = tone === 'bull' ? 'text-bull' : 'text-danger'
  return (
    <div>
      <div className={`text-[10px] font-medium ${toneCls}`}>{title}</div>
      <div className="data-table-scroll">
        <table className="data-table min-w-[26rem]">
          <thead>
            <tr>
              <th>标的</th>
              <th>入场</th>
              <th>出场</th>
              <th className="text-right">份额</th>
              <th className="text-right">入场金额</th>
              <th className="text-right">收益</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${row.symbol}|${row.entry_date}|${row.exit_date}|${index}`}>
                <td className="font-mono text-[11px]">{row.symbol ?? '—'}</td>
                <td className="whitespace-nowrap text-secondary">{row.entry_date ?? '—'}</td>
                <td className="whitespace-nowrap text-secondary">{row.exit_date ?? '—'}</td>
                <td className="text-right font-mono num">{fmtNumOrDash(row.shares)}</td>
                <td className="text-right font-mono num">{fmtNumOrDash(row.entry_value)}</td>
                <td className={`text-right font-mono num ${priceColorClass(row.pnl_pct ?? 0)}`}>{fmtPctOrDash(row.pnl_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TradeChangeSection({ comparison }: { comparison: BacktestRunComparison }) {
  const summary = comparison.trade_summary
  if (!summary) return null
  const summaryById = new Map(comparison.runs.map(run => [run.run_id, run]))
  const baseline = summaryById.get(summary.baseline_run_id)
  const noTrades = summary.baseline_n_trades === 0 && summary.candidates.every(c => c.n_trades === 0)
  return (
    <div className="rounded-btn border border-border">
      <div className="flex items-baseline justify-between gap-2 border-b border-border px-3 py-2">
        <div className="text-xs font-semibold text-foreground">交易变化</div>
        <div className="text-[10px] text-muted">
          基线 {baseline ? runDisplayName(baseline) : summary.baseline_run_id.slice(0, 8)} · {summary.baseline_n_trades} 笔 · 共同 = 相同(标的, 入场日, 出场日)
        </div>
      </div>
      {noTrades ? (
        <div className="px-3 py-4 text-center text-[11px] text-muted">
          所选 run 均无交易明细（因子 run / 旧记录），无可比较的交易变化。
        </div>
      ) : (
        <div className="divide-y divide-border">
          {summary.candidates.map(candidate => {
            const run = summaryById.get(candidate.run_id)
            return (
              <div key={candidate.run_id} className="space-y-1.5 px-3 py-2">
                <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
                  <div className="text-[11px] font-medium text-secondary">
                    {run ? runDisplayName(run) : candidate.run_id.slice(0, 8)}
                    <span className="ml-1.5 font-mono text-[9px] text-muted">{candidate.n_trades} 笔</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-secondary">
                    <span>共同 <b className="font-mono num">{candidate.common}</b></span>
                    {candidate.common_value_diff > 0 && (
                      <span className="text-warning">份额/金额不同 <b className="font-mono num">{candidate.common_value_diff}</b></span>
                    )}
                    <span className="text-bull">新增 <b className="font-mono num">{candidate.added}</b></span>
                    <span className="text-danger">消失 <b className="font-mono num">{candidate.removed}</b></span>
                  </div>
                </div>
                {candidate.samples.common.length > 0 && (
                  <div>
                    <div className="text-[10px] font-medium text-secondary">共同样本（数值不同优先）</div>
                    <div className="data-table-scroll">
                      <table className="data-table min-w-[52rem]">
                        <thead>
                          <tr>
                            <th>标的</th>
                            <th>入场</th>
                            <th>出场</th>
                            <th className="text-right">基线份额</th>
                            <th className="text-right">对比份额</th>
                            <th className="text-right">基线入场额</th>
                            <th className="text-right">对比入场额</th>
                            <th className="text-right">基线出场额</th>
                            <th className="text-right">对比出场额</th>
                            <th className="text-right">基线收益</th>
                            <th className="text-right">对比收益</th>
                          </tr>
                        </thead>
                        <tbody>
                          {candidate.samples.common.map((row, index) => (
                            <tr
                              key={`${row.symbol}|${row.entry_date}|${row.exit_date}|${index}`}
                              className={row.value_differs ? 'bg-warning/5' : undefined}
                            >
                              <td className="font-mono text-[11px]">{row.symbol ?? '—'}</td>
                              <td className="whitespace-nowrap text-secondary">{row.entry_date ?? '—'}</td>
                              <td className="whitespace-nowrap text-secondary">{row.exit_date ?? '—'}</td>
                              <td className="text-right font-mono num">{fmtNumOrDash(row.baseline.shares)}</td>
                              <td className={`text-right font-mono num ${row.value_differs ? 'text-warning' : ''}`}>{fmtNumOrDash(row.candidate.shares)}</td>
                              <td className="text-right font-mono num">{fmtNumOrDash(row.baseline.entry_value)}</td>
                              <td className={`text-right font-mono num ${row.value_differs ? 'text-warning' : ''}`}>{fmtNumOrDash(row.candidate.entry_value)}</td>
                              <td className="text-right font-mono num">{fmtNumOrDash(row.baseline.exit_value)}</td>
                              <td className={`text-right font-mono num ${row.value_differs ? 'text-warning' : ''}`}>{fmtNumOrDash(row.candidate.exit_value)}</td>
                              <td className={`text-right font-mono num ${priceColorClass(row.baseline.pnl_pct ?? 0)}`}>{fmtPctOrDash(row.baseline.pnl_pct)}</td>
                              <td className={`text-right font-mono num ${priceColorClass(row.candidate.pnl_pct ?? 0)}`}>{fmtPctOrDash(row.candidate.pnl_pct)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
                <TradeSampleTable title="新增交易（相对基线）" rows={candidate.samples.added} tone="bull" />
                <TradeSampleTable title="消失交易（相对基线）" rows={candidate.samples.removed} tone="danger" />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ===== 详情抽屉 =====

interface RunDetailDrawerProps {
  runId: string
  onClose: () => void
  onToggleFavorite: (run: Pick<BacktestRunSummary, 'run_id' | 'favorite'>) => void
  onRerun: (runId: string) => void
  rerunning: boolean
  onDelete: (runId: string, displayName: string) => void
  deleting: boolean
}

function RunDetailDrawer({ runId, onClose, onToggleFavorite, onRerun, rerunning, onDelete, deleting }: RunDetailDrawerProps) {
  const [reportDownloading, setReportDownloading] = useState(false)
  const detailQuery = useQuery({
    queryKey: [...RUNS_KEY, 'detail', runId],
    queryFn: () => api.backtestRunGet(runId),
  })
  const run = detailQuery.data
  const [converting, setConverting] = useState(false)

  /** F10 转监控规则: HTTP 错误已由 api.request 统一 toast, 此处只补网络层失败反馈 */
  const handleToMonitorRule = async () => {
    if (!run || converting) return
    setConverting(true)
    try {
      const { rule, created } = await api.toMonitorRule(run.run_id)
      toast(
        created ? `已创建监控规则「${rule.name}」` : `已存在同名规则「${rule.name}」`,
        'success',
        { label: '去监控中心', href: '/monitor' },
      )
    } catch (error) {
      if (error instanceof TypeError) toast('转监控失败：网络异常，请稍后重试', 'error')
    } finally {
      setConverting(false)
    }
  }

  const handleDownloadReport = async () => {
    if (!run || reportDownloading) return
    setReportDownloading(true)
    try {
      // 让出一帧，保证 loading 状态先渲染（大报告生成可能同步阻塞）
      await Promise.resolve()
      downloadRunReportHtml(run)
    } finally {
      setReportDownloading(false)
    }
  }

  // F13 策略版本感知: 拉取当前策略列表, 与 Run 持久化的定义指纹比对
  const strategiesQuery = useQuery({
    queryKey: ['run-detail-strategies'],
    queryFn: api.strategyList,
    staleTime: 30_000,
  })
  const defHashBanner = defHashBannerOf(run, strategiesQuery.data?.strategies)

  return (
    <div className="fixed inset-0 z-[70] flex justify-end" role="dialog" aria-label="运行详情">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <motion.aside
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        exit={{ x: '100%' }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative flex h-full w-full max-w-full flex-col border-l border-border bg-base shadow-2xl sm:max-w-md"
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {run ? (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={`rounded border px-1 py-px text-[10px] ${KIND_BADGE_CLS[run.kind]}`}>{KIND_LABELS[run.kind]}</span>
                  <span className="truncate text-sm font-semibold text-foreground">{runDisplayName(run)}</span>
                  <button
                    type="button"
                    onClick={() => onToggleFavorite(run)}
                    aria-pressed={run.favorite}
                    aria-label={run.favorite ? '取消收藏' : '收藏'}
                    className={`rounded-btn p-1 transition-colors hover:bg-elevated ${run.favorite ? 'text-warning' : 'text-muted hover:text-foreground'}`}
                  >
                    <Star className={`h-3.5 w-3.5 ${run.favorite ? 'fill-current' : ''}`} />
                  </button>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted">
                  <span className="font-mono">{run.run_id}</span>
                  <span>{fmtDateTime(run.created_at)}</span>
                  {run.source_run_id && <span className="font-mono">复跑自 {run.source_run_id}</span>}
                </div>
              </>
            ) : (
              <div className="text-sm text-muted">正在读取运行详情…</div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭详情"
            className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {detailQuery.isPending && (
            <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />正在读取完整运行记录…
            </div>
          )}
          {detailQuery.isError && (
            <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
              {detailQuery.error instanceof Error ? detailQuery.error.message : '读取运行详情失败'}
            </div>
          )}
          {run && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <a href={api.backtestRunExportUrl(run.run_id, 'json')} download className="btn-secondary !h-7 px-2.5 text-[11px]">
                  <FileJson className="h-3 w-3" />导出 JSON
                </a>
                {(run.trades.length > 0 || (Array.isArray(run.factor_result?.group_stats) && run.factor_result.group_stats.length > 0)) && (
                  <a href={api.backtestRunExportUrl(run.run_id, 'csv')} download className="btn-secondary !h-7 px-2.5 text-[11px]">
                    <FileSpreadsheet className="h-3 w-3" />导出 CSV
                  </a>
                )}
                <button
                  type="button"
                  onClick={() => { void handleDownloadReport() }}
                  disabled={reportDownloading}
                  aria-busy={reportDownloading}
                  aria-label={reportDownloading ? '报告生成中' : '下载报告'}
                  className="btn-secondary !h-7 px-2.5 text-[11px]"
                >
                  {reportDownloading ? <Loader2 className="h-3 w-3 animate-spin" /> : <FileDown className="h-3 w-3" />}
                  {reportDownloading ? '生成中…' : '下载报告'}
                </button>
                <button
                  type="button"
                  onClick={() => onRerun(run.run_id)}
                  disabled={rerunning}
                  className="btn-secondary !h-7 px-2.5 text-[11px]"
                >
                  {rerunning ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
                  {rerunning ? '复跑中…' : '复跑'}
                </button>
                {run.kind === 'strategy' && (
                  <button
                    type="button"
                    onClick={() => { void handleToMonitorRule() }}
                    disabled={converting}
                    aria-busy={converting}
                    aria-label={converting ? '正在转为监控规则' : '转为监控规则'}
                    title="把该次回测的策略与股票池配置存为一条策略监控规则"
                    className="btn-secondary !h-7 px-2.5 text-[11px]"
                  >
                    {converting ? <Loader2 className="h-3 w-3 animate-spin" /> : <RadioTower className="h-3 w-3" />}
                    {converting ? '转换中…' : '转为监控规则'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onDelete(run.run_id, runDisplayName(run))}
                  disabled={deleting}
                  className="ml-auto inline-flex items-center gap-1 rounded-btn border border-danger/40 bg-danger/10 px-2.5 py-1 text-[11px] text-danger transition-colors hover:bg-danger/20 disabled:opacity-50"
                >
                  {deleting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                  删除
                </button>
              </div>

              {defHashBanner?.kind === 'changed' && (
                <div role="status" className="flex items-start gap-2 rounded-btn border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>
                    策略定义已变更（回测时 <b className="font-mono">{shortDefHash(defHashBanner.from)}</b> → 当前{' '}
                    <b className="font-mono">{shortDefHash(defHashBanner.to)}</b>），复跑将使用当前定义
                  </span>
                </div>
              )}
              {defHashBanner?.kind === 'removed' && (
                <div role="alert" className="flex items-start gap-2 rounded-btn border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>策略已不存在，复跑将无法按原定义执行</span>
                </div>
              )}

              {run.status !== 'completed' && (
                <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                  该运行状态为 {run.status === 'failed' ? '失败' : run.status}，指标可能不完整。
                </div>
              )}

              <BacktestWarnings warnings={run.warnings} dataSnapshot={run.data_snapshot as BacktestDataSnapshot} />

              {numericStatEntries(run.stats).length > 0 && (
                <section>
                  <div className="section-kicker mb-2">核心指标</div>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {numericStatEntries(run.stats).map(([key, value]) => (
                      <StatBox key={key} label={metricLabel(key)} value={formatMetricValue(key, value)} valueClass={metricValueClass(key, value)} />
                    ))}
                  </div>
                </section>
              )}

              {run.factor_result != null && (
                <section>
                  <div className="section-kicker mb-2">因子指标</div>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {numericStatEntries(run.factor_result).map(([key, value]) => (
                      <StatBox key={key} label={metricLabel(key)} value={formatMetricValue(key, value)} valueClass={metricValueClass(key, value)} />
                    ))}
                  </div>
                  <p className="mt-2 text-[10px] leading-4 text-muted">
                    IC 序列、分层净值与换手等序列数据请通过「导出 JSON」查看。
                  </p>
                </section>
              )}

              <section>
                <div className="section-kicker mb-2">规模</div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-secondary">
                  <span>交易 <b className="font-mono num text-foreground">{run.trades.length}</b> 笔</span>
                  <span>净值点 <b className="font-mono num text-foreground">{run.equity_curve.length}</b></span>
                  <span>分标的统计 <b className="font-mono num text-foreground">{run.per_symbol_stats.length}</b></span>
                </div>
              </section>

              <section>
                <div className="section-kicker mb-2">配置</div>
                <dl className="space-y-1.5">
                  {configDisplayEntries(run.config).map(([label, text]) => (
                    <div key={label} className="flex items-baseline justify-between gap-3 text-[11px]">
                      <dt className="shrink-0 text-muted">{label}</dt>
                      <dd className="min-w-0 break-all text-right font-mono text-secondary">{text}</dd>
                    </div>
                  ))}
                  {configDisplayEntries(run.config).length === 0 && (
                    <div className="text-[11px] text-muted">无配置记录</div>
                  )}
                </dl>
                {(run.cost_model && Object.keys(run.cost_model).length > 0) || run.benchmark?.symbol || run.engine_version || (run.metric_context && Object.keys(run.metric_context).length > 0) ? (
                  <div className="mt-3 space-y-1.5 border-t border-border pt-3">
                    {run.benchmark?.symbol && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">基准</span>
                        <span className="font-mono text-secondary">{run.benchmark.name ? `${run.benchmark.name} ` : ''}{run.benchmark.symbol}</span>
                      </div>
                    )}
                    {Object.entries(run.cost_model ?? {}).map(([key, value]) => (
                      <div key={key} className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">{CONFIG_LABELS[key] ?? key}</span>
                        <span className="font-mono text-secondary">{String(value)}</span>
                      </div>
                    ))}
                    {run.engine_version && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">引擎版本</span>
                        <span className="font-mono text-secondary">{run.engine_version}</span>
                      </div>
                    )}
                    {(run.metric_context as Record<string, any>)?.version && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">指标口径版本</span>
                        <span className="font-mono text-secondary">{String((run.metric_context as Record<string, any>).version)}</span>
                      </div>
                    )}
                    {(run.metric_context as Record<string, any>)?.return_frequency && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">收益频率</span>
                        <span className="font-mono text-secondary">
                          {String((run.metric_context as Record<string, any>).return_frequency)}
                          {' · '}
                          {String((run.metric_context as Record<string, any>).periods_per_year ?? '—')} 期/年
                        </span>
                      </div>
                    )}
                    {Number.isFinite(Number((run.metric_context as Record<string, any>)?.risk_free_rate)) && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">无风险年化</span>
                        <span className="font-mono text-secondary">{fmtPct(Number((run.metric_context as Record<string, any>).risk_free_rate))}</span>
                      </div>
                    )}
                    {run.random_seed != null && (
                      <div className="flex items-baseline justify-between gap-3 text-[11px]">
                        <span className="text-muted">随机种子</span>
                        <span className="font-mono text-secondary">{run.random_seed}</span>
                      </div>
                    )}
                  </div>
                ) : null}
              </section>
            </div>
          )}
        </div>
      </motion.aside>
    </div>
  )
}

// ===== 主面板 =====

export function RunHistoryPanel({ onOpenExperiment }: { onOpenExperiment?: (kind: BacktestExperimentSummary['kind']) => void }) {
  const queryClient = useQueryClient()
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<BacktestRunKind | ''>('')
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [limit, setLimit] = useState(PAGE_SIZE)
  const [selected, setSelected] = useState<string[]>([])
  const [comparingIds, setComparingIds] = useState<string[] | null>(null)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [editingLabelId, setEditingLabelId] = useState<string | null>(null)
  const [labelDraft, setLabelDraft] = useState('')
  const [reportDownloadingId, setReportDownloadingId] = useState<string | null>(null)

  const [experimentsOpen, setExperimentsOpen] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(searchInput.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [searchInput])

  // 筛选变化后回到第一页
  useEffect(() => {
    setLimit(PAGE_SIZE)
  }, [kind, favoriteOnly, query])

  const listQuery = useQuery({
    queryKey: [...RUNS_KEY, 'list', { kind, favoriteOnly, query, limit }],
    queryFn: () => api.backtestRuns({
      kind: kind || undefined,
      favorite: favoriteOnly || undefined,
      query: query || undefined,
      limit,
    }),
    placeholderData: (prev) => prev,
  })
  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const hasFilters = Boolean(kind || favoriteOnly || query)

  const compareQuery = useQuery({
    queryKey: [...RUNS_KEY, 'compare', comparingIds],
    queryFn: () => api.backtestRunsCompare(comparingIds!),
    enabled: comparingIds != null && comparingIds.length >= 2,
  })
  const comparison = comparingIds != null ? compareQuery.data ?? null : null


  // F7 实验区: 默认折叠, 展开时才拉取 (寻优/参数网格统一摘要)
  const experimentsQuery = useQuery({
    queryKey: ['backtest-experiments'],
    queryFn: () => api.backtestExperiments(),
    enabled: experimentsOpen,
    staleTime: 30_000,
  })
  const experiments = experimentsQuery.data?.items ?? []

  /** 打开实验: 写对应恢复键 + 通知父级切换到对应 tab; 详情已落盘, 服务重启后仍可恢复 */
  const openExperiment = (row: BacktestExperimentSummary) => {
    if (row.kind === 'optimizer') openOptimizerExperiment(row.id)
    else openParameterGridExperiment(row.id)
    onOpenExperiment?.(row.kind)
  }
  const invalidateRuns = () => queryClient.invalidateQueries({ queryKey: RUNS_KEY })

  /** 乐观更新列表缓存中的 favorite/label，refetch 后以后端为准 */
  const patchSummaryCaches = (runId: string, patch: Partial<Pick<BacktestRunSummary, 'favorite' | 'label'>>) => {
    queryClient.setQueriesData<BacktestRunListResponse>({ queryKey: RUNS_KEY }, (old) => {
      if (!old || !Array.isArray(old.items)) return old
      return {
        ...old,
        items: old.items.map(item => (item.run_id === runId ? { ...item, ...patch } : item)),
      }
    })
  }

  const patchRun = useMutation({
    mutationFn: ({ runId, body }: { runId: string; body: { favorite?: boolean; label?: string } }) =>
      api.backtestRunPatch(runId, body),
    onSettled: invalidateRuns,
    // HTTP 错误已由 api.request 统一 toast；此处只补网络层失败(fetch 抛 TypeError)的可见反馈
    onError: (error) => {
      if (error instanceof TypeError) toast('保存失败：网络异常，请稍后重试', 'error')
    },
  })

  const deleteRun = useMutation({
    mutationFn: (runId: string) => api.backtestRunDelete(runId),
    onSuccess: (_data, runId) => {
      toast('已删除该运行记录', 'success')
      setSelected(prev => prev.filter(id => id !== runId))
      setComparingIds(prev => (prev && prev.includes(runId) ? null : prev))
      setDetailId(prev => (prev === runId ? null : prev))
      invalidateRuns()
    },
    // HTTP 错误(如旧 run_card 只读 403)已由 api.request 统一 toast；此处只补网络层失败(fetch 抛 TypeError)的可见反馈
    onError: (error) => {
      if (error instanceof TypeError) toast('删除失败：网络异常，请稍后重试', 'error')
    },
  })

  const rerunRun = useMutation({
    mutationFn: (runId: string) => api.backtestRunRerun(runId),
    onSuccess: (newRun, runId) => {
      toast(`复跑完成，已生成新记录 ${newRun.run_id}`, 'success')
      invalidateRuns()
      if (detailId === runId) setDetailId(newRun.run_id)
    },
    // 同 deleteRun：HTTP 错误已由 api.request toast，仅补网络层失败反馈
    onError: (error) => {
      if (error instanceof TypeError) toast('复跑失败：网络异常，请稍后重试', 'error')
    },
  })

  const downloadListReport = async (runId: string, event?: { stopPropagation: () => void }) => {
    event?.stopPropagation()
    if (reportDownloadingId) return
    setReportDownloadingId(runId)
    try {
      const full = await api.backtestRunGet(runId)
      downloadRunReportHtml(full)
    } finally {
      setReportDownloadingId(null)
    }
  }

  const downloadCompareReport = () => {
    if (!comparison) return
    downloadCompareReportHtml(comparison)
  }

  const toggleFavorite = (run: Pick<BacktestRunSummary, 'run_id' | 'favorite'>) => {
    patchSummaryCaches(run.run_id, { favorite: !run.favorite })
    patchRun.mutate({ runId: run.run_id, body: { favorite: !run.favorite } })
  }

  const startEditLabel = (run: BacktestRunSummary) => {
    setEditingLabelId(run.run_id)
    setLabelDraft(run.label)
  }

  const commitLabel = (run: BacktestRunSummary) => {
    const label = labelDraft.trim()
    setEditingLabelId(null)
    if (label === run.label) return
    patchSummaryCaches(run.run_id, { label })
    patchRun.mutate({ runId: run.run_id, body: { label } })
  }

  /** 删除必须先经用户显式确认，绝不自动执行 */
  const requestDelete = (runId: string, displayName: string) => {
    if (!window.confirm(`确认删除运行记录「${displayName}」(${runId})？此操作不可恢复。`)) return
    deleteRun.mutate(runId)
  }

  const toggleSelect = (runId: string) => {
    setSelected(prev => {
      if (prev.includes(runId)) return prev.filter(id => id !== runId)
      if (prev.length >= MAX_COMPARE) {
        toast(`最多同时对比 ${MAX_COMPARE} 个运行`)
        return prev
      }
      return [...prev, runId]
    })
  }

  const summaryById = useMemo(() => new Map(items.map(item => [item.run_id, item])), [items])
  const selectedName = (runId: string) => {
    const summary = summaryById.get(runId)
    return summary ? runDisplayName(summary) : runId.slice(0, 8)
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-3">
      <section className="panel shrink-0" aria-label="研究实验">
        <div className="panel-header !py-2">
          <div>
            <div className="section-kicker">Experiments</div>
            <h2 className="section-title">实验</h2>
          </div>
          <button
            type="button"
            onClick={() => setExperimentsOpen(prev => !prev)}
            aria-expanded={experimentsOpen}
            className="btn-secondary !h-7 inline-flex items-center gap-1 px-2.5 text-[11px]"
          >
            {experimentsOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            {experimentsOpen ? '收起' : `展开 (${experimentsQuery.data?.total ?? 0})`}
          </button>
        </div>
        {experimentsOpen && (
          <div className="panel-body space-y-2 border-t border-border">
            {experimentsQuery.isPending && (
              <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted">
                <Loader2 className="h-4 w-4 animate-spin" />正在载入实验列表…
              </div>
            )}
            {experimentsQuery.isError && (
              <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                {experimentsQuery.error instanceof Error ? experimentsQuery.error.message : '实验列表载入失败'}
              </div>
            )}
            {experimentsQuery.data?.warnings.map(warning => (
              <div key={warning} className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-1.5 text-[11px] text-secondary">
                {warning}
              </div>
            ))}
            {!experimentsQuery.isPending && experiments.length === 0 && (
              <p className="py-4 text-center text-xs text-muted">还没有寻优或参数网格实验记录</p>
            )}
            {experiments.length > 0 && (
              <div className="max-h-64 overflow-y-auto">
                <table className="data-table min-w-[48rem]">
                  <thead>
                    <tr><th>类型</th><th>标题</th><th>时间</th><th className="text-right">场景</th><th className="text-right">固化 Run</th><th>状态</th><th>最佳摘要</th><th></th></tr>
                  </thead>
                  <tbody>
                    {experiments.map(row => {
                      const meta = EXPERIMENT_KIND_META[row.kind]
                      const KindIcon = meta.Icon
                      return (
                        <tr key={`${row.kind}-${row.id}`}>
                          <td>
                            <span className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}>
                              <KindIcon className="h-3 w-3" />
                              {meta.label}
                            </span>
                          </td>
                          <td className="max-w-[18rem] truncate" title={row.title}>{row.title}</td>
                          <td className="whitespace-nowrap font-mono text-[10px] text-muted">{fmtDateTime(row.created_at)}</td>
                          <td className="text-right font-mono num">{row.scenario_count}</td>
                          <td className={`text-right font-mono num ${row.run_count > 0 ? 'text-accent' : 'text-muted'}`}>{row.run_count > 0 ? row.run_count : '—'}</td>
                          <td className="whitespace-nowrap text-[11px] text-secondary">{EXPERIMENT_STATUS_LABELS[row.status]}</td>
                          <td className="max-w-[22rem] truncate text-[11px] text-secondary" title={experimentBestLine(row)}>{experimentBestLine(row)}</td>
                          <td>
                            <button
                              type="button"
                              className="text-[10px] text-accent"
                              title={row.persisted ? '写入恢复键并切换到对应实验面板' : '实验详情可能已过期'}
                              onClick={() => openExperiment(row)}
                            >
                              打开
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>
      {comparingIds != null && (
        <section className="panel flex max-h-[75vh] min-h-0 shrink-0 flex-col">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Compare</div>
              <h2 className="section-title">
                运行对比 <span className="text-[11px] font-normal text-muted">{comparingIds.length} 个运行</span>
              </h2>
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={downloadCompareReport}
                disabled={!comparison}
                title={comparison ? '导出自包含 HTML 对比报告（元信息/指标矩阵/配置差异/交易变化/净值曲线）' : '对比数据载入完成后可导出报告'}
                aria-disabled={!comparison}
                className="btn-secondary !h-7 inline-flex items-center gap-1 px-2.5 text-[11px]"
              >
                <FileDown className="h-3.5 w-3.5" />
                导出对比报告
              </button>
              <button
                type="button"
                onClick={() => setComparingIds(null)}
                aria-label="关闭对比"
                className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          <div className="panel-body space-y-3 overflow-y-auto">
            {compareQuery.isPending && (
              <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted">
                <Loader2 className="h-4 w-4 animate-spin" />正在载入对比数据…
              </div>
            )}
            {compareQuery.isError && (
              <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                {compareQuery.error instanceof Error ? compareQuery.error.message : '对比数据载入失败'}
              </div>
            )}
            {comparison && (
              <>
                {comparison.warnings.length > 0 && (
                  <div className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-5 text-secondary">
                    <div className="mb-0.5 flex items-center gap-1.5 font-medium text-warning">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      可比性提醒
                    </div>
                    <ul className="list-disc space-y-0.5 pl-5">
                      {comparison.warnings.map(warning => <li key={warning}>{compareWarningLabel(warning)}</li>)}
                    </ul>
                  </div>
                )}
                <MetricMatrix comparison={comparison} />
                <ConfigDiffSection comparison={comparison} />
                <TradeChangeSection comparison={comparison} />
                <CompareEquityChart comparison={comparison} />
              </>
            )}
          </div>
        </section>
      )}

      <section className="panel flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="panel-header">
          <div>
            <div className="section-kicker">History</div>
            <h2 className="section-title">运行历史</h2>
          </div>
          <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
            {listQuery.isFetching && !listQuery.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            共 <b className="font-mono text-secondary num">{total}</b> 条
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
          <div className="relative w-full min-w-0 sm:w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input
              value={searchInput}
              onChange={event => setSearchInput(event.target.value)}
              placeholder="搜索名称 / 标签 / run id"
              aria-label="搜索运行历史"
              className="control w-full pl-7 text-xs"
            />
          </div>
          <div className="inline-flex rounded-btn border border-border bg-elevated p-0.5" role="group" aria-label="类型筛选">
            {(['', 'strategy', 'factor', 'composite'] as const).map(value => {
              const active = kind === value
              return (
                <button
                  key={value || 'all'}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setKind(value)}
                  className={`cursor-pointer rounded-[6px] px-2 py-1 text-[11px] font-medium transition-colors duration-150 ${
                    active ? 'bg-accent text-white shadow-sm' : 'text-secondary hover:bg-surface hover:text-foreground'
                  }`}
                >
                  {value === '' ? '全部' : KIND_LABELS[value]}
                </button>
              )
            })}
          </div>
          <button
            type="button"
            aria-pressed={favoriteOnly}
            onClick={() => setFavoriteOnly(value => !value)}
            className={`inline-flex items-center gap-1 rounded-btn border px-2 py-1 text-[11px] font-medium transition-colors ${
              favoriteOnly
                ? 'border-warning/40 bg-warning/10 text-warning'
                : 'border-border bg-elevated text-secondary hover:text-foreground'
            }`}
          >
            <Star className={`h-3 w-3 ${favoriteOnly ? 'fill-current' : ''}`} />
            只看收藏
          </button>
        </div>

        {selected.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-border bg-elevated/30 px-3 py-2">
            <span className="text-[11px] text-secondary">
              对比 <b className="font-mono text-foreground num">{selected.length}</b>/{MAX_COMPARE}
            </span>
            <div className="flex min-w-0 flex-1 flex-wrap gap-1">
              {selected.map(runId => (
                <span key={runId} className="inline-flex max-w-[12rem] items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] text-accent">
                  <span className="truncate">{selectedName(runId)}</span>
                  <button
                    type="button"
                    onClick={() => toggleSelect(runId)}
                    aria-label={`移除 ${selectedName(runId)}`}
                    className="shrink-0 rounded-full hover:text-foreground"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
            <button
              type="button"
              disabled={selected.length < 2}
              onClick={() => setComparingIds([...selected])}
              className="btn-primary !h-7 px-2.5 text-[11px]"
            >
              开始对比
            </button>
            <button
              type="button"
              onClick={() => setSelected([])}
              className="rounded-btn px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-secondary"
            >
              清空
            </button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {listQuery.isPending && (
            <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />正在读取运行历史…
            </div>
          )}
          {listQuery.isError && (
            <div className="p-3">
              <div role="alert" className="flex flex-wrap items-center gap-2 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                <span className="min-w-0 flex-1">{listQuery.error instanceof Error ? listQuery.error.message : '运行历史读取失败'}</span>
                <button type="button" onClick={() => { void listQuery.refetch() }} className="btn-secondary !h-7 px-2.5 text-[11px]">重试</button>
              </div>
            </div>
          )}
          {listQuery.isSuccess && items.length === 0 && (
            <EmptyState
              icon={History}
              title={hasFilters ? '没有符合筛选条件的运行' : '还没有持久化的回测运行'}
              hint={hasFilters
                ? '调整搜索词、类型或收藏筛选后重试。'
                : '在策略 / 因子回测中完成一次运行后会自动保存到这里，可收藏、打标签、对比与复跑。'}
            />
          )}
          {items.length > 0 && (
            <div>
              {items.map(run => {
                const isSelected = selected.includes(run.run_id)
                const headline = headlineStats(run.stats)
                const csvExportable = run.has_csv_export
                return (
                  <div key={run.run_id} className="flex items-start gap-2 border-b border-border px-3 py-2.5 last:border-b-0">
                    <button
                      type="button"
                      onClick={() => toggleSelect(run.run_id)}
                      aria-pressed={isSelected}
                      aria-label={`选择 ${runDisplayName(run)} 参与对比`}
                      className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
                        isSelected ? 'border-accent bg-accent text-white' : 'border-border bg-elevated text-transparent hover:border-accent/50'
                      }`}
                    >
                      <Check className="h-3 w-3" />
                    </button>

                    <div className="min-w-0 flex-1 cursor-pointer" onClick={() => setDetailId(run.run_id)}>
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="max-w-full truncate text-xs font-medium text-foreground">{runDisplayName(run)}</span>
                        <span className={`rounded border px-1 py-px text-[10px] ${KIND_BADGE_CLS[run.kind]}`}>{KIND_LABELS[run.kind]}</span>
                        {run.source_run_id && (
                          <span className="rounded border border-border bg-elevated px-1 py-px text-[10px] text-muted" title={`复跑自 ${run.source_run_id}`}>
                            复跑
                          </span>
                        )}
                        {run.status !== 'completed' && (
                          <span className="rounded border border-danger/30 bg-danger/10 px-1 py-px text-[10px] text-danger">
                            {run.status === 'failed' ? '失败' : run.status}
                          </span>
                        )}
                        {run.warnings_count > 0 && (
                          <span className="inline-flex items-center gap-0.5 text-[10px] text-warning" title={`${run.warnings_count} 条方法论警告`}>
                            <AlertTriangle className="h-3 w-3" />{run.warnings_count}
                          </span>
                        )}
                      </div>

                      <div className="mt-0.5 flex items-center gap-1.5 text-[11px]">
                        {editingLabelId === run.run_id ? (
                          <input
                            autoFocus
                            value={labelDraft}
                            onChange={event => setLabelDraft(event.target.value)}
                            onClick={event => event.stopPropagation()}
                            onKeyDown={event => {
                              if (event.key === 'Enter') commitLabel(run)
                              if (event.key === 'Escape') setEditingLabelId(null)
                            }}
                            onBlur={() => commitLabel(run)}
                            placeholder="输入标签，回车保存"
                            aria-label="编辑标签"
                            className="control !h-6 w-44 max-w-full !px-2 text-[11px]"
                          />
                        ) : (
                          <>
                            {run.label
                              ? <span className="truncate text-secondary">{run.label}</span>
                              : <span className="text-muted">未加标签</span>}
                            <button
                              type="button"
                              onClick={event => { event.stopPropagation(); startEditLabel(run) }}
                              aria-label="编辑标签"
                              className="rounded-btn p-0.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                            >
                              <Pencil className="h-3 w-3" />
                            </button>
                          </>
                        )}
                      </div>

                      <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted">
                        <span className="font-mono">{run.run_id}</span>
                        <span>{fmtDateTime(run.created_at)}</span>
                        <span>{run.start ?? '—'} ~ {run.end ?? '—'}</span>
                        {run.symbols_count != null && <span>{run.symbols_count} 只标的</span>}
                        <span>{run.n_trades} 笔交易</span>
                      </div>

                      {headline.length > 0 && (
                        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                          {headline.map(([key, value]) => (
                            <span key={key} className="text-[10px] text-muted">
                              {metricLabel(key)}{' '}
                              <b className={`font-mono text-[11px] num ${metricValueClass(key, value)}`}>{formatMetricValue(key, value)}</b>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="flex shrink-0 flex-col items-end gap-1">
                      <button
                        type="button"
                        onClick={() => toggleFavorite(run)}
                        aria-pressed={run.favorite}
                        aria-label={run.favorite ? '取消收藏' : '收藏'}
                        className={`rounded-btn p-1 transition-colors hover:bg-elevated ${run.favorite ? 'text-warning' : 'text-muted hover:text-foreground'}`}
                      >
                        <Star className={`h-3.5 w-3.5 ${run.favorite ? 'fill-current' : ''}`} />
                      </button>
                      <div className="flex items-center gap-0.5">
                        <button
                          type="button"
                          onClick={() => setDetailId(run.run_id)}
                          title="查看运行详情"
                          aria-label={`查看 ${runDisplayName(run)} 详情`}
                          className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                        >
                          <Eye className="h-3.5 w-3.5" />
                        </button>
                        <a
                          href={api.backtestRunExportUrl(run.run_id, 'json')}
                          download
                          title="导出 JSON"
                          aria-label="导出 JSON"
                          className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                        >
                          <FileJson className="h-3.5 w-3.5" />
                        </a>
                        {csvExportable && (
                          <a
                            href={api.backtestRunExportUrl(run.run_id, 'csv')}
                            download
                            title="导出 CSV"
                            aria-label="导出 CSV"
                            className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                          >
                            <FileSpreadsheet className="h-3.5 w-3.5" />
                          </a>
                        )}
                        <button
                          type="button"
                          onClick={event => { void downloadListReport(run.run_id, event) }}
                          disabled={reportDownloadingId === run.run_id}
                          title={reportDownloadingId === run.run_id ? '报告生成中' : '下载报告'}
                          aria-label={reportDownloadingId === run.run_id ? '报告生成中' : '下载报告'}
                          aria-busy={reportDownloadingId === run.run_id}
                          className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
                        >
                          {reportDownloadingId === run.run_id
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <FileDown className="h-3.5 w-3.5" />}
                        </button>
                        <button
                          type="button"
                          onClick={() => rerunRun.mutate(run.run_id)}
                          disabled={rerunRun.isPending}
                          title="按原配置复跑"
                          aria-label="复跑"
                          className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
                        >
                          {rerunRun.isPending && rerunRun.variables === run.run_id
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <RotateCcw className="h-3.5 w-3.5" />}
                        </button>
                        <button
                          type="button"
                          onClick={() => requestDelete(run.run_id, runDisplayName(run))}
                          disabled={deleteRun.isPending}
                          title="删除该运行记录"
                          aria-label="删除"
                          className="rounded-btn p-1 text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>
                )
              })}
              {items.length < total && (
                <div className="p-3">
                  <button type="button" onClick={() => setLimit(value => value + PAGE_SIZE)} className="btn-secondary w-full text-xs">
                    加载更多（已显示 {items.length} / {total}）
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </section>

      <AnimatePresence>
        {detailId != null && (
          <RunDetailDrawer
            key={detailId}
            runId={detailId}
            onClose={() => setDetailId(null)}
            onToggleFavorite={toggleFavorite}
            onRerun={runId => rerunRun.mutate(runId)}
            rerunning={rerunRun.isPending}
            onDelete={requestDelete}
            deleting={deleteRun.isPending}
          />
        )}
      </AnimatePresence>
    </div>
  )
}
