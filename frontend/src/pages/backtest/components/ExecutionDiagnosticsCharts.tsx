import { useMemo } from 'react'
import type { EChartsOption, SeriesOption } from 'echarts'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useECharts } from '../charts/useECharts'

/**
 * 执行诊断图表 — 暴露/换手/成本时间线、水下持续时长、退出原因与标的贡献。
 *
 * 数据完全由已有 equity_curve / drawdown_curve / trades / config 推导，不修改交易计算。
 * 成本为固定成本模型估算，与引擎 _calc_portfolio_stats 的 cost_breakdown 同口径：
 *   单边成本 = 名义金额(entry_value/exit_value) × (fees_pct + slippage_bps / 10000)。
 *
 * 图表均为独立组件，由调用方确认数据就绪后再挂载：
 * useECharts 的初始化 effect 依赖为空只跑一次，数据未就绪时挂载会让图表永久空白。
 */

export const EXIT_REASON_LABELS: Record<string, string> = {
  signal: '策略信号',
  stop_loss: '固定止损',
  take_profit: '固定止盈',
  trailing_stop: '移动止损',
  trailing_take_profit: '回撤止盈',
  max_hold: '到期平仓',
  end: '区间结束',
}

const finite = (value: unknown): number | null => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** 归一化 ISO 日期前缀(YYYY-MM-DD); 非字符串/缺失安全返回 '' */
const dateOf = (value: unknown) => String(value ?? '').slice(0, 10)

const moneyFmt = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
const axisMoney = (value: number) => {
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}亿`
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(1)}万`
  return moneyFmt.format(value)
}

const TOOLTIP_STYLE = {
  backgroundColor: 'rgba(15,23,42,0.95)',
  borderColor: 'rgba(148,163,184,0.2)',
  textStyle: { color: '#e2e8f0', fontSize: 12 },
}
const AXIS_LABEL = { color: '#64748b', fontSize: 10 }
const AXIS_LINE = { lineStyle: { color: '#334155' } }
const SPLIT_LINE = { lineStyle: { color: '#1e293b' } }
const LEGEND_STYLE = { top: 0, textStyle: { color: '#94a3b8', fontSize: 10 } }

/** ECharts tooltip/label 回调参数中本文件实际读取的字段(结构子集, 由回调契约保证) */
interface ChartParam {
  seriesName?: string
  value?: unknown
  axisValue?: string
  color?: unknown
  dataIndex: number
}

// ===== 执行时间线: 暴露 / 当日换手 / 累计估算成本 =====

export interface ExecutionRow {
  date: string
  /** 持仓暴露 0..1(取自净值曲线, 缺失为 null) */
  exposure: number | null
  /** 当日换手 = 当日成交名义金额 / 初始资金(无法计算时为 null) */
  turnover: number | null
  /** 累计估算成本(货币, 固定成本模型) */
  cumCost: number | null
}

export interface ExecutionModel {
  rows: ExecutionRow[]
  hasExposure: boolean
  hasTurnover: boolean
  hasCost: boolean
  costRate: number
  /** Σ名义金额 × costRate, 可与 stats.cost_breakdown.total 校验 */
  estimatedCostTotal: number | null
  /** 缺少 entry_value/exit_value 而未计入时间线的交易笔数 */
  skippedTrades: number
}

export function buildExecutionModel(
  curve: Array<{ date: string; value: number; exposure?: number }>,
  trades: StrategyBacktestTrade[] | undefined,
  config: Record<string, unknown> | undefined,
  fallbackInitialCapital: unknown,
): ExecutionModel | null {
  if (!curve?.length) return null
  // 与后端一致: max(fees_pct, 0) + max(slippage_bps, 0) / 10000
  const fees = Math.max(finite(config?.fees_pct) ?? 0, 0)
  const slippage = Math.max(finite(config?.slippage_bps) ?? 0, 0)
  const costRate = fees + slippage / 10000
  const capitalParsed = finite(config?.initial_capital) ?? finite(fallbackInitialCapital)
  const capital = capitalParsed != null && capitalParsed > 0 ? capitalParsed : null

  // 暴露序列: 日期稳定排序, 重复日期保留首个
  const exposureByDate = new Map<string, number | null>()
  const sortedCurve = [...curve].sort((left, right) => {
    const a = dateOf(left?.date)
    const b = dateOf(right?.date)
    return a < b ? -1 : a > b ? 1 : 0
  })
  for (const point of sortedCurve) {
    const date = dateOf(point?.date)
    if (!date || exposureByDate.has(date)) continue
    exposureByDate.set(date, finite(point?.exposure))
  }

  // 当日名义成交: entry_value 计入 entry_date, exit_value 计入 exit_date(与 cost_breakdown 的 gross_notional 同口径)
  const notionalByDate = new Map<string, number>()
  let skippedTrades = 0
  let totalNotional = 0
  for (const trade of trades ?? []) {
    const entryValue = finite(trade?.entry_value)
    const exitValue = finite(trade?.exit_value)
    if (entryValue == null && exitValue == null) {
      skippedTrades += 1
      continue
    }
    const entry = Math.max(entryValue ?? 0, 0)
    const exit = Math.max(exitValue ?? 0, 0)
    totalNotional += entry + exit
    const entryDate = dateOf(trade?.entry_date)
    const exitDate = dateOf(trade?.exit_date)
    if (entry > 0 && entryDate) notionalByDate.set(entryDate, (notionalByDate.get(entryDate) ?? 0) + entry)
    if (exit > 0 && exitDate) notionalByDate.set(exitDate, (notionalByDate.get(exitDate) ?? 0) + exit)
  }

  // 时间轴: 净值日期 ∪ 成交日期, 字典序稳定(ISO 日期字典序即时间序)
  const dates = [...new Set([...exposureByDate.keys(), ...notionalByDate.keys()])].sort()
  const hasExposure = [...exposureByDate.values()].some(value => value != null)
  const hasTurnover = totalNotional > 0 && capital != null
  const hasCost = totalNotional > 0

  let cumCost = 0
  const rows: ExecutionRow[] = dates.map(date => {
    const notional = notionalByDate.get(date) ?? 0
    cumCost += notional * costRate
    return {
      date,
      exposure: exposureByDate.get(date) ?? null,
      turnover: hasTurnover && capital != null ? notional / capital : null,
      cumCost: hasCost ? cumCost : null,
    }
  })
  return {
    rows,
    hasExposure,
    hasTurnover,
    hasCost,
    costRate,
    estimatedCostTotal: hasCost ? totalNotional * costRate : null,
    skippedTrades,
  }
}

export function ExecutionTimelineChart({ model }: { model: ExecutionModel }) {
  const option = useMemo<EChartsOption>(() => {
    const exposureSeries: SeriesOption[] = model.hasExposure
      ? [{
          name: '持仓暴露',
          type: 'line',
          symbol: 'none',
          data: model.rows.map(row => (row.exposure == null ? null : Math.round(row.exposure * 10000) / 100)),
          lineStyle: { width: 1.6, color: '#3b82f6' },
        }]
      : []
    const turnoverSeries: SeriesOption[] = model.hasTurnover
      ? [{
          name: '当日换手',
          type: 'bar',
          barMaxWidth: 8,
          data: model.rows.map(row => (row.turnover == null ? null : Math.round(row.turnover * 100000) / 1000)),
          itemStyle: { color: 'rgba(245,158,11,0.55)' },
        }]
      : []
    const costSeries: SeriesOption[] = model.hasCost
      ? [{
          name: '累计估算成本',
          type: 'line',
          symbol: 'none',
          yAxisIndex: 1,
          data: model.rows.map(row => (row.cumCost == null ? null : Math.round(row.cumCost * 100) / 100)),
          lineStyle: { width: 1.4, color: '#10b981' },
        }]
      : []
    return {
      animation: false,
      grid: { left: 46, right: 56, top: 26, bottom: 30 },
      legend: LEGEND_STYLE,
      tooltip: {
        trigger: 'axis',
        ...TOOLTIP_STYLE,
        formatter: (params: ChartParam | ChartParam[]) => {
          const list = Array.isArray(params) ? params : [params]
          const date = list[0]?.axisValue ?? ''
          let html = `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">${date}</div>`
          for (const item of list) {
            if (item.value == null) continue
            const value = item.seriesName === '累计估算成本'
              ? `${moneyFmt.format(Number(item.value))} 元(固定成本模型估算)`
              : `${Number(item.value).toFixed(2)}%`
            html += `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${String(item.color ?? '')}">${item.seriesName ?? ''}</span><span style="font-family:monospace">${value}</span></div>`
          }
          return html
        },
      },
      xAxis: {
        type: 'category',
        data: model.rows.map(row => row.date),
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(model.rows.length / 6)) },
        axisLine: AXIS_LINE,
      },
      yAxis: [
        {
          type: 'value',
          axisLabel: { ...AXIS_LABEL, formatter: (value: number) => `${value.toFixed(0)}%` },
          splitLine: SPLIT_LINE,
        },
        {
          type: 'value',
          position: 'right',
          axisLabel: { ...AXIS_LABEL, formatter: axisMoney },
          splitLine: { show: false },
        },
      ],
      series: [...exposureSeries, ...turnoverSeries, ...costSeries],
    }
  }, [model])
  const ref = useECharts(option, [model])

  return <div ref={ref} className="h-[220px]" />
}

// ===== 水下回撤与持续时长 =====

export interface UnderwaterRow {
  date: string
  /** 回撤幅度(%，≤0) */
  drawdownPct: number
  /** 截至当日的连续水下天数(净值回到前高后归零) */
  days: number
}

export function buildUnderwaterRows(
  drawdownCurve: Array<{ date: string; value: number }> | undefined,
  equityCurve: Array<{ date: string; value: number }>,
): UnderwaterRow[] {
  let points: Array<{ date: string; dd: number }> = []
  if (drawdownCurve?.length) {
    points = drawdownCurve.flatMap(point => {
      const dd = finite(point?.value)
      const date = dateOf(point?.date)
      return dd == null || !date ? [] : [{ date, dd }]
    })
  } else {
    // 旧结果缺 drawdown_curve 时由净值曲线峰值推导
    let peak = Number.NEGATIVE_INFINITY
    points = (equityCurve ?? []).flatMap(point => {
      const value = finite(point?.value)
      const date = dateOf(point?.date)
      if (value == null || value <= 0 || !date) return []
      peak = Math.max(peak, value)
      return [{ date, dd: value / peak - 1 }]
    })
  }
  // 日期稳定排序, 重复日期保留首个
  const seen = new Set<string>()
  const ordered = [...points]
    .sort((left, right) => (left.date < right.date ? -1 : left.date > right.date ? 1 : 0))
    .filter(point => {
      if (seen.has(point.date)) return false
      seen.add(point.date)
      return true
    })
  let days = 0
  return ordered.map(point => {
    days = point.dd < -1e-9 ? days + 1 : 0
    return { date: point.date, drawdownPct: point.dd * 100, days }
  })
}

export function UnderwaterDurationChart({ rows }: { rows: UnderwaterRow[] }) {
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 46, right: 46, top: 26, bottom: 30 },
    legend: LEGEND_STYLE,
    tooltip: {
      trigger: 'axis',
      ...TOOLTIP_STYLE,
      formatter: (params: ChartParam | ChartParam[]) => {
        const list = Array.isArray(params) ? params : [params]
        const date = list[0]?.axisValue ?? ''
        let html = `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">${date}</div>`
        for (const item of list) {
          if (item.value == null) continue
          const value = item.seriesName === '连续水下天数'
            ? `${Number(item.value)} 天`
            : `${Number(item.value).toFixed(2)}%`
          html += `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${String(item.color ?? '')}">${item.seriesName ?? ''}</span><span style="font-family:monospace">${value}</span></div>`
        }
        return html
      },
    },
    xAxis: {
      type: 'category',
      data: rows.map(row => row.date),
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(rows.length / 6)) },
      axisLine: AXIS_LINE,
    },
    yAxis: [
      {
        type: 'value',
        max: 0,
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => `${value.toFixed(1)}%` },
        splitLine: SPLIT_LINE,
      },
      {
        type: 'value',
        position: 'right',
        minInterval: 1,
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => `${value}天` },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '回撤',
        type: 'line',
        symbol: 'none',
        data: rows.map(row => Math.round(row.drawdownPct * 100) / 100),
        lineStyle: { color: 'rgba(240,68,56,0.6)', width: 1 },
        areaStyle: { color: 'rgba(240,68,56,0.12)' },
      },
      {
        name: '连续水下天数',
        type: 'bar',
        yAxisIndex: 1,
        barMaxWidth: 6,
        data: rows.map(row => row.days),
        itemStyle: { color: 'rgba(245,158,11,0.5)' },
      },
    ],
  }), [rows])
  const ref = useECharts(option, [rows])

  return <div ref={ref} className="h-[180px]" />
}

// ===== 退出原因 / 标的贡献 =====

export interface ContributionRow {
  key: string
  /** 轴标签(短) */
  label: string
  /** tooltip 标题(含代码) */
  detail: string
  count: number
  totalPnl: number
  avgPnlPct: number | null
  winRate: number | null
}

export interface ContributionModel {
  rows: ContributionRow[]
  /** 是否存在可用的 pnl_amount; 不可用时柱长退化为笔数 */
  pnlAvailable: boolean
  /** 被折叠进"其余"的组数 */
  restCount: number
}

interface AggRow {
  key: string
  label: string
  detail: string
  count: number
  wins: number
  totalPnl: number
  pnlPctSum: number
}

export function buildContributionModel(
  trades: StrategyBacktestTrade[] | undefined,
  groupBy: 'exit_reason' | 'symbol',
  topN = 10,
): ContributionModel {
  const groups = new Map<string, AggRow>()
  let pnlAvailable = false
  for (const trade of trades ?? []) {
    const pnlAmount = finite(trade?.pnl_amount)
    if (pnlAmount != null) pnlAvailable = true
    const pnlPct = finite(trade?.pnl_pct)
    let key: string
    let label: string
    let detail: string
    if (groupBy === 'exit_reason') {
      key = String(trade?.exit_reason || 'unknown')
      label = EXIT_REASON_LABELS[key] ?? key
      detail = label
    } else {
      key = String(trade?.symbol || 'unknown')
      const name = typeof trade?.name === 'string' && trade.name.trim() ? trade.name.trim() : ''
      label = name || key
      detail = name && name !== key ? `${name} ${key}` : key
    }
    const row = groups.get(key) ?? { key, label, detail, count: 0, wins: 0, totalPnl: 0, pnlPctSum: 0 }
    row.count += 1
    row.wins += (pnlPct ?? 0) > 0 ? 1 : 0
    row.totalPnl += pnlAmount ?? 0
    row.pnlPctSum += pnlPct ?? 0
    groups.set(key, row)
  }
  const rows = [...groups.values()]
  // 排序: 盈亏可用时按 |累计盈亏| 降序, 否则按笔数降序; 并列按笔数、key 升序保证确定性
  rows.sort((left, right) => {
    const diff = (pnlAvailable ? Math.abs(right.totalPnl) : right.count) - (pnlAvailable ? Math.abs(left.totalPnl) : left.count)
    if (diff !== 0) return diff
    if (right.count !== left.count) return right.count - left.count
    return left.key < right.key ? -1 : left.key > right.key ? 1 : 0
  })
  let restCount = 0
  let merged: AggRow[] = rows
  if (rows.length > topN) {
    const rest = rows.slice(topN)
    restCount = rest.length
    const noun = groupBy === 'symbol' ? '只' : '类'
    const restRow = rest.reduce(
      (sum, row) => ({
        ...sum,
        count: sum.count + row.count,
        wins: sum.wins + row.wins,
        totalPnl: sum.totalPnl + row.totalPnl,
        pnlPctSum: sum.pnlPctSum + row.pnlPctSum,
      }),
      { key: '__rest__', label: `其余 ${restCount} ${noun}`, detail: `其余 ${restCount} ${noun}合计`, count: 0, wins: 0, totalPnl: 0, pnlPctSum: 0 },
    )
    merged = [...rows.slice(0, topN), restRow]
  }
  return {
    rows: merged.map(row => ({
      key: row.key,
      label: row.label,
      detail: row.detail,
      count: row.count,
      totalPnl: row.totalPnl,
      avgPnlPct: row.count > 0 ? row.pnlPctSum / row.count : null,
      winRate: row.count > 0 ? row.wins / row.count : null,
    })),
    pnlAvailable,
    restCount,
  }
}

export function ContributionChart({ model }: { model: ContributionModel }) {
  // 旧结果缺 pnl_amount(或盈亏恰好全为 0)时柱长按笔数展示
  const usePnl = model.pnlAvailable && model.rows.some(row => row.totalPnl !== 0)
  const height = Math.min(320, Math.max(150, model.rows.length * 26 + 52))
  const option = useMemo<EChartsOption>(() => {
    // yAxis 类目自下而上, 反转后排名靠前者显示在最上方
    const ordered = [...model.rows].reverse()
    return {
      animation: false,
      grid: { left: 8, right: 44, top: 8, bottom: 24, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        ...TOOLTIP_STYLE,
        formatter: (params: ChartParam | ChartParam[]) => {
          const item = Array.isArray(params) ? params[0] : params
          const row = item ? ordered[item.dataIndex] : undefined
          if (!row) return ''
          const pnl = `${row.totalPnl >= 0 ? '+' : ''}${moneyFmt.format(Math.round(row.totalPnl))} 元`
          const winRate = row.winRate == null ? '—' : `${(row.winRate * 100).toFixed(1)}%`
          const avgPnl = row.avgPnlPct == null ? '—' : `${(row.avgPnlPct * 100).toFixed(1)}%`
          return [
            `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">${row.detail}</div>`,
            `笔数 ${row.count} · 胜率 ${winRate} · 平均盈亏 ${avgPnl}`,
            `累计盈亏 <span style="font-family:monospace">${pnl}</span>`,
          ].join('<br/>')
        },
      },
      xAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL, formatter: usePnl ? axisMoney : (value: number) => `${value}` },
        splitLine: SPLIT_LINE,
      },
      yAxis: {
        type: 'category',
        data: ordered.map(row => row.label),
        axisLabel: { ...AXIS_LABEL, width: 88, overflow: 'truncate' },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      series: [
        {
          name: usePnl ? '累计盈亏' : '笔数',
          type: 'bar',
          barMaxWidth: 14,
          data: ordered.map(row => ({
            value: usePnl ? Math.round(row.totalPnl * 100) / 100 : row.count,
            itemStyle: {
              color: !usePnl ? '#3b82f6' : row.totalPnl > 0 ? '#ef4444' : row.totalPnl < 0 ? '#22c55e' : '#64748b',
            },
          })),
          label: {
            show: true,
            position: 'right',
            color: '#94a3b8',
            fontSize: 9,
            formatter: (params: { value?: unknown }) => (usePnl ? axisMoney(Number(params.value)) : String(params.value ?? '')),
          },
        },
      ],
    }
  }, [model, usePnl])
  const ref = useECharts(option, [model])

  return (
    <div>
      <div ref={ref} style={{ height }} />
      {!model.pnlAvailable && (
        <div className="px-3 pb-2 text-[10px] text-muted">旧结果缺少 pnl_amount 字段，柱长按笔数展示。</div>
      )}
    </div>
  )
}
