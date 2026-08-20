import { useMemo, useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { FlaskConical, Loader2, Shuffle, Waypoints } from 'lucide-react'
import { api, type StrategyBacktestRequest, type StrategyRobustnessResult, type MonteCarloTradeShuffle, type TradeEquityBand, type WalkForwardResult } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { useECharts } from '../charts/useECharts'
import { MetricExplainer } from './MetricExplainer'
import { validateTradeEquityBand } from './trustDiagnosticsCore'

interface Props {
  request: StrategyBacktestRequest
}

const finiteNumber = (value: unknown): number | null => {
  if (value == null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const fmt = (value: unknown, digits = 2) => {
  const parsed = finiteNumber(value)
  return parsed == null ? '-' : parsed.toFixed(digits)
}

function Summary({ result }: { result: StrategyRobustnessResult }) {
  const stability = result.segment_stability.summary
  const bootstrap = result.bootstrap
  const permutation = result.mc_permutation
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      <div className="rounded-input border border-border bg-base/30 px-3 py-2">
        <div className="text-[10px] text-muted">正 Sharpe 分段</div>
        <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{stability.positive_folds} / {stability.n_folds}</div>
      </div>
      <div className="rounded-input border border-border bg-base/30 px-3 py-2">
        <div className="text-[10px] text-muted">最差分段 Sharpe</div>
        <div className={`mt-1 font-mono text-sm font-semibold num ${priceColorClass(stability.worst)}`}>{fmt(stability.worst)}</div>
      </div>
      <div className="rounded-input border border-border bg-base/30 px-3 py-2">
        <div className="text-[10px] text-muted">Bootstrap Sharpe 置信区间</div>
        <div className="mt-1 font-mono text-sm font-semibold text-foreground num">
          {bootstrap ? `[${fmt(bootstrap.ci_low)}, ${fmt(bootstrap.ci_high)}]` : '未运行'}
        </div>
      </div>
      <div className="rounded-input border border-border bg-base/30 px-3 py-2">
        <div className="text-[10px] text-muted">随机置换 p 值</div>
        <div className="mt-1 font-mono text-sm font-semibold text-foreground num">
          {permutation ? fmt(permutation.p_value, 4) : '未运行'}
        </div>
      </div>
    </div>
  )
}

function SegmentStability({ result }: { result: StrategyRobustnessResult }) {
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="text-[11px] font-medium text-secondary">分段稳定性</div>
        <div className="mt-0.5 text-[10px] text-muted">同一参数按时间顺序切 {result.segment_stability.folds.length} 段重跑，检验时序稳定性；不做训练选参，非 Walk-Forward（见下方“Walk-Forward 样本外”）。</div>
      </div>
      <div className="data-table-scroll">
        <table className="data-table">
          <thead><tr><th>区间</th><th className="text-right">收益</th><th className="text-right">Sharpe</th><th className="text-right">最大回撤</th><th>状态</th></tr></thead>
          <tbody>
            {result.segment_stability.folds.map((fold, index) => (
              <tr key={`${fold.start}-${fold.end}`}>
                <td className="font-mono text-[11px]">#{index + 1} · {fold.start} → {fold.end}</td>
                <td className={`text-right font-mono num ${priceColorClass(Number(fold.stats.total_return))}`}>{fmtPct(fold.stats.total_return)}</td>
                <td className="text-right font-mono num">{fmt(fold.stats.sharpe)}</td>
                <td className="text-right font-mono text-bear num">{fmtPct(fold.stats.max_drawdown)}</td>
                <td>{fold.error ? <span className="text-danger">{fold.error}</span> : <span className="text-bull">完成</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StitchedCurveChart({ curve }: { curve: WalkForwardResult['stitched_curve'] }) {
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 44, right: 12, top: 10, bottom: 24 },
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
    },
    series: [{
      name: 'OOS 拼接净值',
      type: 'line',
      showSymbol: false,
      data: curve.map(point => [point.date, point.value] as [string, number]),
      lineStyle: { width: 1.6, color: '#3b82f6' },
      itemStyle: { color: '#3b82f6' },
    }],
  }), [curve])
  const chartRef = useECharts(option, [curve])
  return <div ref={chartRef} className="h-[220px]" />
}

function WalkForwardCard({ label, children, note }: { label: string; children: ReactNode; note?: string }) {
  return (
    <div className="rounded-input border border-border bg-base/30 px-3 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{children}</div>
      {note && <div className="mt-0.5 text-[9px] leading-3 text-muted">{note}</div>}
    </div>
  )
}

function WalkForwardOOS({ result }: { result: StrategyRobustnessResult }) {
  const wf = result.walk_forward
  if (!wf) return null
  // 旧持久化响应没有 enabled 字段: undefined 视为已启用, 保持向后兼容
  if (wf.enabled === false) {
    return (
      <div className="rounded-btn border border-border px-3 py-2 text-[11px] leading-5 text-muted">
        Walk-Forward 样本外：{wf.warning || '未启用'}。勾选「严格 Walk-Forward」后重新运行即可；开启后每折会对全部候选参数在训练窗重复训练、冻结后再重跑样本外，耗时显著增加。
      </div>
    )
  }
  if (wf.folds.length === 0) {
    return <div className="rounded-btn border border-border px-3 py-2 text-[11px] text-muted">Walk-Forward 样本外：{wf.warning || '区间不足，未生成折'}</div>
  }
  const summary = wf.summary
  const ratio = summary.n_folds > 0 && summary.positive_fold_ratio != null
    ? `${Math.round(summary.positive_fold_ratio * 100)}%`
    : '-'
  const truncated = wf.requested_candidates != null
    && wf.effective_candidates != null
    && wf.requested_candidates > wf.effective_candidates
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="text-[11px] font-medium text-secondary">Walk-Forward 样本外</div>
        <div className="mt-0.5 text-[10px] leading-4 text-muted">
          每折先在训练窗按训练期指标对 {wf.n_candidates} 个候选（{wf.candidate_space}）选参并冻结，再仅在样本外窗口运行一次；OOS 数据不参与任何选参。{truncated ? `请求 ${wf.requested_candidates} 个候选，受执行预算确定性截断为 ${wf.effective_candidates} 个（训练+OOS 最多 ${wf.max_executions} 次）。` : ''}
        </div>
      </div>
      {wf.warning && (
        <div className="border-b border-border bg-warning/5 px-3 py-1.5 text-[10px] leading-4 text-warning">{wf.warning}</div>
      )}
      <div className="space-y-3 p-3">
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          <WalkForwardCard label="OOS 总收益（拼接）">{fmtPct(summary.oos_total_return)}</WalkForwardCard>
          <WalkForwardCard label="OOS Sharpe（拼接）">{fmt(summary.oos_sharpe)}</WalkForwardCard>
          <WalkForwardCard label="OOS 最大回撤（拼接）"><span className="text-bear">{fmtPct(summary.oos_max_drawdown)}</span></WalkForwardCard>
          <WalkForwardCard label="正收益折比例">{summary.positive_return_folds} / {summary.n_folds} · {ratio}</WalkForwardCard>
          <WalkForwardCard label="最差折收益">{fmtPct(summary.worst_fold_return)}</WalkForwardCard>
          <WalkForwardCard label="训练→OOS 平均退化" note="训练 Sharpe − OOS Sharpe 的均值；负值表示 OOS 更好">{fmt(summary.mean_degradation)}</WalkForwardCard>
          <WalkForwardCard label="参数组合数" note="各折冻结参数的去重数量，1 表示无漂移">{wf.param_drift.n_distinct_param_sets}</WalkForwardCard>
          <WalkForwardCard label="折数 / 候选数" note={wf.max_executions != null ? `训练+OOS 额外执行上限 ${wf.max_executions} 次` : undefined}>{summary.n_folds} 折 × {wf.n_candidates} 候选</WalkForwardCard>
        </div>
        {wf.stitched_curve.length >= 2 ? (
          <StitchedCurveChart curve={wf.stitched_curve} />
        ) : (
          <div className="rounded-input border border-border px-3 py-3 text-center text-[11px] text-muted">各折均无可用 OOS 净值曲线，无法拼接。</div>
        )}
        <div className="data-table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>训练区间（选参）</th>
                <th>OOS 区间（冻结参数）</th>
                <th className="text-right">候选</th>
                <th>选中参数</th>
                <th className="text-right">训练 Sharpe</th>
                <th className="text-right">OOS 收益</th>
                <th className="text-right">OOS Sharpe</th>
                <th className="text-right">退化</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {wf.folds.map((fold, index) => (
                <tr key={`${fold.oos_start}-${fold.oos_end}`}>
                  <td className="font-mono text-[11px]">#{index + 1} · {fold.train_start} → {fold.train_end}</td>
                  <td className="font-mono text-[11px]">{fold.oos_start} → {fold.oos_end}</td>
                  <td className="text-right font-mono num">{fold.n_candidates}</td>
                  <td title={JSON.stringify(fold.selected_params)}>
                    <span className={fold.selected_label === 'baseline' ? 'text-muted' : 'text-accent'}>{fold.selected_label}</span>
                  </td>
                  <td className="text-right font-mono num">{fmt(fold.train_stats.sharpe)}</td>
                  <td className={`text-right font-mono num ${priceColorClass(Number(fold.oos_stats.total_return))}`}>{fmtPct(fold.oos_stats.total_return)}</td>
                  <td className="text-right font-mono num">{fmt(fold.oos_stats.sharpe)}</td>
                  <td className={`text-right font-mono num ${finiteNumber(fold.degradation) == null ? '' : priceColorClass(-Number(fold.degradation))}`}>{fmt(fold.degradation)}</td>
                  <td>{fold.error ? <span className="text-danger">{fold.error}</span> : <span className="text-bull">完成</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {Object.keys(wf.param_drift.params).length > 0 && (
          <div className="rounded-input border border-border px-3 py-2">
            <div className="text-[10px] font-medium text-muted">参数漂移（逐折冻结值）</div>
            <div className="mt-1 space-y-0.5">
              {Object.entries(wf.param_drift.params).map(([param, values]) => (
                <div key={param} className="flex flex-wrap items-baseline gap-x-2 font-mono text-[10px]">
                  <span className="text-secondary">{param}</span>
                  <span className="text-muted">{values.map(value => value == null ? '-' : String(value)).join(' → ')}</span>
                </div>
              ))}
            </div>
            {wf.param_drift.distinct_labels.length > 1 && (
              <div className="mt-1 text-[9px] text-muted">出现过的参数组合：{wf.param_drift.distinct_labels.join('、')}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Perturbation({ result }: { result: StrategyRobustnessResult }) {
  const perturbation = result.parameter_perturbation
  if (!perturbation) return null
  if (perturbation.cases.length === 0) {
    return <div className="rounded-btn border border-border px-3 py-2 text-[11px] text-muted">参数扰动：{perturbation.reason || '没有可用场景'}</div>
  }
  const baselineReturn = finiteNumber(perturbation.baseline.total_return)
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="text-[11px] font-medium text-secondary">参数扰动敏感性</div>
        <div className="mt-0.5 text-[10px] text-muted">数值参数按 ±{Math.round(perturbation.fraction * 100)}%（受 min/max/step 约束）逐项重跑；与基线收益 {fmtPct(baselineReturn)} 比较。</div>
      </div>
      <div className="data-table-scroll">
        <table className="data-table">
          <thead><tr><th>参数</th><th className="text-right">基线 → 扰动值</th><th className="text-right">累计收益</th><th className="text-right">收益变化</th><th className="text-right">Sharpe</th><th className="text-right">最大回撤</th></tr></thead>
          <tbody>
            {perturbation.cases.map(item => {
              const totalReturn = finiteNumber(item.stats.total_return)
              const delta = totalReturn != null && baselineReturn != null ? totalReturn - baselineReturn : null
              return (
                <tr key={`${item.param}-${item.direction}`}>
                  <td><span className="font-medium text-secondary">{item.label}</span><span className="ml-1 font-mono text-[10px] text-muted">{item.param}</span></td>
                  <td className="text-right font-mono num">{item.base_value} → {item.value}</td>
                  <td className={`text-right font-mono num ${priceColorClass(totalReturn)}`}>{fmtPct(totalReturn)}</td>
                  <td className={`text-right font-mono num ${priceColorClass(delta)}`}>{fmtPct(delta)}</td>
                  <td className="text-right font-mono num">{fmt(item.stats.sharpe)}</td>
                  <td className="text-right font-mono text-bear num">{fmtPct(item.stats.max_drawdown)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ExitBreakdown({ result }: { result: StrategyRobustnessResult }) {
  if (result.exit_breakdown.length === 0) return null
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2 text-[11px] font-medium text-secondary">退出原因归因</div>
      <div className="data-table-scroll">
        <table className="data-table">
          <thead><tr><th>退出原因</th><th className="text-right">笔数</th><th className="text-right">胜率</th><th className="text-right">平均收益</th><th className="text-right">累计收益</th></tr></thead>
          <tbody>
            {result.exit_breakdown.map(item => (
              <tr key={item.exit_reason}>
                <td>{item.exit_reason}</td>
                <td className="text-right font-mono num">{item.n}</td>
                <td className="text-right font-mono num">{fmtPct(item.win_rate)}</td>
                <td className={`text-right font-mono num ${priceColorClass(item.avg_pnl_pct)}`}>{fmtPct(item.avg_pnl_pct)}</td>
                <td className={`text-right font-mono num ${priceColorClass(item.total_pnl_pct)}`}>{fmtPct(item.total_pnl_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** 逐笔收益 bootstrap 净值带图 — x = trade index, p05–p95 面积带 + p25/p50/p75 参考线 */
function TradeEquityBandChart({ band }: { band: TradeEquityBand }) {
  const option = useMemo<EChartsOption>(() => {
    const { p05, p25, p50, p75, p95 } = band.percentiles
    // 带高度 = p95 - p05, 与 p05 底座堆叠出 p05–p95 区间带
    const bandSpan = p95.map((value, index) => +(value - p05[index]).toFixed(6))
    return {
      animation: false,
      grid: { left: 44, right: 12, top: 24, bottom: 26 },
      legend: { top: 0, textStyle: { color: '#94a3b8', fontSize: 10 } },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15,23,42,0.95)',
        borderColor: 'rgba(148,163,184,0.2)',
        textStyle: { color: '#e2e8f0', fontSize: 12 },
        formatter: (params: unknown) => {
          const first = Array.isArray(params) ? params[0] as { dataIndex?: number } : params as { dataIndex?: number }
          const index = Number(first?.dataIndex ?? -1)
          if (index < 0 || index >= band.n_trades) return ''
          const rows = (['p95', 'p75', 'p50', 'p25', 'p05'] as const)
            .map(key => `<div style="display:flex;justify-content:space-between;gap:16px"><span>${key}</span><span style="font-family:monospace">${band.percentiles[key][index].toFixed(3)}</span></div>`)
          return [`<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">第 ${index + 1} 笔后</div>`, ...rows].join('')
        },
      },
      xAxis: {
        type: 'value',
        min: 0,
        name: '交易序号',
        nameTextStyle: { color: '#64748b', fontSize: 10 },
        axisLabel: { color: '#64748b', fontSize: 10, formatter: (v: number) => `#${Math.round(v)}` },
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
      series: [
        {
          name: '_p05_base',
          type: 'line',
          stack: 'band',
          data: p05,
          symbol: 'none',
          lineStyle: { width: 0, opacity: 0 },
          silent: true,
          legendHoverLink: false,
        },
        {
          name: 'p05–p95 带',
          type: 'line',
          stack: 'band',
          data: bandSpan,
          symbol: 'none',
          lineStyle: { width: 0, opacity: 0 },
          areaStyle: { color: 'rgba(59,130,246,0.12)' },
          emphasis: { disabled: true },
        },
        { name: 'p25', type: 'line', data: p25, symbol: 'none', lineStyle: { width: 1, color: 'rgba(148,163,184,0.55)', type: 'dashed' } },
        { name: 'p50', type: 'line', data: p50, symbol: 'none', lineStyle: { width: 1.8, color: '#3b82f6' } },
        { name: 'p75', type: 'line', data: p75, symbol: 'none', lineStyle: { width: 1, color: 'rgba(148,163,184,0.55)', type: 'dashed' } },
      ],
    }
  }, [band])
  const chartRef = useECharts(option, [band])
  return <div ref={chartRef} className="h-[240px]" />
}

/** 逐笔收益 bootstrap 净值带 — 交易分布诊断, 非账户净值 */
function TradeEquityBandSection({ result }: { result: StrategyRobustnessResult }) {
  const band = validateTradeEquityBand(result.trade_equity_band)
  if (!band) return null
  const finals = band.final_value_percentiles
  const finalCards: Array<[string, number]> = [
    ['p05', finals.p05],
    ['p25', finals.p25],
    ['p50', finals.p50],
    ['p75', finals.p75],
    ['p95', finals.p95],
  ]
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary"><Waypoints className="h-3.5 w-3.5 text-accent" />逐笔收益 bootstrap 净值带</div>
        <div className="mt-0.5 text-[10px] leading-4 text-muted">
          对逐笔收益率做 {band.n_boot.toLocaleString('zh-CN')} 次有放回重抽样，复利合成路径后取跨路径分位；这是<b>交易分布诊断，非账户净值</b> — 不含仓位权重、资金约束与持仓重叠，不能与账户净值曲线直接比较。
        </div>
      </div>
      <div className="space-y-2 p-3">
        <TradeEquityBandChart band={band} />
        <div className="grid grid-cols-5 gap-px overflow-hidden rounded-input border border-border bg-border">
          {finalCards.map(([label, value]) => (
            <div key={label} className="bg-surface px-2 py-2 text-center">
              <div className="text-[10px] text-muted">终值 {label}</div>
              <div className={`mt-0.5 font-mono text-sm font-semibold num ${priceColorClass(value - 1)}`}>{value.toFixed(3)}</div>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
          <span className="font-mono">n_trades: {band.n_trades}</span>
          <span className="font-mono">n_boot: {band.n_boot.toLocaleString('zh-CN')}</span>
          <span className="font-mono">seed: {band.seed}</span>
          <span>终值相对区间 {fmtPct(finals.p05 - 1, 1)} ~ {fmtPct(finals.p95 - 1, 1)}（p05–p95）。</span>
        </div>
      </div>
    </div>
  )
}

/** 蒙特卡洛重排响应校验 — 字段缺失/非有限值按 null 处理 (fail-closed, 不伪造展示) */
function validateMonteCarloShuffle(mc: unknown): MonteCarloTradeShuffle | null {
  if (mc == null || typeof mc !== 'object') return null
  const raw = mc as Record<string, unknown>
  if (finiteNumber(raw.n_sims) == null || finiteNumber(raw.seed) == null || finiteNumber(raw.n_trades) == null) return null
  const final = raw.final_return
  const dd = raw.max_drawdown
  if (final == null || dd == null || typeof final !== 'object' || typeof dd !== 'object') return null
  for (const block of [final, dd]) {
    for (const key of ['p05', 'p50', 'p95', 'mean'] as const) {
      if (finiteNumber((block as Record<string, unknown>)[key]) == null) return null
    }
  }
  if (finiteNumber(raw.prob_final_negative) == null) return null
  const hist = raw.dd_histogram
  if (hist == null || typeof hist !== 'object') return null
  const h = hist as Record<string, unknown>
  if (!Array.isArray(h.counts) || !Array.isArray(h.bin_edges) || h.bin_edges.length !== h.counts.length + 1) return null
  if (h.counts.some(c => finiteNumber(c) == null)) return null
  return mc as MonteCarloTradeShuffle
}

/** 重排最大回撤分布直方图 — 20 桶纯 div 横条, 不引图表库 */
function DdHistogram({ histogram }: { histogram: MonteCarloTradeShuffle['dd_histogram'] }) {
  const maxCount = Math.max(...histogram.counts, 1)
  return (
    <div className="space-y-0.5">
      {histogram.counts.map((count, index) => {
        const lo = histogram.bin_edges[index]
        const hi = histogram.bin_edges[index + 1]
        // 非零计数至少给 2% 可见宽度, 零计数不画条
        const width = count > 0 ? Math.max((count / maxCount) * 100, 2) : 0
        return (
          <div key={index} className="flex items-center gap-2">
            <span className="w-9 shrink-0 text-right font-mono text-[9px] text-muted">{(hi * 100).toFixed(0)}%</span>
            <div className="h-2 flex-1 overflow-hidden rounded-sm bg-base/60" title={`重排最大回撤 ∈ [${fmtPct(lo, 1)}, ${fmtPct(hi, 1)}): ${count} 次`}>
              <div className="h-full rounded-sm bg-accent/70" style={{ width: `${width}%` }} />
            </div>
            <span className="w-9 shrink-0 font-mono text-[9px] text-muted">{count > 0 ? count : ''}</span>
          </div>
        )
      })}
      <div className="pt-0.5 text-[9px] leading-3 text-muted">横轴: 重排最大回撤桶上边界；条长: 落桶模拟次数。</div>
    </div>
  )
}

/** 蒙特卡洛交易顺序重排 — 交易级顺序运气诊断; 样本 < 30 时显示不足提示 */
function MonteCarloShuffleSection({ result }: { result: StrategyRobustnessResult }) {
  const mc = validateMonteCarloShuffle(result.monte_carlo)
  return (
    <div className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
          <Shuffle className="h-3.5 w-3.5 text-accent" />蒙特卡洛重排<MetricExplainer term="monte_carlo_trade_shuffle" />
        </div>
        <div className="mt-0.5 text-[10px] leading-4 text-muted">
          同一批逐笔收益随机换成交顺序重排后的终值与回撤分布 — 回答<b>「顺序运气」</b>问题；交易级诊断，不模拟资金占用与并发持仓，<b>非账户级蒙特卡洛</b>。
        </div>
      </div>
      {mc ? (
        <div className="space-y-2 p-3">
          <div className="grid grid-cols-5 gap-px overflow-hidden rounded-input border border-border bg-border">
            {([
              ['终值 p05', mc.final_return.p05],
              ['终值 p50', mc.final_return.p50],
              ['终值 p95', mc.final_return.p95],
            ] as const).map(([label, value]) => (
              <div key={label} className="bg-surface px-2 py-2 text-center">
                <div className="text-[10px] text-muted">{label}</div>
                <div className={`mt-0.5 font-mono text-sm font-semibold num ${priceColorClass(value - 1)}`}>{fmtPct(value - 1, 1)}</div>
              </div>
            ))}
            {([
              ['重排回撤 p50', mc.max_drawdown.p50],
              ['重排回撤 p95', mc.max_drawdown.p95],
            ] as const).map(([label, value]) => (
              <div key={label} className="bg-surface px-2 py-2 text-center">
                <div className="text-[10px] text-muted">{label}</div>
                <div className="mt-0.5 font-mono text-sm font-semibold text-bear num">{fmtPct(value, 1)}</div>
              </div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-input border border-border bg-border">
            <div className="bg-surface px-2 py-2 text-center">
              <div className="text-[10px] text-muted">终值为负概率</div>
              <div className={`mt-0.5 font-mono text-sm font-semibold num ${priceColorClass(-mc.prob_final_negative)}`}>{fmtPct(mc.prob_final_negative, 1)}</div>
            </div>
            <div className="bg-surface px-2 py-2 text-center">
              <div className="text-[10px] text-muted">重排回撤比实际更深</div>
              <div className="mt-0.5 font-mono text-sm font-semibold text-foreground num">
                {mc.prob_max_dd_worse_than_actual == null ? '—' : fmtPct(mc.prob_max_dd_worse_than_actual, 1)}
              </div>
            </div>
          </div>
          <DdHistogram histogram={mc.dd_histogram} />
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
            <span className="font-mono">n_trades: {mc.n_trades}</span>
            <span className="font-mono">n_sims: {mc.n_sims.toLocaleString('zh-CN')}</span>
            <span className="font-mono">seed: {mc.seed}</span>
          </div>
        </div>
      ) : (
        <div className="px-3 py-3 text-[11px] leading-5 text-muted">交易样本不足(&lt;30)，无法做顺序重排诊断 — 系 fail-closed 缺省，不输出任何分位。</div>
      )}
    </div>
  )
}

export function StrategyRobustnessPanel({ request }: Props) {
  const [nFolds, setNFolds] = useState(4)
  const [runPermutation, setRunPermutation] = useState(false)
  const [runPerturbation, setRunPerturbation] = useState(true)
  const [runWalkForward, setRunWalkForward] = useState(false)
  const mutation = useMutation({
    mutationFn: () => api.strategyRobustness({
      ...request,
      n_folds: nFolds,
      bootstrap: true,
      mc_permutation: runPermutation,
      parameter_perturbation: runPerturbation,
      max_perturbed_params: 6,
      ...(runWalkForward ? { walk_forward_enabled: true } : {}),
    }),
  })

  return (
    <section className="rounded-btn border border-border bg-surface/40">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-secondary"><FlaskConical className="h-3.5 w-3.5 text-accent" />稳健性检验</div>
          <div className="mt-0.5 text-[10px] text-muted">分段稳定性、Bootstrap 置信区间与参数敏感性；严格 Walk-Forward 样本外需勾选开启（会额外多次训练并重跑样本外）。会执行多次同口径回测并保存新的历史 Run。</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-[11px] text-muted">
            分段/折
            <select className="control h-7 w-14 px-1 text-[11px]" value={nFolds} onChange={event => setNFolds(Number(event.target.value))} disabled={mutation.isPending}>
              {[3, 4, 5, 6].map(value => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-muted" title="严格 Walk-Forward：每折先在训练窗对全部候选参数重复训练、选出并冻结参数，再在样本外窗口重跑一次。会额外执行多次训练与 OOS 重跑，运行时间显著增加；候选数受服务端执行预算限制（训练+OOS 最多 24 次）。"><input type="checkbox" className="h-3.5 w-3.5 rounded border-border accent-accent" checked={runWalkForward} onChange={event => setRunWalkForward(event.target.checked)} disabled={mutation.isPending} />严格 Walk-Forward</label>
          <label className="flex items-center gap-1.5 text-[11px] text-muted"><input type="checkbox" className="h-3.5 w-3.5 rounded border-border accent-accent" checked={runPerturbation} onChange={event => setRunPerturbation(event.target.checked)} disabled={mutation.isPending} />参数扰动</label>
          <label className="flex items-center gap-1.5 text-[11px] text-muted"><input type="checkbox" className="h-3.5 w-3.5 rounded border-border accent-accent" checked={runPermutation} onChange={event => setRunPermutation(event.target.checked)} disabled={mutation.isPending} />随机置换</label>
          <button type="button" onClick={() => mutation.mutate()} disabled={mutation.isPending} className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
            {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
            {mutation.isPending ? '检验中…' : '运行检验'}
          </button>
        </div>
      </div>

      {mutation.error && <div className="mx-3 mt-3 rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">{mutation.error instanceof Error ? mutation.error.message : '稳健性检验失败'}</div>}
      {mutation.data ? (
        <div className="space-y-3 p-3">
          <Summary result={mutation.data} />
          <SegmentStability result={mutation.data} />
          <WalkForwardOOS result={mutation.data} />
          <Perturbation result={mutation.data} />
          <ExitBreakdown result={mutation.data} />
          <TradeEquityBandSection result={mutation.data} />
          <MonteCarloShuffleSection result={mutation.data} />
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
            <span className="font-mono">run_id: {mutation.data.run_id}</span>
            <span className="font-mono">seed: {mutation.data.random_seed}</span>
            <span>结果已写入运行历史，可与基线 Run 对比。</span>
          </div>
        </div>
      ) : (
        <div className="px-3 py-3 text-[11px] leading-5 text-muted">先完成一次正式策略回测，再按相同股票池、区间、成本、撮合与仓位参数执行检验。分段/折越多、参数越多，运行时间越长。严格 Walk-Forward 默认关闭：开启后每折会在训练窗对全部候选参数重复训练、冻结后再重跑样本外窗口，耗时显著增加（候选数受执行预算截断）。</div>
      )}
    </section>
  )
}
