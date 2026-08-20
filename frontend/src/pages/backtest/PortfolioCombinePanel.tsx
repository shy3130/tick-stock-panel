import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Combine,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react'
import type { EChartsOption } from 'echarts'
import {
  api,
  type BacktestRunSummary,
  type PortfolioCombineResponse,
  type PortfolioRebalanceMode,
} from '@/lib/api'
import { useECharts } from './charts/useECharts'
import { MetricExplainer } from './components/MetricExplainer'
import { fmtPct } from '@/lib/format'
import { cn } from '@/lib/cn'

/**
 * F15 净值组合回测 — 多个已固化策略 Run 的日频净值事后加权合成。
 *
 * 口径: 独立回测净值事后加权合成, 非共享资金池撮合 (不模拟策略间资金
 * 竞争/同时满仓冲突, 再平衡无摩擦)。纯诊断, 结果不落盘、不生成新 Run。
 * 与上方的「组合策略构建器」并列: 构建器产出可回测的合并策略, 本面板
 * 对既有 Run 做组合层诊断。
 */

const MAX_ITEMS = 8
const MIN_ITEMS = 2
const INPUT_CLS = 'control w-full text-xs'

type SelectedRun = { run_id: string; label: string; weight: number }

const REBALANCE_OPTIONS: Array<{ value: PortfolioRebalanceMode; label: string; hint: string }> = [
  { value: 'daily', label: '每日再平衡', hint: '每日收益按目标权重加权求和' },
  { value: 'monthly', label: '每月再平衡', hint: '每月首个共同交易日重置权重，段内漂移' },
  { value: 'none', label: '不再平衡（漂移）', hint: '买入持有加权，权重随涨跌漂移' },
]

/** 相关性热力格配色: -1 蓝 (分散化好) ↔ +1 红 (同涨同跌) */
function correlationColor(value: number | null): string {
  if (value == null) return 'transparent'
  const intensity = Math.min(Math.abs(value), 1)
  const alpha = 0.12 + intensity * 0.55
  return value < 0
    ? `rgba(59,130,246,${alpha.toFixed(3)})`
    : `rgba(239,68,68,${alpha.toFixed(3)})`
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  return value != null && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function runLabel(run: BacktestRunSummary): string {
  return run.label || run.subject?.name || run.run_id
}

/** 指标卡: 数值 + 可选着色 */
function MetricCard({ label, value, valueClass = 'text-foreground' }: {
  label: React.ReactNode
  value: string
  valueClass?: string
}) {
  return (
    <div className="rounded-btn border border-border bg-elevated/40 px-3 py-2">
      <div className="flex items-center gap-1 text-[10px] text-muted">{label}</div>
      <div className={cn('metric-value mt-1 !text-sm font-mono num', valueClass)}>{value}</div>
    </div>
  )
}

/** 合成净值曲线 (起点恒为 1.0) */
function CombineEquityChart({ curve }: { curve: PortfolioCombineResponse['equity_curve'] }) {
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
      name: '合成净值(起点=1)',
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
    },
    series: [{
      name: '组合合成净值',
      type: 'line',
      showSymbol: false,
      data: curve
        .filter(point => Number.isFinite(point.value))
        .map(point => [point.date, point.value]),
      lineStyle: { width: 1.8, color: '#22c55e' },
      itemStyle: { color: '#22c55e' },
      areaStyle: { color: 'rgba(34,197,94,0.06)' },
    }],
  }), [curve])
  const chartRef = useECharts(option, [curve])
  return <div ref={chartRef} className="h-56 w-full" />
}

/** 相关性矩阵热力格 (对称, 对角 1) */
function CorrelationMatrix({ matrix }: { matrix: PortfolioCombineResponse['correlation_matrix'] }) {
  const { run_ids: runIds, values } = matrix
  const short = (id: string) => id.length > 10 ? `${id.slice(0, 10)}…` : id
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-px text-[10px]">
        <thead>
          <tr>
            <th className="text-left font-normal text-muted" />
            {runIds.map(id => (
              <th key={id} className="px-1.5 py-1 font-mono font-normal text-muted" title={id}>
                {short(id)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {values.map((row, i) => (
            <tr key={runIds[i]}>
              <th className="px-1.5 py-1 text-left font-mono font-normal text-muted" title={runIds[i]}>
                {short(runIds[i])}
              </th>
              {row.map((value, j) => (
                <td
                  key={`${runIds[i]}-${runIds[j]}`}
                  className="px-1.5 py-1 text-center font-mono num text-secondary"
                  style={{ background: correlationColor(value) }}
                  title={value == null ? '收益零方差，相关性不可计算' : `corr = ${value.toFixed(4)}`}
                >
                  {value == null ? '—' : value.toFixed(2)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1.5 text-[10px] leading-4 text-muted">
        日收益 Pearson 相关；蓝（负）代表分散化更好，红（高正）代表同涨同跌。零方差成分对不显示数值。
      </p>
    </div>
  )
}

/** 成分表: 权重 / 各自收益 / 夏普 / 对组合增量的贡献 */
function ItemsTable({ items }: { items: PortfolioCombineResponse['items'] }) {
  return (
    <div className="data-table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>成分 Run</th>
            <th className="text-right">权重(归一)</th>
            <th className="text-right">区间收益</th>
            <th className="text-right">夏普</th>
            <th>对组合增量贡献</th>
          </tr>
        </thead>
        <tbody>
          {items.map(item => {
            const contribution = item.contribution
            return (
              <tr key={item.run_id}>
                <td>
                  <div className="max-w-[16rem] truncate text-xs" title={`${item.run_id}${item.label && item.label !== item.run_id ? ` · ${item.label}` : ''}`}>
                    {item.label || item.run_id}
                  </div>
                  <div className="font-mono text-[10px] text-muted">{item.run_id}</div>
                </td>
                <td className="text-right font-mono num">
                  {fmtPct(item.weight)}
                  {item.weight_raw != null && Math.abs(item.weight_raw - item.weight) > 1e-9 && (
                    <span className="ml-1 text-[10px] text-muted" title={`原始权重 ${item.weight_raw}`}>原始 {item.weight_raw}</span>
                  )}
                </td>
                <td className={cn('text-right font-mono num', (item.total_return ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>
                  {fmtPct(item.total_return)}
                </td>
                <td className="text-right font-mono num">{fmtNum(item.sharpe)}</td>
                <td>
                  {contribution == null ? (
                    <span className="text-[10px] text-muted">组合总收益为 0，贡献不可计算</span>
                  ) : (
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 w-full max-w-[10rem] overflow-hidden rounded-full bg-base">
                        <div
                          className={cn('h-full', contribution >= 0 ? 'bg-bull' : 'bg-bear')}
                          style={{ width: `${Math.min(Math.abs(contribution), 1) * 100}%` }}
                        />
                      </div>
                      <span className={cn('shrink-0 font-mono num text-[11px]', contribution >= 0 ? 'text-bull' : 'text-bear')}>
                        {fmtPct(contribution, 1)}
                      </span>
                    </div>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function PortfolioCombinePanel() {
  const [selected, setSelected] = useState<SelectedRun[]>([])
  const [rebalance, setRebalance] = useState<PortfolioRebalanceMode>('daily')
  const [search, setSearch] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const runsQuery = useQuery({
    queryKey: ['portfolio-combine-runs'],
    queryFn: () => api.backtestRuns({ limit: 200 }),
    staleTime: 30_000,
  })

  // 只列有净值曲线的策略类 run (position 模式); 因子 run 无日频净值
  const candidates = useMemo(() => {
    const kw = search.trim().toLowerCase()
    return (runsQuery.data?.items ?? [])
      .filter(run => run.kind !== 'factor' && (run.n_points ?? 0) >= 2)
      .filter(run => !selected.some(s => s.run_id === run.run_id))
      .filter(run => {
        if (!kw) return true
        return (
          run.run_id.toLowerCase().includes(kw)
          || (run.label || '').toLowerCase().includes(kw)
          || (run.subject?.name || '').toLowerCase().includes(kw)
        )
      })
  }, [runsQuery.data, search, selected])

  const totalWeight = useMemo(
    () => selected.reduce((sum, s) => sum + (Number.isFinite(s.weight) ? s.weight : 0), 0),
    [selected],
  )

  const toggleRun = (run: BacktestRunSummary) => {
    setFormError(null)
    setSelected(curr => {
      if (curr.some(s => s.run_id === run.run_id)) {
        return curr.filter(s => s.run_id !== run.run_id)
      }
      if (curr.length >= MAX_ITEMS) {
        setFormError(`最多选择 ${MAX_ITEMS} 个 Run`)
        return curr
      }
      return [...curr, { run_id: run.run_id, label: runLabel(run), weight: 1 }]
    })
  }

  const updateWeight = (runId: string, raw: number) => {
    setSelected(curr => curr.map(s => (s.run_id === runId ? { ...s, weight: raw } : s)))
  }

  const combine = useMutation({
    mutationFn: () => api.portfolioCombine({
      items: selected.map(s => ({ run_id: s.run_id, weight: s.weight })),
      rebalance,
    }),
    onSuccess: () => setFormError(null),
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '组合合成失败'
      setFormError(msg)
    },
  })

  const submit = () => {
    if (selected.length < MIN_ITEMS) {
      setFormError(`至少选择 ${MIN_ITEMS} 个 Run`)
      return
    }
    if (selected.some(s => !Number.isFinite(s.weight) || s.weight <= 0)) {
      setFormError('每个成分权重须为正数')
      return
    }
    if (totalWeight <= 0) {
      setFormError('权重总和必须大于 0')
      return
    }
    setFormError(null)
    combine.mutate()
  }

  const result = combine.data
  const stats = result?.stats ?? {}
  const rebalanceHint = REBALANCE_OPTIONS.find(o => o.value === rebalance)?.hint

  return (
    <section className="panel flex shrink-0 flex-col">
      <div className="panel-header">
        <div>
          <div className="section-kicker">Portfolio Combine</div>
          <h2 className="section-title flex items-center gap-1.5">
            <Combine className="h-3.5 w-3.5 text-accent" />
            净值组合回测
            <MetricExplainer term="portfolio_combine" />
          </h2>
        </div>
        <button
          type="button"
          onClick={() => runsQuery.refetch()}
          disabled={runsQuery.isFetching}
          className="btn-ghost !h-7 !px-2 text-[11px]"
        >
          <RefreshCw className={cn('h-3 w-3', runsQuery.isFetching && 'animate-spin')} />
          刷新
        </button>
      </div>
      <div className="panel-body space-y-3">
        {/* 口径横幅 */}
        <div className="rounded-btn border border-warning/30 bg-warning/5 p-2.5 text-[11px] leading-relaxed text-secondary">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <p>
              <b className="text-warning">口径：</b>
              由独立回测的日频净值<b>事后加权合成</b>，非共享资金池撮合——不模拟策略间资金竞争、
              同时满仓冲突或保证金约束；再平衡假设无摩擦。仅研究用途，不生成交易建议，结果不落盘。
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[22rem_minmax(0,1fr)]">
          {/* 左: Run 选择 + 权重 + 再平衡 */}
          <div className="space-y-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="搜索运行历史 (run_id / 名称 / 标签)"
                className={cn(INPUT_CLS, 'pl-8')}
              />
            </div>

            <div className="max-h-52 space-y-1 overflow-y-auto rounded-btn border border-border bg-base/40 p-1.5">
              {runsQuery.isLoading && (
                <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />加载运行历史…
                </div>
              )}
              {!runsQuery.isLoading && candidates.length === 0 && (
                <div className="px-2 py-6 text-center text-[11px] text-muted">
                  {selected.length >= MAX_ITEMS
                    ? `已选满 ${MAX_ITEMS} 个 Run`
                    : '没有可组合的策略 Run；先在「策略回测」完成 position 模式回测'}
                </div>
              )}
              {candidates.map(run => (
                <button
                  key={run.run_id}
                  type="button"
                  onClick={() => toggleRun(run)}
                  className="w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-left transition-colors hover:bg-elevated"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs text-foreground">{runLabel(run)}</span>
                    <span className="shrink-0 rounded border border-border px-1 py-px font-mono text-[10px] text-muted">
                      {run.kind}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-muted">
                    <span className="truncate">{run.run_id}</span>
                    <span className="shrink-0">{run.start}~{run.end}</span>
                  </div>
                </button>
              ))}
            </div>

            {/* 已选成分 + 权重 */}
            <div className="space-y-1.5">
              <div className="flex items-baseline justify-between text-[11px] text-secondary">
                <span>已选成分 {selected.length}/{MAX_ITEMS}（权重默认等权，合成时归一化）</span>
                {selected.length > 0 && totalWeight > 0 && (
                  <span className="font-mono text-[10px] text-muted">
                    原始和 {totalWeight % 1 === 0 ? totalWeight : totalWeight.toFixed(3)} → 归一化
                  </span>
                )}
              </div>
              {selected.map(s => (
                <div key={s.run_id} className="flex items-center gap-2 rounded-btn border border-border bg-elevated/40 px-2 py-1.5">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs text-foreground" title={s.run_id}>{s.label}</div>
                    <div className="truncate font-mono text-[10px] text-muted">{s.run_id}</div>
                  </div>
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={s.weight}
                    onChange={e => updateWeight(s.run_id, Number(e.target.value))}
                    className="w-20 rounded-input border border-border bg-base px-2 py-1 text-xs focus:border-accent focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => toggleRun({ run_id: s.run_id } as BacktestRunSummary)}
                    className="btn-ghost !h-7 !w-7 !p-0 text-danger"
                    title="移除"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>

            <label className="block text-xs text-secondary">
              再平衡
              <select
                value={rebalance}
                onChange={e => setRebalance(e.target.value as PortfolioRebalanceMode)}
                className={cn(INPUT_CLS, 'mt-1')}
              >
                {REBALANCE_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {rebalanceHint && <span className="mt-1 block text-[10px] leading-3 text-muted">{rebalanceHint}</span>}
            </label>

            {formError && (
              <div className="rounded-btn border border-danger/30 bg-danger/5 px-2.5 py-1.5 text-[11px] leading-4 text-danger">
                {formError}
              </div>
            )}

            <button
              type="button"
              onClick={submit}
              disabled={combine.isPending}
              className="btn-primary w-full"
            >
              {combine.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {combine.isPending ? '合成中…' : '合成组合净值'}
            </button>
          </div>

          {/* 右: 结果区 */}
          <div className="min-w-0 space-y-3">
            {!result && (
              <div className="flex h-full min-h-48 items-center justify-center rounded-btn border border-dashed border-border px-4 py-10 text-center text-[11px] leading-relaxed text-muted">
                从左侧运行历史选择 2~8 个策略 Run，设置权重与再平衡方式后合成。
                <br />
                候选执行模式（mode=full）与因子 Run 无日频净值语义，会被拒绝。
              </div>
            )}
            {result && (
              <>
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <div className="text-xs font-semibold text-foreground">
                    合成结果 · 共同交易日 {result.overlap_days} 天
                    {result.date_range && (
                      <span className="ml-2 font-mono text-[10px] font-normal text-muted">
                        {result.date_range.start} ~ {result.date_range.end}
                      </span>
                    )}
                  </div>
                  <span className="text-[10px] text-muted">
                    {REBALANCE_OPTIONS.find(o => o.value === result.rebalance)?.label}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                  <MetricCard
                    label={<span>总收益</span>}
                    value={fmtPct(stats.total_return)}
                    valueClass={(stats.total_return ?? 0) >= 0 ? 'text-bull' : 'text-bear'}
                  />
                  <MetricCard label={<span>年化收益</span>} value={fmtPct(stats.annual_return)} />
                  <MetricCard label={<span>夏普<MetricExplainer term="sharpe" /></span>} value={fmtNum(stats.sharpe)} />
                  <MetricCard
                    label={<span>最大回撤<MetricExplainer term="max_drawdown" /></span>}
                    value={fmtPct(stats.max_drawdown)}
                    valueClass="text-bear"
                  />
                  <MetricCard label={<span>卡玛<MetricExplainer term="calmar" /></span>} value={fmtNum(stats.calmar)} />
                  <MetricCard label={<span>年化波动</span>} value={fmtPct(stats.annual_volatility)} />
                </div>

                <div className="rounded-btn border border-border p-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1 pb-2">
                    <div className="text-xs font-semibold text-foreground">合成净值曲线</div>
                    <div className="text-[10px] text-muted">起点恒为 1.0，按归一化权重合成，不代表真实资金</div>
                  </div>
                  <CombineEquityChart curve={result.equity_curve} />
                </div>

                <div className="rounded-btn border border-border p-3">
                  <div className="pb-2 text-xs font-semibold text-foreground">成分与贡献</div>
                  <ItemsTable items={result.items} />
                </div>

                <div className="rounded-btn border border-border p-3">
                  <div className="pb-2 text-xs font-semibold text-foreground">成分相关性矩阵</div>
                  <CorrelationMatrix matrix={result.correlation_matrix} />
                </div>

                {result.warnings.length > 0 && (
                  <div className="rounded-btn border border-border bg-elevated/30 p-2.5">
                    <div className="text-[11px] font-medium text-secondary">提示（{result.warnings.length}）</div>
                    <ul className="mt-1 space-y-1">
                      {result.warnings.map(w => (
                        <li key={w} className="text-[10px] leading-4 text-muted">· {w}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
