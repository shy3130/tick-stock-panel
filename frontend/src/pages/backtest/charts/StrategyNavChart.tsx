import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart, AreaSeries, BaselineSeries, LineSeries, ColorType, CrosshairMode, LineStyle, LineType,
  type AutoscaleInfo, type IChartApi, type ISeriesApi, type SeriesType, type Time, type MouseEventParams } from 'lightweight-charts'
import type { StrategyBacktestResult } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { timeKey } from '@/lib/lightweight-model'

/**
 * 回测净值图 — TradingView lightweight-charts v5:
 *   窗格0 主图: 策略净值(面积, 右轴资金) + 同期基准指数(虚线, 左轴点位)
 *              + 仓位(浅黄色阶梯面积, 独立 0–100% 刻度覆盖整个主图)
 *   窗格1 回撤 (BaselineSeries 锚 0 向下填充)
 * 双窗格共享时间轴: 联动缩放/十字线, 与个股分析页 (FinancialPlot) 手感一致。
 * 惯用法 (视口保持/主题增量更新/清理) 沿用 FinancialPlot 的成熟模式。
 */

interface Props {
  result: StrategyBacktestResult
}

const NAV_COLOR = '#3b82f6'
const BENCHMARK_COLOR = '#64748b'
const DRAWDOWN_COLOR = '#f04438'
const MAIN_PRICE_MARGINS = { top: 0.08, bottom: 0.06 }

const moneyFmt = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
const axisMoneyFmt = (v: number) => {
  if (Math.abs(v) >= 100_000_000) return `${(v / 100_000_000).toFixed(1)}亿`
  if (Math.abs(v) >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return moneyFmt.format(v)
}

export function StrategyNavChart({ result }: Props) {
  const ct = useChartTheme()
  // 图例点选显隐 (策略净值/回撤常显)。隐藏即从窗格移除, 窗格拓扑随之重建。
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hover, setHover] = useState<number | null>(null)
  const hostRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<SeriesType>[]>([])
  const runRef = useRef<string | number | null>(null)
  const toggleLegend = (name: string) =>
    setHidden(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  // 行数据: 同一时间轴对齐净值/基准/仓位/回撤 (基准缺失日插 whitespace 保持缺口)
  const rows = useMemo(() => {
    if (result.equity_curve.length < 2) return []
    const benchmarkByDate = new Map((result.benchmark_curve ?? []).map(r => [r.date.slice(0, 10), r.close ?? r.value]))
    const ddByDate = new Map(result.drawdown_curve.map(r => [r.date.slice(0, 10), r.value * 100]))
    return result.equity_curve.map(r => {
      const date = r.date.slice(0, 10)
      const position = r.exposure != null ? r.exposure * 100
        : r.cash != null && r.value > 0 ? Math.max(0, Math.min(100, (1 - r.cash / r.value) * 100))
          : null
      return {
        date,
        nav: r.value,
        benchmark: benchmarkByDate.get(date) ?? null,
        position,
        drawdown: ddByDate.get(date) ?? null,
      }
    })
  }, [result.equity_curve, result.drawdown_curve, result.benchmark_curve, result.run_id])

  const hasPosition = rows.some(r => r.position != null)
  const hasBenchmark = rows.some(r => r.benchmark != null)
  const showPosition = hasPosition && !hidden.has('仓位')
  const showBenchmark = hasBenchmark && !hidden.has('同期上证指数')
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const hasData = rows.length > 0

  useEffect(() => {
    if (!hasData || !hostRef.current) return
    const theme = ct
    const chart = createChart(hostRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: theme.text,
        fontFamily: 'JetBrains Mono, monospace', fontSize: 10, attributionLogo: false,
        panes: { separatorColor: theme.border, separatorHoverColor: theme.crosshair, enableResize: true } },
      grid: { vertLines: { visible: false }, horzLines: { color: theme.grid } },
      crosshair: { mode: CrosshairMode.Normal,
        vertLine: { color: theme.crosshair, labelBackgroundColor: theme.crosshairLabelBg },
        horzLine: { color: theme.crosshair, labelBackgroundColor: theme.crosshairLabelBg } },
      rightPriceScale: { borderVisible: false, minimumWidth: 52, scaleMargins: MAIN_PRICE_MARGINS },
      leftPriceScale: { visible: false, borderVisible: false, minimumWidth: 44, scaleMargins: MAIN_PRICE_MARGINS },
      timeScale: { borderColor: theme.border, fixLeftEdge: true, fixRightEdge: true, minBarSpacing: 0.5,
        tickMarkFormatter: (time: Time) => timeKey(time).slice(2, 10) },
      localization: { locale: 'zh-CN', timeFormatter: (time: Time) => timeKey(time) },
    })
    chartRef.current = chart
    const onMove = (event: MouseEventParams<Time>) => {
      if (event.time == null) { setHover(null); return }
      const key = timeKey(event.time)
      setHover(rowsRef.current.findIndex(r => r.date === key))
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      chart.unsubscribeCrosshairMove(onMove)
      chart.remove()
      chartRef.current = null
      seriesRef.current = []
      runRef.current = null
    }
    // 空状态会移除宿主节点, 数据恢复后必须给新节点重新挂载图表。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasData])

  // 主题变化: 增量 applyOptions, 不重建实例/序列
  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { textColor: ct.text, panes: { separatorColor: ct.border } },
      grid: { horzLines: { color: ct.grid } },
      timeScale: { borderColor: ct.border },
      crosshair: { vertLine: { color: ct.crosshair, labelBackgroundColor: ct.crosshairLabelBg },
        horzLine: { color: ct.crosshair, labelBackgroundColor: ct.crosshairLabelBg } },
    })
  }, [ct])

  // 序列重建: 数据或图例拓扑变化时全量重建; 同一 run_id 内保留缩放窗口。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const sameRun = runRef.current === (result.run_id ?? null)
    const view = sameRun ? chart.timeScale().getVisibleLogicalRange() : null
    runRef.current = result.run_id ?? null
    for (const series of seriesRef.current) { try { chart.removeSeries(series) } catch { /* 实例已销毁 */ } }
    seriesRef.current = []
    if (rows.length === 0) return

    // 仓位与净值共用主图区, 各自保留资金与百分比刻度; 回撤紧接其下。
    const nav = chart.addSeries(AreaSeries, {
      lineColor: NAV_COLOR, lineWidth: 1, topColor: 'rgba(59,130,246,0.14)', bottomColor: 'rgba(59,130,246,0.01)',
      priceFormat: { type: 'custom', formatter: axisMoneyFmt, minMove: 0.01 },
    }, 0)
    nav.setData(rows.map(r => ({ time: r.date as Time, value: r.nav })))
    seriesRef.current.push(nav)

    if (showBenchmark) {
      const benchmark = chart.addSeries(LineSeries, {
        color: BENCHMARK_COLOR, lineWidth: 1, lineStyle: LineStyle.Dashed,
        priceScaleId: 'left',
        priceFormat: { type: 'custom', formatter: (v: number) => v.toFixed(0), minMove: 0.01 },
      }, 0)
      benchmark.setData(rows.map(r => r.benchmark != null ? { time: r.date as Time, value: r.benchmark } : { time: r.date as Time }))
      seriesRef.current.push(benchmark)
    }
    chart.applyOptions({ leftPriceScale: { visible: showBenchmark } })
    // 重建窗格时恢复主图边距, 避免沿用回撤窗格的零上边距。
    chart.priceScale('right', 0).applyOptions({ scaleMargins: MAIN_PRICE_MARGINS })
    chart.priceScale('left', 0).applyOptions({ scaleMargins: MAIN_PRICE_MARGINS })

    if (showPosition) {
      const position = chart.addSeries(BaselineSeries, {
        // 以 0% 为填充基线, 空仓不着色。
        baseValue: { type: 'price', price: 0 }, lineType: LineType.WithSteps,
        lineVisible: false, crosshairMarkerVisible: false,
        topLineColor: 'transparent', bottomLineColor: 'transparent',
        topFillColor1: 'rgba(245,158,11,0.08)', topFillColor2: 'rgba(245,158,11,0.015)',
        bottomFillColor1: 'transparent', bottomFillColor2: 'transparent',
        priceScaleId: 'position', priceLineVisible: false, lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: (v: number) => `${v.toFixed(0)}%`, minMove: 0.01 },
        // 固定 0-100 满刻度, 不随数据缩放
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      }, 0)
      position.setData(rows.filter(r => r.position != null).map(r => ({ time: r.date as Time, value: r.position! })))
      position.setSeriesOrder(0)
      seriesRef.current.push(position)
      chart.priceScale('position').applyOptions({ scaleMargins: MAIN_PRICE_MARGINS })
    }

    // 回撤用 BaselineSeries 锚 0: 0 → 回撤线之间填充, 深度一眼可读
    const drawdown = chart.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: 0 },
      // 基准填充不参与自动缩放, 局部负回撤区间也须保留 0% 参照。
      autoscaleInfoProvider: (getRange: () => AutoscaleInfo | null) => {
        const info = getRange()
        if (!info?.priceRange) return info
        const minValue = Math.min(info.priceRange.minValue, 0)
        const maxValue = Math.max(info.priceRange.maxValue, 0)
        return {
          ...info,
          priceRange: {
            // 全程无回撤时仍让 0% 贴上沿, 避免图形库把退化范围对称展开。
            minValue: minValue === 0 && maxValue === 0 ? -1 : minValue,
            maxValue,
          },
        }
      },
      topLineColor: 'rgba(240,68,56,0.25)', topFillColor1: 'rgba(240,68,56,0.02)', topFillColor2: 'rgba(240,68,56,0.02)',
      bottomLineColor: DRAWDOWN_COLOR, bottomFillColor1: 'rgba(240,68,56,0.05)', bottomFillColor2: 'rgba(240,68,56,0.24)',
      lineWidth: 1,
      priceFormat: { type: 'custom', formatter: (v: number) => `${v.toFixed(1)}%`, minMove: 0.01 },
    }, 1)
    drawdown.setData(rows.filter(r => r.drawdown != null).map(r => ({ time: r.date as Time, value: r.drawdown! })))
    seriesRef.current.push(drawdown)
    chart.priceScale('right', 1).applyOptions({ entireTextOnly: true, scaleMargins: { top: 0, bottom: MAIN_PRICE_MARGINS.bottom } })

    // 窗格高度配比: 主图为主, 回撤为辅
    const panes = chart.panes()
    panes[0]?.setStretchFactor(0.7)
    panes[1]?.setStretchFactor(0.3)

    if (view) chart.timeScale().setVisibleLogicalRange(view)
    else chart.timeScale().fitContent()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, showPosition, showBenchmark, result.run_id])

  const hoverRow = hover != null && rows[hover] ? rows[hover] : null

  if (rows.length === 0) {
    return <div className="flex h-[340px] items-center justify-center text-xs text-muted">净值数据不足，无法绘制曲线</div>
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 pb-2">
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-[#3b82f6]" />
          策略净值
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-[#f04438]" />
          回撤
        </span>
        {hasPosition && (
          <button type="button" onClick={() => toggleLegend('仓位')} title="点击显示/隐藏"
            className={`flex items-center gap-1.5 text-[10px] text-secondary cursor-pointer transition-opacity ${showPosition ? 'opacity-100' : 'opacity-40'}`}>
            <span className="h-2.5 w-3 rounded-sm bg-amber-500/15" />
            仓位
          </button>
        )}
        {hasBenchmark && (
          <button type="button" onClick={() => toggleLegend('同期上证指数')} title="点击显示/隐藏"
            className={`flex items-center gap-1.5 text-[10px] text-secondary cursor-pointer transition-opacity ${showBenchmark ? 'opacity-100' : 'opacity-40'}`}>
            <span className="w-3 h-0.5 rounded border-t border-dashed border-[#64748b]" />
            同期上证指数
          </button>
        )}
        {/* 悬停读数: 十字线所在日期各序列当前值 */}
        <span className="ml-auto flex items-center gap-3 font-mono text-[10px] text-muted">
          {hoverRow ? <>
            <span>{hoverRow.date}</span>
            <span className="text-[#3b82f6]">{moneyFmt.format(hoverRow.nav)}</span>
            {showBenchmark && hoverRow.benchmark != null && <span className="text-[#94a3b8]">{hoverRow.benchmark.toFixed(2)}点</span>}
            {showPosition && hoverRow.position != null && <span className="text-[#f59e0b]">{hoverRow.position.toFixed(0)}%</span>}
            {hoverRow.drawdown != null && <span className="text-[#f04438]">{hoverRow.drawdown.toFixed(1)}%</span>}
          </> : <span>滚轮缩放 · 拖动平移 · 悬停读数</span>}
        </span>
      </div>
      <div ref={hostRef} className="h-[340px]" />
    </div>
  )
}
